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

import collections
import os
import threading
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

    #: How far ahead of the clock the decoder keeps frames ready. Decoding is
    #: usually 0.3 ms a frame, but measured it occasionally stalls for ~180 ms;
    #: done inline that froze the whole service loop and showed on the device
    #: as the picture stopping. Half a second of buffer swallows those.
    BUFFER_S = 0.5

    #: The service sends ~20 fps; decoding a 60 fps file frame by frame only
    #: to throw two thirds away is wasted work. Frames between are grabbed
    #: (demuxed, not converted to pixels).
    MAX_OUT_FPS = 30

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

        self._queue = collections.deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._finished = False

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
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, int(self.start_s * self.fps))
        self._thread = threading.Thread(target=self._decode, name="video-decode",
                                        daemon=True)
        self._thread.start()

    # --- decoder thread ---------------------------------------------------

    def _decode(self):
        step = max(1, int(round(self.fps / self.MAX_OUT_FPS)))
        interval = step / self.fps
        due = time.monotonic()               # wall-clock time of the next frame

        while not self._stop.is_set():
            now = time.monotonic()
            with self._lock:
                ahead = (self._queue[-1][0] - now) if self._queue else -1.0
            if ahead > self.BUFFER_S:
                time.sleep(0.01)
                continue

            # Fallen behind (a long stall, a busy machine): skip forward without
            # decoding pixels rather than playing the backlog in slow motion.
            skip = step - 1
            if due < now - 0.25:
                late = int((now - due) / interval)
                skip += late * step
                due += late * interval

            ok = True
            for _ in range(skip):
                if not self.cap.grab():
                    ok = False
                    break
            if ok:
                ok, frame = self.cap.read()
            if not ok:
                if not self.loop:
                    break
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, int(self.start_s * self.fps))
                continue

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            with self._lock:
                self._queue.append((due, rgb))
            due += interval

        self._finished = True

    # --- service side -----------------------------------------------------

    def poll(self):
        now = time.monotonic()
        newest = None
        with self._lock:
            while self._queue and self._queue[0][0] <= now:
                newest = self._queue.popleft()[1]
        if newest is None:
            return None
        return newest, P.STATE_LEVEL

    def send_key(self, pressed, key):
        pass                             # a recording has no controls

    def alive(self):
        if not self._finished:
            return True
        with self._lock:
            return bool(self._queue)

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self.cap is not None:
            self.cap.release()
            self.cap = None
