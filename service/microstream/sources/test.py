"""Test pattern and tilt calibration screen.

Two jobs, neither of which needs a game:

1. Prove the whole loop (device -> input -> encode -> screen) without DOOM in
   the way, which is the fastest way to tell a firmware bug from a game bug.
2. Calibrate the tilt controls. It draws the live accelerometer values onto the
   streamed image, so you hold the device, tilt it, and read straight off its
   own screen which axis does what and how far you have to lean. Feed what you
   find to the service as --tilt-turn / --tilt-move / --deadzone.

Unlike DOOM it reacts to the *raw* terminal state rather than to mapped keys,
because when you are calibrating, the mapping is exactly what you do not trust
yet.
"""

import time

import numpy as np
from PIL import Image, ImageDraw

from .. import protocol as P
from .base import Source

AXIS_NAMES = ("x", "y", "z")


class TestSource(Source):
    name = "test"
    pixel_aspect = 1.0
    default_crop = None

    def __init__(self, width=128, height=128, fps=30, deadzone=180, run=420,
                 turn_axis=1, move_axis=0):
        self.width = width
        self.height = height
        self.period = 1.0 / fps
        self.deadzone = deadzone
        self.run = run
        self.turn_axis = turn_axis
        self.move_axis = move_axis

        self.buttons = 0
        self.accel = (0, 0, 0)
        self.heading = 0.0
        self.walk = 0.0
        self.flash = 0
        self._next = 0.0
        self._last = time.monotonic()

    def start(self):
        self._next = time.monotonic()

    def set_raw_input(self, buttons, accel):
        self.buttons = buttons
        self.accel = accel

    def send_key(self, pressed, key):
        pass                      # calibration reads raw input, not key codes

    def poll(self):
        now = time.monotonic()
        if now < self._next:
            return None
        self._next = now + self.period

        dt = min(0.2, now - self._last)
        self._last = now
        self._step(dt)
        return np.asarray(self._render(), dtype=np.uint8), P.STATE_LEVEL

    # --- motion -----------------------------------------------------------

    def _tilt(self):
        return self.accel[self.turn_axis], self.accel[self.move_axis]

    def _step(self, dt):
        turn, move = self._tilt()
        dz = self.deadzone

        if self.buttons & P.BTN_LEFT:  turn = -dz * 2
        if self.buttons & P.BTN_RIGHT: turn = dz * 2
        if self.buttons & P.BTN_UP:    move = dz * 2
        if self.buttons & P.BTN_DOWN:  move = -dz * 2

        if abs(turn) > dz:
            self.heading += (1 if turn > 0 else -1) * 90.0 * dt * (2.5 if abs(turn) > self.run else 1.0)
        if abs(move) > dz:
            self.walk += (1 if move > 0 else -1) * 60.0 * dt * (2.5 if abs(move) > self.run else 1.0)
        self.heading %= 360.0

        if self.buttons & P.BTN_FIRE:
            self.flash = 3
        elif self.flash:
            self.flash -= 1

    # --- drawing ----------------------------------------------------------

    def _render(self):
        w, h = self.width, self.height
        img = Image.new("RGB", (w, h), (0, 0, 0))
        d = ImageDraw.Draw(img)
        horizon = h // 2

        for y in range(horizon):
            v = int(30 + 40 * (y / max(1, horizon)))
            d.line([(0, y), (w, y)], fill=(v // 2, v // 2, v // 3))
        for y in range(horizon, h):
            v = int(70 - 40 * ((y - horizon) / max(1, h - horizon)))
            d.line([(0, y), (w, y)], fill=(v // 2, v // 3, v // 4))

        # Posts that slide past as you turn or walk: enough motion to judge
        # latency and judder honestly.
        for i in range(-2, 8):
            depth = max(4.0, (i * 40 - self.walk) % 320)
            scale = 900.0 / depth
            x = w / 2 + (i * 40 - self.heading * 1.6) % 320 - 160
            half = max(2, int(scale * 0.18))
            shade = max(40, min(200, int(18000 / depth)))
            if -half < x < w + half:
                d.rectangle([x - half, int(horizon - scale * 0.35),
                             x + half, int(horizon + scale * 0.35)],
                            fill=(shade, shade // 3, shade // 4),
                            outline=(shade // 2, shade // 6, shade // 8))

        if self.flash:
            d.rectangle([0, 0, w, h], fill=(255, 230, 150))

        cx = w // 2
        d.line([(cx - 4, horizon), (cx + 4, horizon)], fill=(255, 255, 255))
        d.line([(cx, horizon - 4), (cx, horizon + 4)], fill=(255, 255, 255))

        # --- the calibration overlay ---
        d.rectangle([0, h - 40, w, h], fill=(0, 0, 0))
        for i, raw in enumerate(self.accel):
            y = h - 38 + i * 9
            bar = max(-60, min(60, int(raw / 16)))
            d.text((2, y - 1), "%s%+5d" % (AXIS_NAMES[i], raw), fill=(190, 190, 190))
            d.line([(64, y + 3), (64 + bar, y + 3)], fill=(90, 220, 90))
            d.line([(64, y), (64, y + 6)], fill=(120, 120, 120))

        turn, move = self._tilt()
        status = ""
        if abs(turn) > self.deadzone:
            status += "RIGHT " if turn > 0 else "LEFT "
        if abs(move) > self.deadzone:
            status += "FWD " if move > 0 else "BACK "
        if self.buttons & P.BTN_FIRE:
            status += "FIRE"
        d.text((2, h - 11), status[:22] or "level", fill=(220, 200, 80))

        return img
