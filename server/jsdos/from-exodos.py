#!/usr/bin/env python3
"""Turn an eXoDOS entry into a .jsdos bundle.

The kiosk can run eXoDOS titles in place, straight from the collection, so a
bundle is optional: it is a self-contained copy for a machine without the
collection, or for a title you want to start a little faster.

eXoDOS stores each game as `<Title (Year)>.zip` containing one top-level
folder, and keeps the launch command in `!dos/<folder>/dosbox.conf`. The
shared reader in service/microstream/exodos.py works out what to run and mount
from both; this extracts the game and packs it.

    python server/jsdos/from-exodos.py "SkyRoads (1993)"
    python server/jsdos/from-exodos.py "Descent (1995)" --out descent.jsdos
    python server/jsdos/from-exodos.py --list stunt

Bundles land next to the js-dos backend and are gitignored: whatever your own
collection's licensing allows, it does not belong in a public repository.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "service"))

from microstream.exodos import Collection

DEFAULT_ROOT = os.environ.get("EXODOS_ROOT", r"D:\eXoDOS")


def bundle(collection, title, out, keep_temp=False):
    try:
        game = collection.title(title)
    except RuntimeError as exc:
        sys.exit(str(exc))

    tmp = tempfile.mkdtemp(prefix="exodos_")
    try:
        with zipfile.ZipFile(game.zip_path) as z:
            print("extracting %s (%d entries, top folder %s)"
                  % (os.path.basename(game.zip_path), len(game.names), game.top))
            z.extractall(tmp)

        game_dir = os.path.join(tmp, game.top)
        if not os.path.isdir(game_dir):
            sys.exit("unexpected archive layout: no folder %r" % game.top)

        if game.cmd:
            print("launch command from dosbox.conf: %s%s"
                  % (game.subdir.replace("/", "\\") + "\\" if game.subdir else "", game.cmd))
        else:
            print("no dosbox.conf launch line; make-bundle will guess")

        pre = [line for line in game.autoexec(in_place=False)[1:]
               if line.startswith(("mount ", "imgmount "))]
        for line in pre:
            print("extra mount: %s" % line)
        if game.drive != "c:":
            print("starts from drive %s" % game.drive.upper())

        argv = ["node", os.path.join(HERE, "make-bundle.js"), game_dir, out]
        if game.cmd:
            argv.append(game.cmd)
        if game.subdir:
            argv += ["--cd", game.subdir.replace("/", "\\")]
        for line in pre:
            argv += ["--pre", line]
        if game.drive != "c:":
            argv += ["--drive", game.drive]
        subprocess.check_call(argv)
    finally:
        if keep_temp:
            print("kept %s" % tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("title", nargs="?", help='e.g. "SkyRoads (1993)"')
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--out", default="")
    ap.add_argument("--list", dest="pattern", default="",
                    help="search the collection instead of bundling")
    ap.add_argument("--conf", action="store_true",
                    help="print the dosbox.conf the kiosk would use in place, and stop")
    ap.add_argument("--keep-temp", action="store_true")
    args = ap.parse_args()

    collection = Collection(args.root)
    if not collection.exists():
        sys.exit("no eXoDOS collection at %s (set --root or EXODOS_ROOT)" % args.root)

    if args.pattern:
        hits = collection.search(args.pattern)
        for h in hits[:40]:
            size = os.path.getsize(collection.zip_path(h)) / 1048576.0
            print("%7.1f MB  %s" % (size, h))
        print("%d match%s" % (len(hits), "" if len(hits) == 1 else "es"))
        return

    if not args.title:
        ap.error("give a title, or --list PATTERN")

    if args.conf:
        print(collection.title(args.title).dosbox_conf(in_place=True))
        return

    out = args.out or os.path.join(
        HERE, re.sub(r"[^A-Za-z0-9]+", "-", args.title).strip("-").lower() + ".jsdos")
    bundle(collection, args.title, out, keep_temp=args.keep_temp)


if __name__ == "__main__":
    main()
