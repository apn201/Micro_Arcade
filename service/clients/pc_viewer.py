#!/usr/bin/env python3
"""The AtomS3R, simulated: same protocol, same decode path, keyboard instead of tilt.

This is both the phase-1 way to play and the reference implementation for the
firmware -- if a rect encoding works here, porting it to MicroPython is a
translation rather than a design exercise.

    python service/clients/pc_viewer.py
    python service/clients/pc_viewer.py --host 192.168.1.10 --token hunter2
    python service/clients/pc_viewer.py --no-jpeg      # pretend the device cannot decode JPEG
    python service/clients/pc_viewer.py --mode tilt    # arrows tilt, like the board: needed for the kiosk menu

Keys: arrows move/turn, Ctrl fire, Space use, Enter/Esc menus, Shift run,
Tab map, Y confirm, Q quit.
"""

import argparse
import io
import os
import socket
import struct
import sys
import time
import tkinter as tk
import zlib

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

import numpy as np
from PIL import Image, ImageTk

from microstream import protocol as P

# What a hand-held AtomS3R reports at a comfortable tilt, in milli-g.
TILT_FULL = 450
TILT_GRAVITY = 1000


def rgb565_to_array(data, w, h):
    v = np.frombuffer(data, dtype=">u2").reshape(h, w)
    out = np.empty((h, w, 3), dtype=np.uint8)
    out[:, :, 0] = ((v >> 11) & 0x1F) << 3
    out[:, :, 1] = ((v >> 5) & 0x3F) << 2
    out[:, :, 2] = (v & 0x1F) << 3
    return out


class Viewer:
    def __init__(self, args):
        self.args = args
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.addr = (args.host, args.port)

        self.caps = 0
        if not args.no_jpeg:
            self.caps |= P.CAP_JPEG
        if not args.no_deflate:
            self.caps |= P.CAP_DEFLATE

        self.session = 0
        self.connected = False
        self.width, self.height = args.width, args.height
        self.seq = 0
        self.held = set()
        self.last_frame_id = 0
        self.state = P.STATE_LEVEL

        # The screen persists between updates: that is the whole point of
        # sending only what changed.
        self.canvas_img = Image.new("RGB", (self.width, self.height), (0, 0, 0))

        self.asm_id = None
        self.asm = {}
        self.asm_count = 0

        self.t0 = time.monotonic()
        self.frames = self.bytes = self.rects = self.incomplete = 0
        self.enc_counts = {}
        self.rtt_ms = 0
        self.last_rx = 0.0
        self.last_hello = 0.0
        self.last_ping = 0.0
        self.stat_text = "connecting..."

        self.build_ui()

    # --- UI ---------------------------------------------------------------

    def build_ui(self):
        self.root = tk.Tk()
        self.root.title("micro-doom terminal (PC stand-in)")
        self.root.configure(bg="#101010")
        self.label = tk.Label(self.root, bg="#000000")
        self.label.pack(padx=12, pady=(12, 6))
        self.status = tk.Label(self.root, text="", fg="#b0b0b0", bg="#101010",
                               font=("Consolas", 9), anchor="w", justify="left")
        self.status.pack(fill="x", padx=12, pady=(0, 10))
        self.root.bind("<KeyPress>", self.on_key_down)
        self.root.bind("<KeyRelease>", self.on_key_up)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.photo = None

    def on_key_down(self, ev):
        if ev.keysym in ("q", "Q"):
            self.quit()
            return "break"
        self.held.add(ev.keysym)
        return "break" if ev.keysym == "Tab" else None

    def on_key_up(self, ev):
        self.held.discard(ev.keysym)

    # --- protocol ---------------------------------------------------------

    def send_hello(self):
        self.sock.sendto(P.pack_hello(self.args.token, self.width, self.height,
                                      self.caps), self.addr)
        self.last_hello = time.monotonic()

    def buttons_and_tilt(self):
        held = self.held
        buttons = 0
        left, right = "Left" in held, "Right" in held
        up, down = "Up" in held, "Down" in held

        if self.args.mode == "tilt":
            # Report what the real board reports, so the server's default axes
            # (--tilt-turn x- --tilt-move y-) need no changes for PC testing.
            # Measured on the AtomS3R: tilting left reads x = +0.85 g, tilting
            # forward reads y = -0.70 g.
            tilt = [0, 0, TILT_GRAVITY]
            tilt[self.args.tilt_turn_axis] = ((1 if left else 0) - (1 if right else 0)) * TILT_FULL
            tilt[self.args.tilt_move_axis] = ((1 if down else 0) - (1 if up else 0)) * TILT_FULL
            ax, ay, az = tilt
        else:
            ax, ay, az = 0, 0, TILT_GRAVITY
            if left:  buttons |= P.BTN_LEFT
            if right: buttons |= P.BTN_RIGHT
            if up:    buttons |= P.BTN_UP
            if down:  buttons |= P.BTN_DOWN

        if "Control_L" in held or "Control_R" in held: buttons |= P.BTN_FIRE
        if "space" in held:                            buttons |= P.BTN_USE
        if "Return" in held:                           buttons |= P.BTN_ENTER
        if "Escape" in held:                           buttons |= P.BTN_ESCAPE
        if "Shift_L" in held or "Shift_R" in held:     buttons |= P.BTN_RUN
        if "Tab" in held:                              buttons |= P.BTN_MAP
        if "y" in held or "Y" in held:                 buttons |= P.BTN_YES
        return buttons, ax, ay, az

    def send_input(self):
        if not self.connected:
            return
        self.seq += 1
        buttons, ax, ay, az = self.buttons_and_tilt()
        self.sock.sendto(P.pack_input(self.session, self.seq, buttons, ax, ay,
                                      az, self.last_frame_id), self.addr)

    def handle_packet(self, data):
        ptype = P.packet_type(data)
        if ptype is None:
            return

        if ptype == P.PKT_HELLO_ACK:
            ack = P.parse_hello_ack(data)
            if not ack:
                return
            if ack["status"] != P.STATUS_OK:
                self.stat_text = "rejected: %s" % P.STATUS_TEXT.get(ack["status"])
                self.connected = False
                return
            self.session = ack["session"]
            if (ack["width"], ack["height"]) != (self.width, self.height):
                self.width, self.height = ack["width"], ack["height"]
                self.canvas_img = Image.new("RGB", (self.width, self.height))
            self.connected = True
            self.last_rx = time.monotonic()
            print("connected: session %d, %dx%d, tile %d" % (
                self.session, self.width, self.height, ack["tile"]))

        elif ptype == P.PKT_FRAME:
            if P.packet_session(data) != self.session:
                return
            frag = P.parse_frame(data)
            if frag:
                self.on_fragment(frag)

        elif ptype == P.PKT_PONG:
            t_client = struct.unpack_from("<I", data, 4)[0]
            self.rtt_ms = (int(time.monotonic() * 1000) - t_client) & 0xFFFFFFFF

        elif ptype == P.PKT_KICK:
            self.connected = False
            self.stat_text = "kicked by server"

    def on_fragment(self, frag):
        fid = frag["frame_id"]
        if self.asm_id is None or fid != self.asm_id:
            if self.asm_id is not None and len(self.asm) < self.asm_count:
                self.incomplete += 1
            self.asm_id, self.asm, self.asm_count = fid, {}, frag["frag_count"]

        self.asm[frag["frag_idx"]] = frag["payload"]
        if len(self.asm) < self.asm_count:
            return

        payload = b"".join(self.asm[i] for i in range(self.asm_count))
        self.asm_id, self.asm = None, {}
        self.apply_update(fid, payload)

    # --- drawing ----------------------------------------------------------

    def apply_update(self, frame_id, payload):
        rects, state = P.parse_update(payload)
        self.state = state
        self.last_frame_id = frame_id
        self.last_rx = time.monotonic()
        self.bytes += len(payload)

        if not rects:
            return                       # keepalive: the screen is unchanged

        for x, y, w, h, enc, data in rects:
            try:
                self.draw_rect(x, y, w, h, enc, data)
            except Exception as exc:                       # noqa: BLE001
                print("rect decode failed (%s): %s" % (P.ENC_NAMES.get(enc), exc))
                continue
            self.enc_counts[enc] = self.enc_counts.get(enc, 0) + 1

        self.frames += 1
        self.rects += len(rects)

        scale = self.args.scale
        big = self.canvas_img.resize((self.width * scale, self.height * scale),
                                     Image.NEAREST)
        self.photo = ImageTk.PhotoImage(big)
        self.label.configure(image=self.photo)

    def draw_rect(self, x, y, w, h, enc, data):
        if enc == P.ENC_SOLID:
            v = (data[0] << 8) | data[1]
            color = (((v >> 11) & 0x1F) << 3, ((v >> 5) & 0x3F) << 2, (v & 0x1F) << 3)
            self.canvas_img.paste(color, (x, y, x + w, y + h))
        elif enc == P.ENC_RAW565:
            self.canvas_img.paste(Image.fromarray(rgb565_to_array(data, w, h)), (x, y))
        elif enc == P.ENC_DEFLATE565:
            raw = zlib.decompress(data)
            self.canvas_img.paste(Image.fromarray(rgb565_to_array(raw, w, h)), (x, y))
        elif enc == P.ENC_JPEG:
            img = Image.open(io.BytesIO(data))
            img.load()
            self.canvas_img.paste(img, (x, y))

    # --- loop -------------------------------------------------------------

    def pump(self):
        now = time.monotonic()

        while True:
            try:
                data, _ = self.sock.recvfrom(4096)
            except (BlockingIOError, ConnectionResetError):
                break
            self.handle_packet(data)

        if not self.connected and now - self.last_hello > 0.5:
            self.send_hello()
        if self.connected and self.last_rx and now - self.last_rx > 2.0:
            self.connected = False
            self.stat_text = "lost the server, reconnecting..."

        self.send_input()

        if self.connected and now - self.last_ping > 1.0:
            self.last_ping = now
            self.sock.sendto(P.pack_ping(self.session,
                                         int(now * 1000) & 0xFFFFFFFF), self.addr)

        elapsed = now - self.t0
        if elapsed >= 1.0:
            if self.connected:
                mix = " ".join("%s:%d" % (P.ENC_NAMES.get(k, k), v)
                               for k, v in sorted(self.enc_counts.items()))
                self.stat_text = (
                    "%5.1f fps  %6.1f kB/s  %4.1f rects  rtt %3d ms  "
                    "state %d  %s" % (
                        self.frames / elapsed, self.bytes / elapsed / 1024.0,
                        self.rects / max(1, self.frames), self.rtt_ms,
                        self.state, mix))
            self.t0 = now
            self.frames = self.bytes = self.rects = 0
            self.enc_counts = {}

        self.status.configure(text=self.stat_text)
        self.root.after(self.args.input_ms, self.pump)

    def quit(self):
        try:
            if self.connected:
                self.sock.sendto(P.pack_bye(self.session), self.addr)
        except OSError:
            pass
        self.root.destroy()

    def run(self):
        self.send_hello()
        self.root.after(10, self.pump)
        self.root.mainloop()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=20002)
    ap.add_argument("--token", default="")
    ap.add_argument("--width", type=int, default=128)
    ap.add_argument("--height", type=int, default=128)
    ap.add_argument("--scale", type=int, default=4, help="window magnification")
    ap.add_argument("--mode", choices=("tilt", "buttons"), default="buttons",
                    help="tilt fakes accelerometer readings; buttons sends D-pad bits")
    ap.add_argument("--tilt-turn-axis", type=int, default=0, choices=(0, 1, 2),
                    help="accel axis Left/Right tilts, as mounted on the device (default x)")
    ap.add_argument("--tilt-move-axis", type=int, default=1, choices=(0, 1, 2),
                    help="accel axis Up/Down tilts (default y)")
    ap.add_argument("--input-ms", type=int, default=25)
    ap.add_argument("--no-jpeg", action="store_true",
                    help="advertise no JPEG support, as a device without it would")
    ap.add_argument("--no-deflate", action="store_true",
                    help="advertise no zlib support (the open question for the AtomS3R)")
    args = ap.parse_args()

    Viewer(args).run()


if __name__ == "__main__":
    main()
