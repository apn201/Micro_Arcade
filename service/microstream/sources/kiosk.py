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
    long press       leave the game, back to the menu

Long press is handled here rather than in a profile, because it is a system
gesture: it has to work identically in every game, and no game should be able
to bind it. The console equivalent is the home button.
"""

import os
import time

import numpy as np
from PIL import Image, ImageDraw

from .. import protocol as P
from ..profiles import DoomProfile, KeymapProfile
from .base import Source

#: Hold the button this long to leave a game. Long enough not to fire by
#: accident mid-fight, short enough not to feel broken.
LONG_PRESS_MS = 900

#: Tilt past this to move the selection, and wait this long before repeating.
MENU_TILT = 250
MENU_REPEAT_MS = 320


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
            d.text((6, y), name[:18],
                   fill=(255, 255, 255) if chosen else (170, 170, 190))

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


class KioskSource(Source):
    name = "kiosk"
    pixel_aspect = 1.0
    default_crop = None

    def __init__(self, games, width=128, height=128, tilt_kw=None,
                 build_source=None, title="MICRO ARCADE"):
        self.games = [g for g in games if not g.get("hidden")]
        self.width = width
        self.height = height
        self.tilt_kw = dict(tilt_kw or {})
        self.build_source = build_source or default_build_source
        self.menu = Menu(width, height, title)

        self.child = None
        self.child_game = None
        self.profile = None          # the server reads this every tick
        self.selected = 0
        self.pending = None          # a game waiting to be started
        self.dirty = True
        self.status = None

        self._press_started = 0
        self._long_fired = False
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
        self.show_menu()

    def alive(self):
        return True                  # the kiosk outlives any single game

    def fileno(self):
        return self.child.fileno() if self.child else None

    def stop(self):
        self.stop_child()

    # --- menu / game switching -------------------------------------------

    def show_menu(self, status=None):
        self.status = status
        self.profile = None
        self.dirty = True

    def stop_child(self):
        if self.child:
            print("kiosk: stopping %s" % self.child_game.get("id", "?"))
            try:
                self.child.stop()
            except Exception as exc:                      # noqa: BLE001
                print("kiosk: stopping failed: %s" % exc)
            self.child = None
            self.child_game = None

    def launch(self, game):
        """Queue a game. The actual start happens on the next poll so the
        loading screen reaches the device first -- starting DOSBox blocks the
        service loop for a second or two, and a frozen menu with no
        explanation looks like a crash."""
        self.pending = game
        self.dirty = True

    def _start_pending(self):
        game = self.pending
        self.pending = None
        self.stop_child()

        try:
            source, profile = self.build_source(game, self.tilt_kw)
            source.start()
        except Exception as exc:                          # noqa: BLE001
            print("kiosk: %s failed to start: %s" % (game.get("id"), exc))
            self.show_menu(status=str(exc)[:22])
            return

        self.child = source
        self.child_game = game
        self.profile = profile
        print("kiosk: playing %s" % game.get("title", game.get("id")))

    # --- input ------------------------------------------------------------

    def set_raw_input(self, buttons, accel):
        now = int(time.monotonic() * 1000)
        pressed = bool(buttons & P.BTN_FIRE)

        # Long press is the system gesture: leave whatever is running.
        if pressed:
            if not self._press_started:
                self._press_started = now
                self._long_fired = False
            elif (not self._long_fired and
                  now - self._press_started >= LONG_PRESS_MS):
                self._long_fired = True
                if self.child or self.pending:
                    self.stop_child()
                    self.pending = None
                    self.show_menu(status="back")
                    return
        else:
            was = self._press_started
            fired = self._long_fired
            self._press_started = 0
            self._long_fired = False

            # A click only counts on release, and only if it was not the tail
            # of a long press.
            if was and not fired and not self.child and not self.pending:
                if self.games:
                    self.launch(self.games[self.selected])
                return

        if self.child or self.pending:
            return                   # the game owns the rest of the input

        # Menu navigation. The same tilt axis that walks forward in a game
        # moves the selection here.
        if not self.games:
            return
        axis = self.tilt_kw.get("move_axis", 1)
        move = accel[axis] if axis < len(accel) else 0
        if self.tilt_kw.get("move_invert", True):
            move = -move

        if abs(move) > MENU_TILT and now - self._last_move >= MENU_REPEAT_MS:
            self._last_move = now
            step = -1 if move > 0 else 1      # tilt forward moves up the list
            self.selected = (self.selected + step) % len(self.games)
            self.dirty = True

    def send_key(self, pressed, key):
        if self.child:
            self.child.send_key(pressed, key)

    # --- frames -----------------------------------------------------------

    def poll(self):
        if self.pending is not None and self._frame is not None:
            # The loading screen has been drawn at least once; now block.
            self._start_pending()

        if self.child:
            frame = self.child.poll()
            if frame is None and not self.child.alive():
                print("kiosk: %s exited" % self.child_game.get("id", "?"))
                self.stop_child()
                self.show_menu(status="game exited")
                return None
            return frame

        if self.pending is not None:
            title = self.pending.get("title", self.pending.get("id", "game"))
            self._frame = self.menu.message("LOADING", title)
            return self._frame, P.STATE_NONLEVEL

        if self.dirty:
            self.dirty = False
            self._frame = self.menu.render(self.games, self.selected, self.status)
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
        bundle = game.get("bundle_path") or game.get("bundle")
        source = JsDosSource(bundle=bundle,
                             backend=game.get("backend", "dosboxNode"),
                             boot_keys=game.get("boot_keys"))
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
