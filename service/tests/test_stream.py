#!/usr/bin/env python3
"""End-to-end regression test: no hardware, no WAD, no compiler.

Runs the service on the test-pattern source and drives it with a headless
client, checking the things the firmware will depend on: the handshake, caps
negotiation, fragment reassembly, every rect encoding, the idle skip, and the
keepalive that stops a static screen looking like a dead link.

    python service/tests/test_stream.py
    python service/tests/test_stream.py --doom    # also test against real DOOM

Exit code is non-zero if anything fails, so it works as a CI gate.
"""

import argparse
import io
import os
import random
import socket
import struct
import subprocess
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE = os.path.abspath(os.path.join(HERE, ".."))
ROOT = os.path.abspath(os.path.join(SERVICE, ".."))
sys.path.insert(0, SERVICE)

import numpy as np
from PIL import Image

from microstream import protocol as P

PORT = 20096
TOKEN = "test-token"

failures = []


def check(name, ok, detail=""):
    print("%-44s %s%s" % (name, "PASS" if ok else "FAIL", "  " + detail if detail else ""))
    if not ok:
        failures.append(name)


def rgb565_to_array(data, w, h):
    v = np.frombuffer(data, dtype=">u2").reshape(h, w)
    out = np.empty((h, w, 3), dtype=np.uint8)
    out[:, :, 0] = ((v >> 11) & 0x1F) << 3
    out[:, :, 1] = ((v >> 5) & 0x3F) << 2
    out[:, :, 2] = (v & 0x1F) << 3
    return out


class HeadlessClient:
    """The firmware's decode path, in as little Python as it takes."""

    def __init__(self, port, token=TOKEN, caps=P.CAP_ALL, ack=True, loss=0.0,
                 min_gap_ms=0.0):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.connect(("127.0.0.1", port))
        self.sock.setblocking(False)
        self.token = token
        self.caps = caps
        # A client that never acknowledges stands in for one too slow to
        # reassemble anything -- which is exactly what the device looked like
        # before flow control existed.
        self.ack = ack
        # Simulated packet loss: this is what a device whose UDP queue
        # overflows during a JPEG decode looks like from the server.
        self.loss = loss
        # Minimum spacing this client can cope with. The AtomS3R's socket holds
        # one datagram: anything arriving while it still holds the previous one
        # is gone. Modelling that here is the only way to keep it fixed.
        self.min_gap_ms = min_gap_ms
        self._last_rx = 0.0
        self.gap_drops = 0
        self.session = 0
        self.connected = False
        self.seq = 0
        self.canvas = Image.new("RGB", (128, 128))
        self.asm_id, self.asm, self.asm_count = None, {}, 0
        self.frames = self.bytes = self.rects = self.keepalives = 0
        self.enc = {}
        self.state = -1
        self.last_frame_id = 0
        self.decode_errors = 0

    def hello(self):
        self.sock.send(P.pack_hello(self.token, 128, 128, self.caps))

    def input(self, buttons=0, accel=(0, 0, 1000)):
        self.seq += 1
        self.sock.send(P.pack_input(self.session, self.seq, buttons, *accel,
                                    self.last_frame_id if self.ack else 0))

    def pump(self):
        while True:
            try:
                data = self.sock.recv(4096)
            except (BlockingIOError, ConnectionResetError):
                return
            if self.loss and random.random() < self.loss:
                continue
            if self.min_gap_ms:
                t = time.monotonic()
                if (t - self._last_rx) * 1000.0 < self.min_gap_ms:
                    self.gap_drops += 1
                    continue
                self._last_rx = t
            t = P.packet_type(data)
            if t == P.PKT_HELLO_ACK:
                ack = P.parse_hello_ack(data)
                if ack and ack["status"] == P.STATUS_OK:
                    self.session = ack["session"]
                    self.connected = True
            elif t == P.PKT_FRAME:
                frag = P.parse_frame(data)
                if frag:
                    self._fragment(frag)

    def _fragment(self, frag):
        fid = frag["frame_id"]
        if self.asm_id != fid:
            self.asm_id, self.asm, self.asm_count = fid, {}, frag["frag_count"]
        self.asm[frag["frag_idx"]] = frag["payload"]
        if len(self.asm) == self.asm_count:
            payload = b"".join(self.asm[i] for i in range(self.asm_count))
            self.asm_id, self.asm = None, {}
            self._apply(fid, payload)

    def _apply(self, fid, payload):
        rects, state = P.parse_update(payload)
        self.state = state
        self.last_frame_id = fid
        self.bytes += len(payload)
        if not rects:
            self.keepalives += 1
            return
        for x, y, w, h, enc, data in rects:
            self.enc[enc] = self.enc.get(enc, 0) + 1
            try:
                if enc == P.ENC_SOLID:
                    v = (data[0] << 8) | data[1]
                    self.canvas.paste((((v >> 11) & 0x1F) << 3,
                                       ((v >> 5) & 0x3F) << 2,
                                       (v & 0x1F) << 3), (x, y, x + w, y + h))
                elif enc == P.ENC_RAW565:
                    self.canvas.paste(Image.fromarray(rgb565_to_array(data, w, h)), (x, y))
                elif enc == P.ENC_DEFLATE565:
                    self.canvas.paste(Image.fromarray(
                        rgb565_to_array(zlib.decompress(data), w, h)), (x, y))
                elif enc == P.ENC_JPEG:
                    img = Image.open(io.BytesIO(data))
                    img.load()
                    self.canvas.paste(img, (x, y))
            except Exception:                                   # noqa: BLE001
                self.decode_errors += 1
        self.frames += 1
        self.rects += len(rects)

    def reset_stats(self):
        self.frames = self.bytes = self.rects = self.keepalives = 0
        self.enc = {}


def drive(client, seconds, buttons=0, accel=(0, 0, 1000)):
    end = time.monotonic() + seconds
    nxt = 0.0
    while time.monotonic() < end:
        client.pump()
        now = time.monotonic()
        if not client.connected:
            client.hello()
            time.sleep(0.05)
        elif now >= nxt:
            nxt = now + 0.033
            client.input(buttons, accel)
        time.sleep(0.002)
    client.pump()
    return client


def start_service(extra):
    proc = subprocess.Popen(
        [sys.executable, "-u", os.path.join(SERVICE, "run.py"),
         "--port", str(PORT), "--token", TOKEN] + extra,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(3.0)
    if proc.poll() is not None:
        print(proc.stdout.read())
        sys.exit("service exited during startup")
    return proc


def stop_service(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def check_recovery():
    """The regression test for frozen regions.

    Dirty rectangles only work if the server diffs against what the client
    actually has. Diffing against the last frame *encoded* instead means a lost
    update leaves those pixels stale forever -- the server believes it already
    sent them -- and only tiles that keep changing ever repair. On hardware
    that looked like two animating blocks on an otherwise frozen screen.

    Keyframes are disabled for this run on purpose. With them on, a periodic
    full frame repairs the damage within a few seconds and the test passes even
    when reference tracking is completely broken -- which is exactly what it
    did the first time it was written.

    So: lose a quarter of all packets while the picture moves, then stop and
    let it settle, and compare against a fresh session's opening keyframe of
    the same still scene. They have to agree.
    """
    print("\n=== recovery from packet loss (keyframes disabled) ===")
    # --no-fit-datagram matters here: with it on, a full-screen keyframe gets
    # squeezed to a lower JPEG quality than the small updates the lossy client
    # assembled its screen from, and the two differ by ~2 mean pixels for
    # reasons that have nothing to do with reference tracking. This test is
    # about staleness, so hold the encoding constant.
    proc = start_service(["--source", "test", "--keyframe-ms", "999999",
                          "--no-fit-datagram"])
    try:
        lossy = HeadlessClient(PORT, loss=0.25)
        drive(lossy, 4.0, buttons=P.BTN_RIGHT)   # move, while dropping packets
        lossy.loss = 0.0
        drive(lossy, 4.0)                        # hold still and let it repair
        settled = np.asarray(lossy.canvas, dtype=np.int16)

        fresh = HeadlessClient(PORT)             # new session opens with a full frame
        drive(fresh, 2.5)
        truth = np.asarray(fresh.canvas, dtype=np.int16)

        # A correctly repaired screen lands at ~0.02 mean difference (JPEG
        # rounding). The same run with reference tracking broken sits at ~4.
        diff = float(np.abs(settled - truth).mean())
        check("screen recovers from 25% packet loss", diff < 0.5,
              "mean pixel difference %.2f from a fresh keyframe" % diff)
    finally:
        stop_service(proc)


def check_fragment_pacing():
    """Regression test for the bug that made the device unusable.

    Its socket holds a single datagram, so two fragments sent back to back
    meant exactly one arrived and *every* update needing two never completed:
    `frags 12, lost 12` in five seconds, while updates that happened to fit in
    one datagram worked perfectly.

    A small fragment size forces multi-fragment updates, and the client refuses
    anything arriving within 2ms of the last packet. With pacing the stream
    survives; without it, nothing completes at all.
    """
    print("\n=== fragment pacing (client drops back-to-back datagrams) ===")
    args = ["--source", "test", "--frag", "600", "--no-fit-datagram"]

    proc = start_service(args + ["--frag-gap", "4"])
    try:
        paced = HeadlessClient(PORT, min_gap_ms=2.0)
        drive(paced, 4.0, buttons=P.BTN_RIGHT)
        check("paced fragments reach a one-datagram client", paced.frames > 15,
              "%d updates, %d datagrams dropped as too close" % (
                  paced.frames, paced.gap_drops))
    finally:
        stop_service(proc)

    # And prove the test bites: the same client, all fragments at once.
    proc = start_service(args + ["--frag-gap", "-1"])
    try:
        unpaced = HeadlessClient(PORT, min_gap_ms=2.0)
        drive(unpaced, 4.0, buttons=P.BTN_RIGHT)
        check("unpaced fragments starve that client (test is meaningful)",
              unpaced.frames < 5,
              "%d updates got through" % unpaced.frames)
    finally:
        stop_service(proc)


def run_suite(extra, label, expect="jpeg"):
    print("\n=== %s ===" % label)
    proc = start_service(extra)
    try:
        bad = HeadlessClient(PORT, token="wrong")
        bad.hello()
        time.sleep(0.4)
        bad.pump()
        check("bad token is rejected", not bad.connected)

        c = HeadlessClient(PORT)
        drive(c, 1.0)
        check("handshake completes", c.connected)

        c.reset_stats()
        drive(c, 3.0, buttons=P.BTN_RIGHT)
        check("frames arrive while moving", c.frames >= 30,
              "%d frames in 3s" % c.frames)
        check("every rect decoded", c.decode_errors == 0,
              "%d errors" % c.decode_errors)
        avg = c.bytes // max(1, c.frames)
        check("frame size fits a WiFi budget", 100 < avg < 12000,
              "avg %d B, %.0f kB/s at 20fps" % (avg, avg * 20 / 1024.0))
        mix = " ".join("%s:%d" % (P.ENC_NAMES[k], v) for k, v in sorted(c.enc.items()))
        if expect == "jpeg":
            # On the measured AtomS3R, a JPEG decode is ~7x cheaper per pixel
            # than a zlib inflate, so the cost model should reach for JPEG even
            # on flat content where deflate would be smaller.
            check("jpeg chosen under the cost model",
                  c.enc.get(P.ENC_JPEG, 0) > c.enc.get(P.ENC_DEFLATE565, 0), mix)
        elif expect == "deflate":
            # --select bytes is for a link-limited setup, and there flat
            # content should go out losslessly and much smaller.
            check("deflate chosen when minimising bytes",
                  c.enc.get(P.ENC_DEFLATE565, 0) > c.enc.get(P.ENC_JPEG, 0), mix)

        # Nothing moving: the encoder should stop sending and fall back to
        # keepalives, which is where the idle win comes from.
        c.reset_stats()
        drive(c, 3.0)
        idle_frames = c.frames
        check("idle costs little", c.bytes < avg * 25,
              "%d B over 3s idle vs %d B/frame moving" % (c.bytes, avg))
        check("keepalives keep the link alive", c.keepalives > 0 or idle_frames > 0,
              "%d keepalives" % c.keepalives)

        # A client that cannot decode JPEG must still get a usable stream.
        # A client without a JPEG decoder gets raw or deflate rectangles, which
        # on DOOM means tens of kilobytes and therefore ~24 fragments per
        # update. Fragment pacing costs it real throughput -- 3ms x 24 is 72ms
        # of transmission before the update can even complete. That is the
        # deliberate price of making one-datagram devices work at all, so the
        # bar here is "still making progress", not "as fast as JPEG".
        c2 = HeadlessClient(PORT, caps=P.CAP_DEFLATE)
        drive(c2, 3.0, buttons=P.BTN_LEFT)
        check("deflate-only client is served", c2.frames > 5 and c2.decode_errors == 0,
              "%d frames, %s" % (c2.frames,
                                 " ".join("%s:%d" % (P.ENC_NAMES[k], v)
                                          for k, v in sorted(c2.enc.items()))))
        check("no jpeg sent to a client without it",
              c2.enc.get(P.ENC_JPEG, 0) == 0)

        # Flow control: an unacknowledging client must be throttled to roughly
        # one update per ack timeout, not flooded at the full frame rate.
        slow = HeadlessClient(PORT, ack=False)
        drive(slow, 3.0, buttons=P.BTN_RIGHT)
        check("flow control throttles a silent client", 1 <= slow.frames <= 20,
              "%d updates in 3s (vs ~60 unthrottled)" % slow.frames)

        c3 = HeadlessClient(PORT, caps=0)
        drive(c3, 3.0, buttons=P.BTN_RIGHT)
        check("raw-only client is served", c3.frames > 3 and c3.decode_errors == 0,
              "%d frames (paced fragments make this path slow by design)"
              % c3.frames)

        return c


    finally:
        stop_service(proc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--doom", action="store_true",
                    help="also run against real DOOM (needs the built binary and a WAD)")
    ap.add_argument("--save", default="", help="write the final frame here")
    args = ap.parse_args()

    c = run_suite(["--source", "test"], "test pattern (cost model)")
    run_suite(["--source", "test", "--select", "bytes"],
              "test pattern (minimising bytes)", expect="deflate")

    if args.doom:
        exe = os.path.join(ROOT, "server",
                           "doom-source.exe" if os.name == "nt" else "doom-source")
        wad = os.path.join(ROOT, "server", "doom1.wad")
        if not (os.path.exists(exe) and os.path.exists(wad)):
            print("\nskipping DOOM: build it with server/build-source.sh and "
                  "fetch a WAD with server/fetch-wad.sh")
        else:
            c = run_suite(["--source", "doom"], "real DOOM")

    check_recovery()
    check_fragment_pacing()

    if args.save:
        c.canvas.save(args.save)
        print("wrote %s" % args.save)

    print()
    if failures:
        print("%d check(s) failed: %s" % (len(failures), ", ".join(failures)))
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
