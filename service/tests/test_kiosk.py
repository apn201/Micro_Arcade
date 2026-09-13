#!/usr/bin/env python3
"""The kiosk, driven exactly as the device drives it: tilt and one button.

There is no menu in the firmware and none in the protocol -- the menu is a
source that draws a list, so this test exercises the real thing through the
real socket, using only the three gestures the cabinet actually has:

    tilt        move the selection
    click       select
    long press  back to the menu

Needs a playable library. `--doom` restricts it to the native DOOM entry,
which needs no bundle; by default it uses whatever kiosk/games*.json offers.

    python service/tests/test_kiosk.py
"""

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE = os.path.abspath(os.path.join(HERE, ".."))
ROOT = os.path.abspath(os.path.join(SERVICE, ".."))
sys.path.insert(0, SERVICE)
sys.path.insert(0, HERE)

import numpy as np

from microstream import protocol as P
import test_stream as T

PORT = 20078
failures = []


def check(name, ok, detail=""):
    print("%-46s %s%s" % (name, "PASS" if ok else "FAIL",
                          "  " + detail if detail else ""))
    if not ok:
        failures.append(name)


def hold(client, ms, buttons=0, accel=(0, 0, 1000)):
    T.drive(client, ms / 1000.0, buttons=buttons, accel=accel)


def frame_of(client):
    return np.asarray(client.canvas, dtype=np.int16)


def differs(a, b, threshold=2.0):
    return float(np.abs(a - b).mean()) > threshold


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--library", default="")
    ap.add_argument("--save", default="", help="directory for screenshots")
    args = ap.parse_args()

    extra = ["--source", "kiosk"]
    if args.library:
        extra += ["--library", args.library]

    T.PORT = PORT
    T.TOKEN = ""
    proc = subprocess.Popen(
        [sys.executable, "-u", os.path.join(SERVICE, "run.py"),
         "--port", str(PORT)] + extra,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(3.5)
    if proc.poll() is not None:
        print(proc.stdout.read())
        sys.exit("service exited during startup")

    saved = {}
    try:
        c = T.HeadlessClient(PORT, token="")
        hold(c, 2500)
        check("the menu appears without any firmware menu code", c.frames > 0,
              "%d updates" % c.frames)
        menu_first = frame_of(c)
        saved["menu"] = c.canvas.copy()

        # Tilt moves the selection, one step per repeat interval. Hold it just
        # long enough for a single step: with a two-entry library, holding for
        # several repeats cycles right back to where it started and the frame
        # is identical again.
        hold(c, 200, accel=(0, 700, 700))
        hold(c, 900)
        menu_moved = frame_of(c)
        check("tilt moves the selection", differs(menu_first, menu_moved),
              "menu redrew")
        saved["menu_moved"] = c.canvas.copy()

        # A click selects. Press and release well inside the long-press window.
        hold(c, 200, buttons=P.BTN_FIRE)
        hold(c, 300)
        c.reset_stats()
        hold(c, 9000)                      # DOSBox/DOOM needs time to boot
        playing = frame_of(c)
        check("a click launches the selected game",
              differs(menu_moved, playing, 6.0) and c.frames > 5,
              "%d updates while playing" % c.frames)
        saved["playing"] = c.canvas.copy()

        # A long press is the system gesture: it must work from inside a game
        # and must not be bindable by one.
        hold(c, 1400, buttons=P.BTN_FIRE)
        hold(c, 2500)
        back = frame_of(c)
        check("a long press returns to the menu",
              differs(playing, back, 6.0), "screen changed back")
        saved["back"] = c.canvas.copy()

        # And the menu must still be usable afterwards.
        c.reset_stats()
        hold(c, 1200, accel=(0, 700, 700))
        check("the menu still responds after a game", c.frames > 0,
              "%d updates" % c.frames)

    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out = ""

    if args.save:
        os.makedirs(args.save, exist_ok=True)
        for name, img in saved.items():
            img.save(os.path.join(args.save, "kiosk_%s.png" % name))
        print("wrote %d screenshots to %s" % (len(saved), args.save))

    for line in out.splitlines():
        if any(k in line for k in ("kiosk:", "library:", "geometry:", "jsdos:", "doom:")):
            print("   ", line)

    print()
    if failures:
        print("%d check(s) failed: %s" % (len(failures), ", ".join(failures)))
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
