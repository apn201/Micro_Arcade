#!/usr/bin/env python3
"""Turn an eXoDOS entry into a .jsdos bundle.

eXoDOS stores each game as `<Title (Year)>.zip` containing one top-level
folder, and keeps the launch command in `!dos/<folder>/dosbox.conf`. This
reads both, so you do not have to work out what to run for each title.

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

DEFAULT_ROOT = r"D:\eXoDOS"
HERE = os.path.dirname(os.path.abspath(__file__))


def exodos_paths(root):
    games = os.path.join(root, "eXo", "eXoDOS")
    return games, os.path.join(games, "!dos")


def launch_command(dos_dir, folder):
    """(start command, subdirectory) as eXoDOS runs it, from its dosbox.conf.

    The subdirectory matters: several titles keep the game one level down and
    are started with `cd viz` first. Running from the archive root instead
    gets "Illegal command" or silently missing data files.
    """
    conf = os.path.join(dos_dir, folder, "dosbox.conf")
    if not os.path.exists(conf):
        return None, None
    with open(conf, "r", errors="replace") as fh:
        text = fh.read()
    idx = text.lower().find("[autoexec]")
    if idx < 0:
        return None, None

    cmd = None
    subdir = None
    for line in text[idx:].splitlines()[1:]:
        line = line.strip().lstrip("@").strip()
        low = line.lower()
        if low.startswith("cd "):
            target = line[3:].strip().strip("\\/")
            # `cd ..` only walks back out to the mount point.
            if target and target != "..":
                subdir = target
            continue
        if not line or low.startswith(("mount", "cls", "c:", "exit",
                                       "echo", "rem", "imgmount", "[")):
            continue
        if low.startswith("call "):
            line = line[5:].strip()
        cmd = line
        break

    return cmd, subdir


def resolve_extension(cmd, game_dir):
    """`call run` means RUN.BAT or RUN.EXE -- whichever is actually there.

    Guessing from the !dos folder is wrong: eXoDOS keeps the launcher scripts
    there and the game's own files in the zip, so the check has to happen
    against the extracted game.
    """
    if not cmd or re.search(r"\.(exe|com|bat)$", cmd, re.I):
        return cmd
    existing = {f.lower(): f for f in os.listdir(game_dir)}
    for ext in (".bat", ".exe", ".com"):
        hit = existing.get(cmd.lower() + ext)
        if hit:
            return hit
    return cmd + ".EXE"


def bundle(root, title, out, keep_temp=False):
    games_dir, dos_dir = exodos_paths(root)
    zip_path = os.path.join(games_dir, title + ".zip")
    if not os.path.exists(zip_path):
        sys.exit("no such game: %s" % zip_path)

    tmp = tempfile.mkdtemp(prefix="exodos_")
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
            top = names[0].split("/")[0]
            print("extracting %s (%d entries, top folder %s)"
                  % (os.path.basename(zip_path), len(names), top))
            z.extractall(tmp)

        game_dir = os.path.join(tmp, top)
        if not os.path.isdir(game_dir):
            sys.exit("unexpected archive layout: no folder %r" % top)

        cmd, subdir = launch_command(dos_dir, top)
        run_dir = os.path.join(game_dir, subdir) if subdir else game_dir
        if not os.path.isdir(run_dir):
            run_dir = game_dir
            subdir = None
        cmd = resolve_extension(cmd, run_dir)
        if cmd:
            print("launch command from dosbox.conf: %s%s"
                  % (subdir + "\\" if subdir else "", cmd))
        else:
            print("no dosbox.conf launch line; make-bundle will guess")

        # A CD image needs imgmount and a different autoexec; bail loudly
        # rather than produce a bundle that boots to a DOS prompt.
        if any(f.lower().endswith((".iso", ".cue", ".bin"))
               for f in os.listdir(run_dir)) or os.path.isdir(os.path.join(run_dir, "cd")):
            print("!! this title carries a CD image; js-dos needs imgmount "
                  "support that this script does not generate yet")

        argv = ["node", os.path.join(HERE, "make-bundle.js"), game_dir, out]
        if cmd:
            argv.append(cmd)
        if subdir:
            argv += ["--cd", subdir]
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
    ap.add_argument("--root", default=os.environ.get("EXODOS_ROOT", DEFAULT_ROOT))
    ap.add_argument("--out", default="")
    ap.add_argument("--list", dest="pattern", default="",
                    help="search the collection instead of bundling")
    ap.add_argument("--keep-temp", action="store_true")
    args = ap.parse_args()

    games_dir, _ = exodos_paths(args.root)
    if not os.path.isdir(games_dir):
        sys.exit("no eXoDOS collection at %s (set --root or EXODOS_ROOT)" % args.root)

    if args.pattern:
        pat = args.pattern.lower()
        hits = sorted(f[:-4] for f in os.listdir(games_dir)
                      if f.lower().endswith(".zip") and pat in f.lower())
        for h in hits[:40]:
            size = os.path.getsize(os.path.join(games_dir, h + ".zip")) / 1048576.0
            print("%7.1f MB  %s" % (size, h))
        print("%d match%s" % (len(hits), "" if len(hits) == 1 else "es"))
        return

    if not args.title:
        ap.error("give a title, or --list PATTERN")

    out = args.out or os.path.join(
        HERE, re.sub(r"[^A-Za-z0-9]+", "-", args.title).strip("-").lower() + ".jsdos")
    bundle(args.root, args.title, out, keep_temp=args.keep_temp)


if __name__ == "__main__":
    main()
