#!/usr/bin/env python3
"""Build every bundle the kiosk library asks for.

The library names each title by its eXoDOS entry; this walks that list and
calls from-exodos.py for anything not already built, so setting up a fresh
machine is one command instead of twenty.

    python server/jsdos/build-library.py
    python server/jsdos/build-library.py --library kiosk/games.json
    python server/jsdos/build-library.py --force        # rebuild everything

Bundles are gitignored and stay on your machine: whatever your own
collection's licensing allows, it does not belong in a public repository.
"""

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))


def library_path(given):
    if given:
        return given
    for name in ("games.json", "games.example.json"):
        path = os.path.join(ROOT, "kiosk", name)
        if os.path.exists(path):
            return path
    return None


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--library", default="",
                    help="games.json to read (default: your games.json, else "
                         "games.example.json)")
    ap.add_argument("--root", default=os.environ.get("EXODOS_ROOT", r"D:\eXoDOS"))
    ap.add_argument("--force", action="store_true",
                    help="rebuild bundles that already exist")
    args = ap.parse_args()

    path = library_path(args.library)
    if not path:
        sys.exit("no library found: pass --library")
    with open(path, "r", encoding="utf-8") as fh:
        games = json.load(fh).get("games", [])

    wanted = [g for g in games if g.get("exodos")]
    print("%s: %d titles from eXoDOS\n" % (os.path.relpath(path, ROOT), len(wanted)))

    built = skipped = failed = 0
    for g in wanted:
        bundle = os.path.join(HERE, g.get("bundle", ""))
        if os.path.exists(bundle) and not args.force:
            print("have    %s" % g["title"])
            skipped += 1
            continue
        print("build   %s ..." % g["title"], end=" ", flush=True)
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "from-exodos.py"),
             g["exodos"], "--root", args.root],
            capture_output=True, text=True)
        if os.path.exists(bundle):
            print("ok (%.0f MB)" % (os.path.getsize(bundle) / 1e6))
            built += 1
        else:
            tail = (proc.stdout + proc.stderr).strip().splitlines()
            print("FAILED: %s" % (tail[-1] if tail else "no output"))
            failed += 1

    print("\n%d built, %d already present, %d failed" % (built, skipped, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
