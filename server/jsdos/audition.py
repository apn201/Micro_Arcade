#!/usr/bin/env python3
"""Audition DOS titles for the kiosk: can a script get you *into the game*?

The bar is not "does it boot". An arcade cabinet with a 128x128 screen and a
tilt sensor cannot navigate a DOS menu -- the text is unreadable and there is
no pointer -- so a title only earns a place in the library if a scripted key
sequence lands the player in gameplay: plane in the air, car on the grid. If it
cannot, it is skipped.

Motion alone cannot answer that. A car waiting on the start line does not
move, and an attract-mode demo moves beautifully -- so "the picture changed"
proves nothing in either direction. The question is causal: **does the screen
respond to input?**

So after the boot sequence this samples the screen twice: a quiet window with
no input, then a window while a movement key is held down. Gameplay reacts;
a menu and a demo do not.

    python server/jsdos/audition.py "Stunts (1990)"
    python server/jsdos/audition.py --plan plans.json --jobs 4

Verdicts:
    PLAY     the picture reacts to a held movement key -- you are in the game
    DEMO     lots of motion that ignores input: an attract loop
    STATIC   nothing moves and input changes nothing: a menu or title screen
    TEXT     still in a text mode; its prompt went unanswered
    DEAD     no frames at all
    HUNG     the emulator never settled

Every run writes <stem>_strip.png: quiet samples then held-key samples, so the
verdict can be checked by eye in one glance.
"""

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "service"))

import numpy as np
from PIL import Image

from microstream.keys import code as key_code

#: Mash through an eXoDOS sound-card launcher and whatever title screens and
#: default-highlighted menu items follow. Crude, but most DOS menus start on
#: the item you want, so Enter repeatedly is a decent generic path into a game.
GENERIC = ([(3500, "1")] +
           [(7000 + i * 2500, "enter") for i in range(10)])

RUN_FOR_MS = 46000

#: After the boot sequence: three quiet samples, then four while a movement key
#: is held. The gap between those two groups is the whole verdict. They hang off
#: the *end* of the run, so a title that needs a long menu sequence just gets a
#: longer run_for and the windows follow it.
QUIET_BACK = (13000, 11800, 10600)
REACT_BACK = (8000, 6800, 5600, 4400)
HOLD_BACK, HOLD_MS = 9500, 6000
#: Keys held during the reaction window, per genre, set in the plan. A single
#: direction gives false negatives -- a character can be stood against scenery,
#: a car can be facing a wall -- so a probe is a *list* and each key gets a
#: share of the window. Anything that answers the controls answers one of them.
DEFAULT_PROBE = ["right", "left", "up"]


def windows(run_for):
    """Sample times for a run of this length: (quiet, react, hold_at)."""
    return ([run_for - b for b in QUIET_BACK],
            [run_for - b for b in REACT_BACK],
            run_for - HOLD_BACK)

DIMS = {64000: (320, 200), 76800: (320, 240), 128000: (640, 200),
        192000: (640, 300), 256000: (640, 400), 307200: (640, 480)}

#: Mean absolute pixel change between samples that counts as "moving".
MOTION_THRESHOLD = 1.5


def stem_of(title):
    return re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-").lower()


def ensure_bundle(title, root):
    path = os.path.join(HERE, stem_of(title) + ".jsdos")
    if os.path.exists(path):
        return path, None
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "from-exodos.py"), title, "--root", root],
        capture_output=True, text=True, timeout=900)
    if not os.path.exists(path):
        tail = (proc.stdout + proc.stderr).strip().splitlines()
        return None, (tail[-1] if tail else "bundle failed")
    return path, None


def load_frame(path):
    if not os.path.exists(path):
        return None
    data = open(path, "rb").read()
    wh = DIMS.get(len(data) // 3)
    if not wh:
        return None
    return np.asarray(Image.frombytes("RGB", wh, data[:wh[0] * wh[1] * 3]))


def audition(title, root, keys, run_for, probe=None):
    path, err = ensure_bundle(title, root)
    if not path:
        return dict(title=title, verdict="DEAD", detail="bundle: %s" % err)

    stem = os.path.splitext(os.path.basename(path))[0]
    raw = os.path.join(HERE, stem + ".rgb")

    # The boot sequence, then one long hold of a movement key.
    probe = probe or DEFAULT_PROBE
    if isinstance(probe, str):
        probe = [probe]

    quiet_at, react_at, hold_from = windows(run_for)
    spec_parts = ["%d:%d" % (at, key_code(k)) for at, k in keys]
    share = HOLD_MS // len(probe)
    for i, k in enumerate(probe):
        spec_parts.append("%d:%d:%d" % (hold_from + i * share, key_code(k), share - 150))
    spec = ",".join(spec_parts)
    shots = tuple(quiet_at) + tuple(react_at)

    try:
        subprocess.run(
            ["node", os.path.join(HERE, "probe.js"), path, raw,
             "--keys", spec, "--until", str(run_for),
             "--shots", ",".join(str(s) for s in shots)],
            cwd=HERE, capture_output=True, text=True,
            timeout=run_for / 1000.0 + 60)
    except subprocess.TimeoutExpired:
        return dict(title=title, verdict="HUNG", detail="emulator did not settle")

    frames = [load_frame(raw + "." + str(s)) for s in shots]
    frames = [f for f in frames if f is not None]
    final = load_frame(raw)
    if final is not None:
        frames.append(final)

    for f in [raw] + [raw + "." + str(s) for s in shots]:
        try:
            os.remove(f)
        except OSError:
            pass

    if not frames:
        return dict(title=title, verdict="DEAD", detail="no frames")

    last = frames[-1]
    h, w = last.shape[:2]

    def spread(group):
        group = [f for f in group if f is not None and f.shape == last.shape]
        if len(group) < 2:
            return 0.0
        return max(float(np.abs(group[i].astype(np.int16) -
                                group[i + 1].astype(np.int16)).mean())
                   for i in range(len(group) - 1))

    quiet = spread(frames[:len(quiet_at)])
    react = spread(frames[len(quiet_at):])

    if w >= 640:
        verdict = "TEXT"
    elif react >= MOTION_THRESHOLD and react >= quiet * 2.0:
        verdict = "PLAY"          # it answered the controls
    elif quiet >= MOTION_THRESHOLD:
        verdict = "DEMO"          # busy, but not because of us
    else:
        verdict = "STATIC"

    # The still is taken from the reaction window, not from whatever happened to
    # be on screen when the run ended -- by then a title may have walked into a
    # level card or a hint box, which is a poor advertisement for it.
    react_frames = [f for f in frames[len(quiet_at):] if f.shape == last.shape]
    shot = react_frames[len(react_frames) // 2] if react_frames else last
    Image.fromarray(shot).save(os.path.join(HERE, stem + ".png"))
    Image.fromarray(shot).resize((128, 128), Image.BOX).save(
        os.path.join(HERE, stem + "_128.png"))

    # A strip of the samples makes an attract-mode demo obvious at a glance.
    usable = [f for f in frames if f.shape == last.shape]
    if len(usable) > 1:
        strip = Image.new("RGB", (128 * len(usable), 128))
        for i, f in enumerate(usable):
            strip.paste(Image.fromarray(f).resize((128, 128), Image.BOX), (i * 128, 0))
        strip.save(os.path.join(HERE, stem + "_strip.png"))

    return dict(title=title, verdict=verdict, stem=stem, probe=probe,
                quiet=round(quiet, 2), react=round(react, 2),
                detail="%dx%d  quiet %.2f  held-key %.2f" % (w, h, quiet, react))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("titles", nargs="*")
    ap.add_argument("--plan", default="",
                    help='JSON: {title: {"keys": [[ms, key], ...], '
                         '"probe": ["up"], "run_for": 60000}}')
    ap.add_argument("--root", default=os.environ.get("EXODOS_ROOT", r"D:\eXoDOS"))
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--run-for", type=int, default=RUN_FOR_MS)
    ap.add_argument("--out", default="", help="write the verdicts as JSON")
    args = ap.parse_args()

    plan = {}
    if args.plan:
        with open(args.plan, "r", encoding="utf-8") as fh:
            plan = json.load(fh)

    titles = list(args.titles) + [t for t in plan if t not in args.titles]
    if not titles:
        ap.error("give titles, or --plan")

    print("auditioning %d titles, %d at a time\n" % (len(titles), args.jobs))
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {}
        for t in titles:
            entry = plan.get(t)
            if not isinstance(entry, dict):
                entry = {"keys": entry or GENERIC}
            keys = [tuple(k[:2]) for k in entry.get("keys", GENERIC)]
            run_for = entry.get("run_for", args.run_for)
            probe = entry.get("probe", DEFAULT_PROBE)
            futures[pool.submit(audition, t, args.root, keys, run_for, probe)] = t
        for fut in concurrent.futures.as_completed(futures):
            r = fut.result()
            results.append(r)
            print("%-6s %-46s %s" % (r["verdict"], r["title"][:46], r.get("detail", "")))

    print()
    for want in ("PLAY", "DEMO", "STATIC", "TEXT", "HUNG", "DEAD"):
        n = [r["title"] for r in results if r["verdict"] == want]
        if n:
            print("%s (%d): %s" % (want, len(n), ", ".join(t[:28] for t in n)))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2)


if __name__ == "__main__":
    main()
