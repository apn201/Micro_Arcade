"""A video file as a frame source.

Not a game and not emulated: a recording, played in real time and streamed
through exactly the same rectangle pipeline as everything else. The terminal
cannot tell the difference, which is the whole premise of the cabinet -- so
anything that can be rendered somewhere else can be on this screen, including
things no emulator will ever run.

Decoding uses OpenCV, which bundles its own FFmpeg, so no system codec install
is needed. It is optional: without it the kiosk simply leaves video titles out
of the menu.

    pip install opencv-python-headless
"""

import os
import time

from .. import protocol as P
from .base import Source

try:
    import cv2
except ImportError:                      # the rest of the service works without it
    cv2 = None


def available():
    return cv2 is not None


class VideoSource(Source):
    name = "video"
    pixel_aspect = 1.0

    #: Beyond this far behind real time, seek instead of decoding every frame
    #: in between -- after a stall, skipping ahead is what "live" means.
    MAX_CATCHUP_S = 2.0

    def __init__(self, path, loop=True, start_s=0.0, crop_aspect=1.0):
        if cv2 is None:
            raise RuntimeError("video needs OpenCV: pip install opencv-python-headless")
        if not path or not os.path.exists(path):
            raise RuntimeError("no such video: %s" % path)

        self.path = path
        self.loop = loop
        self.start_s = float(start_s or 0.0)
        self.crop_aspect = crop_aspect
        self.cap = None
        self.default_crop = None
        self._t0 = 0.0
        self._index = 0
        self._ended = False

        # Open now rather than in start(): the kiosk reads this source's shape
        # to rebuild the scaler, and it should be right from the first frame.
        self._open()

    def _open(self):
        cap = cv2.VideoCapture(self.path)
        if not cap.isOpened():
            raise RuntimeError("cannot open video: %s" % self.path)
        self.cap = cap
        self.fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        # The screen is square and the server fills it, so a wide video would
        # be squeezed. Crop it instead: the middle square, undistorted.
        if self.crop_aspect and self.width > self.height * self.crop_aspect + 1:
            cw = int(round(self.height * self.crop_aspect))
            self.default_crop = ((self.width - cw) // 2, 0, cw, self.height)

    def start(self):
        self._seek(self.start_s)

    def _seek(self, seconds):
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, int(seconds * self.fps))
        self._index = int(seconds * self.fps)
        self._t0 = time.monotonic() - seconds

    def poll(self):
        if self._ended:
            return None

        due = int((time.monotonic() - self._t0) * self.fps)
        if due <= self._index:
            return None                  # not time for the next frame yet

        if due - self._index > self.MAX_CATCHUP_S * self.fps:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, due - 1)
            self._index = due - 1

        # Frames we are late for are grabbed but never decoded to pixels; only
        # the newest one pays for colour conversion.
        while self._index < due - 1:
            if not self.cap.grab():
                return self._end()
            self._index += 1

        ok, frame = self.cap.read()
        if not ok:
            return self._end()
        self._index += 1
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), P.STATE_LEVEL

    def _end(self):
        if self.loop:
            self._seek(self.start_s)
        else:
            self._ended = True
        return None

    def send_key(self, pressed, key):
        pass                             # a recording has no controls

    def alive(self):
        return not self._ended

    def stop(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None
