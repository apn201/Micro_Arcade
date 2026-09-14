#!/usr/bin/env python3
"""Start the streaming service.

    python service/run.py                          # DOOM, defaults
    python service/run.py --source jsdos --bundle digger.jsdos
    python service/run.py --token hunter2 --fps 25
    python service/run.py --debug-input            # watch the tilt numbers

Every option also reads an MD_* environment variable (MD_PORT, MD_TOKEN,
MD_FPS, MD_TILT_TURN ...), which is how the systemd unit in deploy/ configures
it. Command-line arguments win over the environment.

Every control-feel knob lives here rather than in the device, so tuning is a
restart instead of a reflash.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from microstream.profiles import DoomProfile, KeymapProfile
from microstream.server import Config, StreamServer
from microstream.sources.doom import DoomSource

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def env(name, default, cast=str):
    """Let every option come from an MD_* environment variable too.

    The systemd unit configures the service this way: one KEY=value file is
    less error-prone to edit than a command line buried in a unit, and it keeps
    a token out of `ps`.
    """
    raw = os.environ.get("MD_" + name)
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        raise SystemExit("MD_%s: cannot parse %r" % (name, raw))


def env_flag(name, default=False):
    raw = os.environ.get("MD_" + name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def axis(spec):
    """'y' or 'y-' -> (index, inverted)"""
    name = spec[0].lower()
    if name not in "xyz":
        raise argparse.ArgumentTypeError("axis must be x, y or z")
    return "xyz".index(name), spec.endswith("-")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=env("SOURCE", "doom"),
                    choices=("kiosk", "doom", "test", "jsdos", "video"),
                    help="kiosk = the game library with a menu (default for a "
                         "cabinet), doom = native headless DOOM, jsdos = one "
                         "DOS bundle, video = one video file, test = "
                         "calibration pattern")

    net = ap.add_argument_group("network")
    net.add_argument("--bind", default=env("BIND", "0.0.0.0"))
    net.add_argument("--port", type=int, default=env("PORT", 20002, int))
    net.add_argument("--token", default=env("TOKEN", ""))
    net.add_argument("--frag", type=int, default=env("FRAG", 1400, int),
                     help="UDP payload bytes per fragment")
    net.add_argument("--keyframe-ms", type=int,
                     default=env("KEYFRAME_MS", 3000, int),
                     help="send a full frame at least this often")
    net.add_argument("--frag-gap", type=int, default=env("FRAG_GAP", 3, int),
                     help="ms between the fragments of one update; -1 sends "
                          "them all at once. The AtomS3R's socket holds a "
                          "single datagram, so a burst means only the first "
                          "one arrives and the update never completes")
    net.add_argument("--no-fit-datagram", dest="fit_datagram",
                     action="store_false",
                     help="do not trade JPEG quality to keep an update inside "
                          "a single datagram")
    net.add_argument("--ack-timeout", type=int,
                     default=env("ACK_TIMEOUT", 0, int),
                     help="ms to wait for an acknowledgement before sending "
                          "anyway; 0 adapts to the measured round trip. This "
                          "is how long the picture freezes after a lost update")
    net.add_argument("--window", type=int, default=env("WINDOW", 2, int),
                     help="updates allowed in flight before waiting for the "
                          "client to catch up (0 disables flow control)")

    vid = ap.add_argument_group("video")
    vid.add_argument("--size", default=env("SIZE", "128x128"))
    vid.add_argument("--fps", type=int, default=env("FPS", 20, int))
    vid.add_argument("--quality", type=int, default=env("QUALITY", 55, int),
                     help="JPEG quality")
    vid.add_argument("--chroma444", action="store_true",
                     default=env_flag("CHROMA444"),
                     help="4:4:4 JPEG: sharper HUD, ~30%% more bytes")
    vid.add_argument("--tile", type=int, default=env("TILE", 32, int),
                     help="dirty-rectangle granularity, 0 to always send the whole frame")
    vid.add_argument("--letterbox", action="store_true",
                     default=env_flag("LETTERBOX"),
                     help="preserve 4:3 aspect instead of filling the square")
    vid.add_argument("--brighten", type=float,
                     default=env("BRIGHTEN", 1.0, float))
    vid.add_argument("--no-crop", dest="crop", action="store_false",
                     help="keep DOOM's status bar in the streamed image")
    vid.add_argument("--select", choices=("cost", "bytes"),
                     default=env("SELECT", "cost"),
                     help="choose encodings by predicted device decode time "
                          "(default) or by payload size")
    vid.add_argument("--link-kbps", type=int,
                     default=env("LINK_KBPS", 4000, int),
                     help="assumed link speed, used to price bytes against "
                          "decode time (default 4000)")

    game = ap.add_argument_group("game")
    game.add_argument("--exe", default=None, help="path to the doom-source binary")
    game.add_argument("--wad",
                      default=env("WAD", os.path.join(ROOT, "server", "doom1.wad")))
    game.add_argument("--warp", default=env("WARP", "1 1"))
    game.add_argument("--skill", type=int, default=env("SKILL", 3, int))
    game.add_argument("--doom-output", action="store_true",
                     help="let DOOM print to the console")
    game.add_argument("--bundle", default=env("BUNDLE", ""),
                      help="path to a .jsdos bundle, for --source jsdos")
    game.add_argument("--keymap", default=env("KEYMAP", ""),
                      help="JSON file of role -> key names for --source jsdos "
                           "(roles: left right up down fire double use run "
                           "start menu)")
    game.add_argument("--jsdos-backend", default=env("JSDOS_BACKEND", "dosboxNode"),
                      help="dosboxNode (default) or dosboxXNode")
    game.add_argument("--dos-output", action="store_true",
                      help="let DOSBox print its console to stderr")
    game.add_argument("--video", default=env("VIDEO", ""),
                      help="video file for --source video (needs OpenCV)")
    game.add_argument("--exodos-root", default=os.environ.get("EXODOS_ROOT", ""),
                      help="eXoDOS collection to run DOS titles from in place when "
                           "they have no bundle (also the library's exodos.root)")
    game.add_argument("--reel", action="store_true", default=env_flag("REEL"),
                      help="start the kiosk in its demo reel instead of the menu")
    game.add_argument("--library", default=env("LIBRARY", ""),
                      help="games.json for --source kiosk (defaults to "
                           "kiosk/games.json, then kiosk/games.example.json)")

    ctl = ap.add_argument_group("controls")
    ctl.add_argument("--no-tilt", dest="tilt", action="store_false")
    # Defaults measured on the AtomS3R held screen-up: tilting left drives
    # x positive, tilting forward drives y negative, so both are inverted.
    ctl.add_argument("--tilt-turn", type=axis,
                     default=axis(env("TILT_TURN", "x-")),
                     help="accel axis for turning, e.g. x- (default x-)")
    ctl.add_argument("--tilt-move", type=axis,
                     default=axis(env("TILT_MOVE", "y-")),
                     help="accel axis for forward/back (default y-)")
    ctl.add_argument("--deadzone", type=int, default=env("DEADZONE", 180, int),
                     help="milli-g before a tilt counts")
    ctl.add_argument("--run", type=int, default=env("RUN", 0, int),
                     help="milli-g of tilt that also means run; 0 disables. "
                          "Full tilt is about 700, so anything below ~600 "
                          "makes every committed lean a sprint")
    ctl.add_argument("--no-auto-use", dest="auto_use", action="store_false",
                     help="do not pulse USE while walking forward")
    ctl.add_argument("--no-calibrate", dest="calibrate", action="store_false")
    ctl.add_argument("--smooth", type=float, default=env("SMOOTH", 0.4, float),
                     help="accelerometer smoothing, 0-1; lower is smoother and "
                          "slower to respond, 0 disables (default 0.4)")
    ctl.add_argument("--debug-keys", action="store_true",
                     default=env_flag("DEBUG_KEYS"),
                     help="log key presses and releases with the tilt values "
                          "that caused them -- a few lines a second, safe to "
                          "leave on while playing")
    ctl.add_argument("--debug-input", action="store_true",
                     default=env_flag("DEBUG_INPUT"),
                     help="log every input packet: 30 lines a second, and the "
                          "writing blocks the service loop")

    args = ap.parse_args()

    try:
        width, height = (int(v) for v in args.size.lower().split("x"))
    except ValueError:
        ap.error("--size wants WxH, e.g. 128x128")

    if args.source == "kiosk":
        source = None            # built below, once the tilt settings exist
    elif args.source == "doom":
        source = DoomSource(exe=args.exe, wad=args.wad, warp=args.warp,
                            skill=args.skill, quiet=not args.doom_output)
    elif args.source == "jsdos":
        from microstream.sources.jsdos import JsDosSource
        if not args.bundle:
            ap.error("--source jsdos needs --bundle FILE.jsdos")
        source = JsDosSource(bundle=args.bundle, backend=args.jsdos_backend,
                             quiet=not args.dos_output,
                             verbose_dos=args.dos_output)
    elif args.source == "video":
        from microstream.sources.video import VideoSource
        if not args.video:
            ap.error("--source video needs --video FILE")
        try:
            source = VideoSource(args.video)
        except RuntimeError as exc:
            ap.error(str(exc))
    else:
        from microstream.sources.test import TestSource
        source = TestSource(width=width, height=height)

    turn_axis, turn_inv = args.tilt_turn
    move_axis, move_inv = args.tilt_move
    tilt_kw = dict(tilt=args.tilt, turn_axis=turn_axis, turn_invert=turn_inv,
                   move_axis=move_axis, move_invert=move_inv,
                   deadzone=args.deadzone, run=args.run,
                   calibrate=args.calibrate, smooth=args.smooth)

    if args.source == "kiosk":
        from microstream.sources.kiosk import KioskSource

        path = args.library
        if not path:
            for candidate in ("games.json", "games.example.json"):
                candidate = os.path.join(ROOT, "kiosk", candidate)
                if os.path.exists(candidate):
                    path = candidate
                    break
        if not path or not os.path.exists(path):
            ap.error("no game library found; pass --library FILE")

        with open(path, "r", encoding="utf-8") as fh:
            library = json.load(fh)
        games = library.get("games", [])
        if not games:
            ap.error("%s lists no games" % path)

        # Resolve relative bundle paths against the library file, and drop
        # entries whose bundle is not actually here: a menu full of titles
        # that fail on selection is worse than a short menu.
        base = os.path.dirname(os.path.abspath(path))

        # Titles with no bundle of their own can run in place, straight from
        # an eXoDOS collection. "prefer": "in_place" does that even when a
        # bundle exists. Loading a zip costs roughly five times its size in
        # memory while it runs, hence the cap.
        from microstream.exodos import Collection
        exo_cfg = library.get("exodos") or {}
        exodos = Collection(args.exodos_root or exo_cfg.get("root") or "")
        prefer_in_place = exo_cfg.get("prefer") == "in_place"
        max_mb = int(exo_cfg.get("max_mb", 700))
        in_place = 0

        playable = []
        for game in games:
            if game.get("engine") == "jsdos":
                bundle = game.get("bundle", "")
                local = game.get("bundle_path") or ""
                if local and not os.path.isabs(local):
                    local = os.path.join(base, local)
                if not local and bundle:
                    if bundle.startswith("http"):
                        # A URL counts as playable once it has been fetched;
                        # fetch-bundle.sh drops it next to the Node backend.
                        local = os.path.join(ROOT, "server", "jsdos",
                                             os.path.basename(bundle))
                    elif os.path.isabs(bundle):
                        local = bundle
                    else:
                        # Relative names resolve against the library file, or
                        # against server/jsdos where from-exodos.py writes.
                        local = os.path.join(base, bundle)
                        if not os.path.exists(local):
                            local = os.path.join(ROOT, "server", "jsdos", bundle)
                have_bundle = bool(local) and os.path.exists(local)
                if game.get("exodos") and exodos.has(game["exodos"]) and (
                        prefer_in_place or not have_bundle):
                    mb = os.path.getsize(exodos.zip_path(game["exodos"])) / 1e6
                    if mb > max_mb:
                        print("library: skipping %s (%.0f MB, over the %d MB limit "
                              "for running in place)" % (game.get("id"), mb, max_mb))
                        continue
                    playable.append(dict(game, exodos_root=exodos.root))
                    in_place += 1
                    continue
                if not have_bundle:
                    print("library: skipping %s (no bundle, and no eXoDOS collection "
                          "that has it)" % game.get("id"))
                    continue
                game = dict(game, bundle_path=local)
            elif game.get("engine") == "video":
                from microstream.sources import video
                name = game.get("file", "")
                local = name if os.path.isabs(name) else os.path.join(base, name)
                if not name or not os.path.exists(local):
                    print("library: skipping %s (no video file)" % game.get("id"))
                    continue
                if not video.available():
                    print("library: skipping %s (video needs OpenCV)" % game.get("id"))
                    continue
                game = dict(game, file_path=local)
            playable.append(game)

        if not playable:
            ap.error("no playable entries in %s -- fetch a bundle first "
                     "(server/jsdos/fetch-bundle.sh)" % path)

        print("library: %s (%d of %d playable)"
              % (os.path.basename(path), len(playable), len(games)))
        if in_place:
            print("library: %d DOS titles run in place from %s" % (in_place, exodos.root))
        source = KioskSource(playable, width=width, height=height,
                             tilt_kw=tilt_kw,
                             title=library.get("title", "MICRO ARCADE"),
                             settings={k: library[k] for k in ("kiosk", "reel")
                                       if k in library},
                             reel=args.reel)
        print("library: %d in the menu, %d in the demo reel"
              % (len(source.games), len(source.reel_games)))
        profile = DoomProfile(auto_use=args.auto_use, **tilt_kw)
    elif args.source == "jsdos":
        # A DOS game is described by data, so its controls are too.
        keymap = {}
        if args.keymap:
            with open(args.keymap, "r", encoding="utf-8") as fh:
                keymap = json.load(fh)
            if "keys" in keymap:              # accept a whole game entry
                keymap = keymap["keys"]
        profile = KeymapProfile(keymap=keymap,
                                title=os.path.basename(args.bundle),
                                **tilt_kw)
    else:
        profile = DoomProfile(auto_use=args.auto_use, **tilt_kw)

    cfg = Config(bind=args.bind, port=args.port, token=args.token,
                 width=width, height=height, fps=args.fps,
                 quality=args.quality, chroma444=args.chroma444,
                 tile=args.tile, frag=args.frag, letterbox=args.letterbox,
                 brighten=args.brighten, crop_statusbar=args.crop,
                 select=args.select, link_kbps=args.link_kbps,
                 window=args.window, keyframe_ms=args.keyframe_ms,
                 ack_timeout_ms=args.ack_timeout,
                 frag_gap_ms=args.frag_gap, fit_datagram=args.fit_datagram,
                 debug_input=args.debug_input, debug_keys=args.debug_keys)

    source.start()
    server = StreamServer(source, profile, cfg)
    try:
        server.run()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.close()
        source.stop()


if __name__ == "__main__":
    main()
