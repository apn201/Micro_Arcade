#!/usr/bin/env python3
"""The video engine, tested against a clip this test writes itself.

No Skyrim needed: a short synthetic video is enough to check real-time pacing,
looping, the square centre crop and that the kiosk can build the engine. Skips
cleanly when OpenCV is not installed, because video is an optional extra.

    python service/tests/test_video.py
"""

import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, SERVICE)

import numpy as np

from microstream.sources import video as V

FPS = 20
FRAMES = 30                   # 1.5 s of video


def make_clip(folder):
    """A 320x180 clip whose brightness ramps frame by frame."""
    import cv2
    for name, fourcc in (("clip.mp4", "mp4v"), ("clip.avi", "MJPG")):
        path = os.path.join(folder, name)
        out = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*fourcc), FPS, (320, 180))
        if not out.isOpened():
            continue
        for i in range(FRAMES):
            frame = np.full((180, 320, 3), 20 + i * 7, dtype=np.uint8)
            out.write(frame)
        out.release()
        if os.path.getsize(path) > 0:
            return path
    raise RuntimeError("OpenCV could not write a test clip")


def poll_for(src, seconds):
    """Every frame the source hands out in `seconds`, with its timestamp."""
    got = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        r = src.poll()
        if r is not None:
            got.append((time.monotonic(), r[0]))
        time.sleep(0.01)
    return got


def test_video():
    if not V.available():
        print("SKIP  OpenCV not installed; video titles are optional")
        return

    with tempfile.TemporaryDirectory() as tmp:
        path = make_clip(tmp)

        src = V.VideoSource(path)
        assert (src.width, src.height) == (320, 180)
        # 16:9 is cropped to its middle square for the square screen.
        assert src.default_crop == (70, 0, 180, 180), src.default_crop

        src.start()
        t0 = time.monotonic()
        frames = poll_for(src, 0.6)
        assert frames, "no frames at all"
        first = frames[0][1]
        assert first.shape == (180, 320, 3), first.shape

        # Real time, not as fast as it can decode: ~0.6 s at 20 fps is ~12
        # frames' worth of video, and brightness should have advanced about
        # that far rather than to the end of the clip.
        level = float(frames[-1][1].mean())
        expect = 20 + 0.6 * FPS * 7
        assert abs(level - expect) < 40, (level, expect)
        assert len(frames) <= FPS, "delivered faster than real time"

        # Past the end it loops back to the start instead of dying.
        late = poll_for(src, 1.4)
        assert src.alive()
        assert any(t - t0 > 1.6 for t, _ in late), "stopped at the end of the clip"
        assert min(float(f.mean()) for _, f in late) < 60, "never looped to the start"
        src.stop()

        # Without looping it ends, and says so.
        once = V.VideoSource(path, loop=False)
        once.start()
        poll_for(once, 1.9)
        assert not once.alive()
        once.stop()

        # The kiosk builds it from a library entry.
        from microstream.sources.kiosk import default_build_source
        source, profile = default_build_source(
            {"id": "clip", "title": "Clip", "engine": "video", "file_path": path}, {})
        assert isinstance(source, V.VideoSource)
        source.stop()

        try:
            V.VideoSource(os.path.join(tmp, "missing.mp4"))
        except RuntimeError:
            pass
        else:
            raise AssertionError("a missing file should fail loudly")

    print("PASS  video: square crop, real-time pacing, loop, one-shot end, kiosk engine")


if __name__ == "__main__":
    test_video()
