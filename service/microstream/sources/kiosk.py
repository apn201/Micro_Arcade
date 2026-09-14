"""The arcade kiosk: a game library that is itself a frame source.

The menu is not a firmware feature and not a protocol feature. It is a source
that draws a list of games and, when you pick one, starts that game's source
and forwards everything to it. The terminal never learns that a menu exists --
it receives 128x128 rectangles either way -- which is what keeps the device
genuinely dumb and lets the library change without reflashing anything.

Input vocabulary, given a tilt sensor and one button that covers the screen
while pressed:

    tilt up/down     move the selection
    click            select
    double click     next title, where the library turns browsing on
    long press       back to the menu, and take the current pose as level

Long press is handled here rather than in a profile, because it is a system
gesture: it has to work identically in every game, and no game should be able
to bind it. The console equivalent is the home button.

One library, two catalogues. The menu lists what can actually be played. The
demo reel is the attract mode: it runs everything marked for it -- real games,
booting live with their loading screens, controls working -- one after another,
plus recordings that no emulator could run. A library entry says where it
belongs with "catalog": "play", "demo", or both (the default).
"""

import os
import time

import numpy as np
from PIL import Image, ImageDraw

from .. import protocol as P
from ..profiles import AutopilotProfile, DoomProfile, KeymapProfile
from .base import Source

#: Hold the button this long to leave a game. Long enough not to fire by
#: accident mid-fight, short enough not to feel broken.
LONG_PRESS_MS = 900

#: Ignore the start of a press when sampling "level": the tap itself jolts the
#: device, and what matters is the pose it settles into while held.
PRESS_SETTLE_MS = 150

#: Two presses this close together are a double click. Same window the input
#: profiles use, so the gesture feels the same wherever it lands.
DOUBLE_CLICK_MS = 400

#: Tilt past this to move the selection, and wait this long before repeating.
MENU_TILT = 250
MENU_REPEAT_MS = 320

#: Library settings, per mode. "double_click": "next" makes double click skip
#: to the next title; "game" leaves it to the game. "advance_s" moves the reel
#: on by itself (0: only on double click). "idle_s" starts the reel from an
#: untouched menu (0: never). "autopilot" is how the reel plays a title nobody
#: is holding: roles to hold, tap and weave between (false: leave it alone).
#: A title's own "demo_input" overrides it.
DEFAULT_SETTINGS = {
    "kiosk": {"double_click": "game"},
    "reel": {"advance_s": 45, "double_click": "next", "idle_s": 120, "autopilot": {}},
}

#: The menu row that starts the demo reel.
REEL_ROW = {"id": "_reel", "title": "DEMO REEL"}


def in_catalog(game, name):
    cat = game.get("catalog", ("play", "demo"))
    if isinstance(cat, str):
        cat = (cat,)
    return name in cat


class Menu:
    """Draws the library list at the device's own resolution."""

    ROW_H = 13
    TOP = 26

    def __init__(self, width, height, title="MICRO ARCADE"):
        self.width = width
        self.height = height
        self.title = title
        self.rows = max(1, (height - self.TOP - 14) // self.ROW_H)

    def render(self, games, selected, status=None):
        img = Image.new("RGB", (self.width, self.height), (8, 8, 16))
        d = ImageDraw.Draw(img)

        d.rectangle([0, 0, self.width, 18], fill=(40, 12, 12))
        d.text((4, 4), self.title, fill=(255, 140, 60))

        # Scroll so the selection stays visible without jumping around.
        first = 0
        if len(games) > self.rows:
            first = min(max(0, selected - self.rows // 2), len(games) - self.rows)

        for i in range(first, min(len(games), first + self.rows)):
            y = self.TOP + (i - first) * self.ROW_H
            chosen = (i == selected)
            if chosen:
                d.rectangle([2, y - 2, self.width - 3, y + self.ROW_H - 4],
                            fill=(60, 60, 110))
            name = games[i].get("title", games[i].get("id", "?"))
            if games[i] is REEL_ROW:
                colour = (255, 200, 110)
            else:
                colour = (255, 255, 255) if chosen else (170, 170, 190)
            d.text((6, y), name[:18], fill=colour)

        if len(games) > self.rows:
            # A crude scrollbar: on a screen this size it is the only way to
            # tell a short list from a long one.
            bar_h = max(6, int(self.height * self.rows / len(games)))
            bar_y = self.TOP + int((self.height - self.TOP - bar_h) *
                                   selected / max(1, len(games) - 1))
            d.rectangle([self.width - 3, bar_y, self.width - 2, bar_y + bar_h],
                        fill=(120, 120, 160))

        d.text((4, self.height - 12), status or "tilt + click", fill=(120, 120, 140))
        return np.asarray(img, dtype=np.uint8)

    def message(self, line1, line2=""):
        img = Image.new("RGB", (self.width, self.height), (8, 8, 16))
        d = ImageDraw.Draw(img)
        d.text((6, self.height // 2 - 12), line1[:18], fill=(255, 180, 80))
        if line2:
            d.text((6, self.height // 2 + 4), line2[:20], fill=(150, 150, 170))
        return np.asarray(img, dtype=np.uint8)


def now_ms():
    return int(time.monotonic() * 1000)


class KioskSource(Source):
    name = "kiosk"
    pixel_aspect = 1.0
    default_crop = None

    def __init__(self, games, width=128, height=128, tilt_kw=None,
                 build_source=None, title="MICRO ARCADE", settings=None,
                 reel=False):
        visible = [g for g in games if not g.get("hidden")]
        self.games = [g for g in visible if in_catalog(g, "play")]
        self.reel_games = [g for g in visible if in_catalog(g, "demo")]
        # The reel row goes last, so the first title stays where it always was.
        self.rows = list(self.games) + ([REEL_ROW] if self.reel_games else [])

        self.settings = {k: dict(v) for k, v in DEFAULT_SETTINGS.items()}
        for section, values in (settings or {}).items():
            self.settings.setdefault(section, {}).update(values or {})

        self.width = width
        self.height = height
        self.tilt_kw = dict(tilt_kw or {})
        self.build_source = build_source or default_build_source
        self.menu = Menu(width, height, title)

        self.mode = "menu"           # "menu", "play" or "reel"
        self.child = None
        self.child_game = None
        self.child_started_ms = 0
        self.profile = None          # the server reads this every tick
        self.selected = 0
        self.play_index = 0
        self.reel_index = 0
        self.pending = None          # a title waiting to be started
        self.dirty = True
        self.status = None
        self._start_in_reel = reel
        self._reel_failures = 0

        self._press_started = 0
        self._long_fired = False
        self._press_sum = [0, 0, 0]
        self._press_n = 0
        self._last_click = 0
        self._last_input = now_ms()
        self.neutral = None          # tilt "level" from the last long press
        self._last_move = 0
        self._frame = None

    # --- geometry the server has to follow --------------------------------

    def geometry(self):
        """(w, h, crop, pixel_aspect) of whatever is on screen right now."""
        if self.child:
            return (self.child.width, self.child.height,
                    self.child.default_crop, self.child.pixel_aspect)
        return (self.width, self.height, None, 1.0)

    def start(self):
        if self._start_in_reel and self.reel_games:
            self.start_reel(0)
        else:
            self.show_menu()

    def alive(self):
        return True                  # the kiosk outlives any single game

    def fileno(self):
        return self.child.fileno() if self.child else None

    def stop(self):
        self.stop_child()

    # --- menu / game switching -------------------------------------------

    def show_menu(self, status=None):
        self.mode = "menu"
        self.status = status
        self.profile = None
        self.dirty = True
        self._last_input = now_ms()

    def stop_child(self):
        if self.child:
            print("kiosk: stopping %s" % self.child_game.get("id", "?"))
            try:
                self.child.stop()
            except Exception as exc:                      # noqa: BLE001
                print("kiosk: stopping failed: %s" % exc)
            self.child = None
            self.child_game = None
            self.profile = None

    def launch(self, game):
        """Start a title from the menu."""
        self.mode = "play"
        if game in self.games:
            self.play_index = self.games.index(game)
        self._queue(game)

    def start_reel(self, index=0):
        if not self.reel_games:
            self.show_menu(status="no demo titles")
            return
        self.mode = "reel"
        self.reel_index = index % len(self.reel_games)
        game = self.reel_games[self.reel_index]
        # A title can boot differently for the reel: straight to its intro or
        # attract mode rather than all the way into a level.
        if "demo_keys" in game:
            game = dict(game, boot_keys=game["demo_keys"])
        self._queue(game)

    def next_title(self):
        if self.mode == "reel":
            self.start_reel(self.reel_index + 1)
        elif self.mode == "play" and self.games:
            self.launch(self.games[(self.play_index + 1) % len(self.games)])

    def _queue(self, game):
        """Queue a title. The actual start happens on a later poll, so the
        loading card reaches the device first -- starting DOSBox blocks the
        service loop for a second or two, and a frozen screen with no
        explanation looks like a crash."""
        self.stop_child()
        self.pending = game
        self._frame = None
        self.dirty = True

    def _start_pending(self):
        game = self.pending
        self.pending = None
        self.stop_child()

        try:
            source, profile = self.build_source(game, self.tilt_kw)
            if self.neutral is not None and hasattr(profile, "preset_neutral"):
                profile.preset_neutral(self.neutral)
            if self._skip_on_double() and hasattr(profile, "double_click_enabled"):
                profile.double_click_enabled = False
            if self.mode == "reel" and profile is not None:
                # Nobody is holding the cabinet in attract mode, so the reel
                # plays the game itself -- but only once the scripted boot
                # keys have had time to get into it.
                pattern = game.get("demo_input", self.settings["reel"].get("autopilot"))
                if pattern is not False and pattern is not None:
                    boot_ms = sum(float(step.get("wait", 0))
                                  for step in game.get("boot_keys") or [])
                    profile = AutopilotProfile(profile, pattern,
                                               start_after_ms=int(boot_ms) + 4000)
            source.start()
        except Exception as exc:                          # noqa: BLE001
            print("kiosk: %s failed to start: %s" % (game.get("id"), exc))
            if self.mode == "reel":
                # One broken title must not stop the attract loop, but a reel
                # where nothing starts must not spin forever either.
                self._reel_failures += 1
                if self._reel_failures < len(self.reel_games):
                    self.start_reel(self.reel_index + 1)
                    return
            self.show_menu(status=str(exc)[:22])
            return

        self._reel_failures = 0
        self.child = source
        self.child_game = game
        self.child_started_ms = now_ms()
        self.profile = profile
        print("kiosk: %s %s" % ("demo reel:" if self.mode == "reel" else "playing",
                               game.get("title", game.get("id"))))

    def _skip_on_double(self):
        section = "reel" if self.mode == "reel" else "kiosk"
        return self.settings.get(section, {}).get("double_click") == "next"

    # --- input ------------------------------------------------------------

    def set_raw_input(self, buttons, accel):
        now = now_ms()
        pressed = bool(buttons & P.BTN_FIRE)

        if pressed:
            if not self._press_started:
                self._press_started = now
                self._long_fired = False
                self._press_sum = [0, 0, 0]
                self._press_n = 0
                self._last_input = now

                # Double click, when browsing is on: the second press of a
                # pair skips to the next title. The first press has already
                # reached the game -- delaying every press to wait for a
                # possible second one would cost far more than it saves.
                if (self.child or self.pending) and self._skip_on_double():
                    if self._last_click and now - self._last_click <= DOUBLE_CLICK_MS:
                        self._last_click = 0
                        self._long_fired = True      # the rest of this press is spent
                        self.next_title()
                        return
                    self._last_click = now

            # Long press is the system gesture: leave whatever is running, and
            # take the pose the device is held in as "level". Holding still
            # with a finger on the screen for most of a second is the best
            # calibration sample this cabinet ever gets.
            elif not self._long_fired:
                if now - self._press_started >= PRESS_SETTLE_MS:
                    for i in range(3):
                        self._press_sum[i] += accel[i]
                    self._press_n += 1
                if now - self._press_started >= LONG_PRESS_MS:
                    self._long_fired = True
                    self._recalibrate()
                    self.stop_child()
                    self.pending = None
                    self.show_menu(status="tilt reset")
                    return
        else:
            was = self._press_started
            fired = self._long_fired
            self._press_started = 0
            self._long_fired = False

            # A click only counts on release, and only if it was not the tail
            # of a long press.
            if was and not fired and self.mode == "menu" and not self.pending:
                if self.rows:
                    row = self.rows[self.selected]
                    if row is REEL_ROW:
                        self.start_reel(0)
                    else:
                        self.launch(row)
                return

        if self.mode != "menu" or self.pending:
            return                   # the game owns the rest of the input

        # Menu navigation. The same tilt axis that walks forward in a game
        # moves the selection here.
        if not self.rows:
            return
        axis = self.tilt_kw.get("move_axis", 1)
        move = accel[axis] if axis < len(accel) else 0
        if self.neutral is not None and axis < len(self.neutral):
            move -= self.neutral[axis]
        if self.tilt_kw.get("move_invert", True):
            move = -move

        if abs(move) > MENU_TILT and now - self._last_move >= MENU_REPEAT_MS:
            self._last_move = now
            self._last_input = now
            step = -1 if move > 0 else 1      # tilt forward moves up the list
            self.selected = (self.selected + step) % len(self.rows)
            self.dirty = True

    def _recalibrate(self):
        if not self._press_n:
            return
        self.neutral = [v // self._press_n for v in self._press_sum]
        print("kiosk: tilt level reset to %d, %d, %d (long press)" % tuple(self.neutral))

    def send_key(self, pressed, key):
        if self.child:
            self.child.send_key(pressed, key)

    # --- frames -----------------------------------------------------------

    def poll(self):
        now = now_ms()

        # An untouched menu turns into the attract loop, like a cabinet would.
        idle_s = self.settings["reel"].get("idle_s", 0)
        if (self.mode == "menu" and not self.pending and self.reel_games and
                idle_s and now - self._last_input >= idle_s * 1000):
            print("kiosk: menu idle, starting the demo reel")
            self.start_reel(0)

        # The reel moves on by itself unless it is set to wait for a double
        # click. A title's own demo_s wins over the reel's default.
        if self.child and self.mode == "reel":
            advance_s = self.child_game.get("demo_s",
                                            self.settings["reel"].get("advance_s", 0))
            if advance_s and now - self.child_started_ms >= advance_s * 1000:
                self.next_title()

        if self.pending is not None and self._frame is not None:
            # The loading card has been drawn at least once; now block.
            self._start_pending()

        if self.child:
            frame = self.child.poll()
            if frame is None and not self.child.alive():
                print("kiosk: %s exited" % self.child_game.get("id", "?"))
                if self.mode == "reel":
                    self.next_title()
                    return None
                self.stop_child()
                self.show_menu(status="game exited")
                return None
            return frame

        if self.pending is not None:
            title = self.pending.get("title", self.pending.get("id", "game"))
            heading = "DEMO REEL" if self.mode == "reel" else "LOADING"
            self._frame = self.menu.message(heading, title)
            return self._frame, P.STATE_NONLEVEL

        if self.dirty:
            self.dirty = False
            self._frame = self.menu.render(self.rows, self.selected, self.status)
            return self._frame, P.STATE_MENU

        return None


def default_build_source(game, tilt_kw):
    """(source, profile) for a library entry. Engines are data, not code."""
    engine = game.get("engine", "jsdos")
    controls = dict(tilt_kw)
    controls.update(game.get("controls", {}))

    if engine == "doom":
        from .doom import DoomSource
        args = game.get("args", {})
        source = DoomSource(wad=game.get("wad") or _default_wad(),
                            warp=args.get("warp", "1 1"),
                            skill=args.get("skill", 3))
        profile = DoomProfile(auto_use=args.get("auto_use", True), **controls)
        return source, profile

    if engine == "jsdos":
        from .jsdos import JsDosSource
        bundle = game.get("bundle_path")
        # No bundle of its own, but a collection that has it: run in place.
        in_place = bool(game.get("exodos_root")) and not bundle
        source = JsDosSource(bundle=bundle or game.get("bundle"),
                             backend=game.get("backend", "dosboxNode"),
                             boot_keys=game.get("boot_keys"),
                             exodos_root=game.get("exodos_root") if in_place else None,
                             exodos_title=game.get("exodos") if in_place else None)
        profile = KeymapProfile(keymap=game.get("keys", {}),
                                title=game.get("title"), **controls)
        return source, profile

    if engine == "video":
        from .video import VideoSource
        source = VideoSource(path=game.get("file_path") or game.get("file"),
                             loop=game.get("loop", True),
                             start_s=game.get("start_s", 0))
        # A recording has no controls: its keys go to a source that ignores
        # them. Long press still leaves it, because the kiosk owns that gesture.
        profile = KeymapProfile(keymap={}, title=game.get("title"), **controls)
        return source, profile

    if engine == "test":
        from .test import TestSource
        source = TestSource()
        return source, DoomProfile(**controls)

    raise RuntimeError("unknown engine %r" % engine)


def _default_wad():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, "..", "..", ".."))
    return os.path.join(root, "server", "doom1.wad")
