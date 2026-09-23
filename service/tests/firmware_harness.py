"""Run the real firmware (client/main.py) on the PC, against the real service.

M5GFX, the IMU, the button and MicroPython's `time.ticks_*` are stubbed. What
is *not* stubbed is any of the firmware's own code: the protocol parsing,
struct offsets, fragment reassembly and rect dispatch all run exactly as they
will on the device. Draw calls composite into a PIL image, so if DOOM comes out
the other side, the decode path is correct.

This turns firmware work into an edit-and-run loop instead of an
edit-flash-squint loop. What it cannot tell you is whether the real M5GFX
accepts the arguments being passed -- run client/caps_test.py on the device for
that, once.

    python service/tests/firmware_harness.py

Environment: MD_HARNESS_PORT, MD_HARNESS_SECONDS, MD_HARNESS_OUT.
"""
import io
import os
import subprocess
import sys
import threading
import time
import types
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
PORT = int(os.environ.get("MD_HARNESS_PORT", "20094"))
SECONDS = float(os.environ.get("MD_HARNESS_SECONDS", "8"))
OUT = os.environ.get("MD_HARNESS_OUT", os.path.join(HERE, "firmware_canvas.png"))

import numpy as np
from PIL import Image

# --- MicroPython time shims (the firmware imports the same module object) ---
time.ticks_ms = lambda: int(time.monotonic() * 1000) & 0x3FFFFFFF
time.ticks_us = lambda: int(time.monotonic() * 1000000) & 0x3FFFFFFF
time.ticks_diff = lambda a, b: a - b
time.ticks_add = lambda a, b: a + b
time.sleep_ms = lambda ms: time.sleep(ms / 1000.0)


def rgb565_to_img(buf, w, h):
    v = np.frombuffer(bytes(buf), dtype=">u2")[: w * h].reshape(h, w)
    out = np.empty((h, w, 3), dtype=np.uint8)
    out[:, :, 0] = ((v >> 11) & 0x1F) << 3
    out[:, :, 1] = ((v >> 5) & 0x3F) << 2
    out[:, :, 2] = (v & 0x1F) << 3
    return Image.fromarray(out)


class Display:
    def __init__(self):
        self.canvas = Image.new("RGB", (128, 128), (0, 0, 0))
        self.calls = {"jpeg": 0, "raw": 0, "solid": 0, "text": 0}
        self.errors = []

    # geometry / text
    def setRotation(self, r): pass
    def setTextSize(self, s): pass
    def setCursor(self, x, y): pass
    def print(self, text, color=0xFFFFFF): self.calls["text"] += 1
    def fillScreen(self, color):
        self.canvas.paste((color >> 16 & 0xFF, color >> 8 & 0xFF, color & 0xFF),
                          (0, 0, 128, 128))
    def startWrite(self): pass
    def endWrite(self): pass

    # the three paths the protocol uses
    def drawJpg(self, buf, x, y, *a):
        img = Image.open(io.BytesIO(bytes(buf)))
        img.load()
        self.canvas.paste(img, (x, y))
        self.calls["jpeg"] += 1

    def drawRawBuf(self, buf, x, y, w, h, n, swap):
        if len(bytes(buf)) < w * h * 2:
            self.errors.append("drawRawBuf short buffer %d < %d" % (len(bytes(buf)), w * h * 2))
            return
        self.canvas.paste(rgb565_to_img(buf, w, h), (x, y))
        self.calls["raw"] += 1

    def fillRect(self, x, y, w, h, rgb):
        self.canvas.paste((rgb >> 16 & 0xFF, rgb >> 8 & 0xFF, rgb & 0xFF),
                          (x, y, x + w, y + h))
        self.calls["solid"] += 1


class Btn:
    def __init__(self): self.pressed = False
    def isPressed(self): return self.pressed
    def isHolding(self): return self.pressed


class Imu:
    accel = (-0.05, 0.02, 0.98)      # the resting values measured on the device
    @classmethod
    def getAccel(cls): return cls.accel


m5 = types.ModuleType("M5")
m5.Display = Display()
m5.begin = lambda: None
m5.update = lambda: None
m5.BtnA = Btn()
m5.Imu = Imu
sys.modules["M5"] = m5

# `from M5 import *` needs these as module attributes, which they are.
net = types.ModuleType("network")
net.STA_IF = 0
net.AP_IF = 1


class WLAN:
    def __init__(self, mode): pass
    def active(self, on=True): return True
    def isconnected(self): return True
    def connect(self, *a): pass
    def ifconfig(self): return ("127.0.0.1", "255.255.255.0", "127.0.0.1", "127.0.0.1")


net.WLAN = WLAN
sys.modules["network"] = net

# --- socket shim: force a particular receive call -------------------------
# UIFlow2's socket has no recv_into(); other builds have it, and some only
# offer the stream readinto(). The firmware probes for whichever exists, so
# this lets all three paths be exercised here rather than on the device.
RECV_MODE = os.environ.get("MD_HARNESS_RECV", "recv_into")

import socket as _socket

_real_socket = _socket.socket


class RestrictedSocket:
    """A socket exposing only the receive call named by RECV_MODE."""

    def __init__(self, *a, **kw):
        self._s = _real_socket(*a, **kw)

        if RECV_MODE == "recv_into":
            self.recv_into = self._s.recv_into
        elif RECV_MODE == "readinto":
            self.readinto = self._readinto
        elif RECV_MODE == "recv":
            self.recv = self._s.recv
        else:
            raise SystemExit("MD_HARNESS_RECV must be recv_into, readinto or recv")

    def _readinto(self, buf):
        # MicroPython's stream readinto returns None when nothing is waiting.
        try:
            return self._s.recv_into(buf)
        except BlockingIOError:
            return None

    def setsockopt(self, *a): return self._s.setsockopt(*a)
    def connect(self, addr): return self._s.connect(addr)
    def setblocking(self, flag): return self._s.setblocking(flag)
    def send(self, data): return self._s.send(data)
    def close(self): return self._s.close()


_socket.socket = RestrictedSocket

cfg = types.ModuleType("config")
cfg.WIFI_SSID = "test-ssid"
cfg.WIFI_PASS = ""
cfg.SERVER_HOST = "127.0.0.1"
cfg.SERVER_PORT = PORT
cfg.TOKEN = ""
cfg.ROTATION = 0
cfg.WIDTH = 128
cfg.HEIGHT = 128
cfg.INPUT_PERIOD_MS = 33
cfg.PIN_UP = cfg.PIN_DOWN = cfg.PIN_LEFT = cfg.PIN_RIGHT = None
cfg.PIN_FIRE = cfg.PIN_USE = cfg.PIN_PIEZO = None
cfg.PIEZO_FIRE_HZ = 220
cfg.PIEZO_FIRE_MS = 25
cfg.DEBUG = True
sys.modules["config"] = cfg


def main():
    svc = subprocess.Popen(
        [sys.executable, "-u", os.path.join(ROOT, "service", "run.py"),
         "--port", str(PORT), "--source", "doom"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(4)
    if svc.poll() is not None:
        print(svc.stdout.read())
        sys.exit("service died")

    sys.path.insert(0, os.path.join(ROOT, "client"))
    import main as firmware

    term = firmware.Terminal()
    print("firmware caps: 0x%02x  inflate: %s  receive mode forced: %s" % (
        term.caps, firmware.inflate_name, RECV_MODE))

    t = threading.Thread(target=term.run, daemon=True)
    t.start()

    # let it connect and stream, then walk forward so rects vary
    time.sleep(SECONDS / 2)
    m5.BtnA.pressed = True
    Imu.accel = (-0.05, -0.70, 0.69)        # tilt "forward", as measured
    time.sleep(SECONDS / 2)

    d = m5.Display
    print("draw calls:", d.calls)
    print("errors:", d.errors or "none")
    print("connected: %s  session: %d  last frame: %d  state: %d  recv: %s" % (
        term.connected, term.session, term.last_frame_id, term.state,
        term.recv_kind))
    d.canvas.save(OUT)
    print("saved %s" % OUT)

    svc.terminate()
    try:
        out, _ = svc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        svc.kill()
        out = ""
    keep = ("fps sent", "link:", "tilt neutral", "controls:", "keys ")
    tail = [l for l in out.splitlines() if any(k in l for k in keep)]
    print("--- service ---")
    for l in tail[-8:]:
        print(l)


if __name__ == "__main__":
    main()
