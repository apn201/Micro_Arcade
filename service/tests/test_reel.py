#!/usr/bin/env python3
"""The kiosk's two catalogues, driven with the cabinet's own gestures.

No emulator and no network: dummy sources stand in for games, so this checks
the logic only -- which titles the menu lists, the demo reel starting from an
idle menu, its timer and double-click skip, browsing in the normal kiosk, and
that long press always gets you home.

    python service/tests/test_reel.py
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from microstream import protocol as P
from microstream.profiles import KeymapProfile
from microstream.sources import kiosk as K


class Dummy:
    width, height, default_crop, pixel_aspect = 320, 200, None, 1.0

    def __init__(self):
        self.running = True

    def start(self):
        pass

    def stop(self):
        self.running = False

    def poll(self):
        return None

    def fileno(self):
        return None

    def alive(self):
        return self.running

    def send_key(self, pressed, key):
        pass


started = []
profiles = []


def build(game, tilt):
    started.append(game["id"])
    profile = KeymapProfile(keymap={}, **tilt)
    profiles.append(profile)
    return Dummy(), profile


GAMES = [
    {"id": "stunts", "title": "Stunts"},                          # both, the default
    {"id": "doom", "title": "DOOM", "catalog": "play"},
    {"id": "dlair", "title": "Dragon's Lair", "catalog": ["demo"]},
    {"id": "skyrim", "title": "Skyrim", "catalog": "demo", "demo_s": 0.2},
]
TILT = dict(tilt=True, turn_axis=0, turn_invert=True, move_axis=1,
            move_invert=True, calibrate=False)


def pump(k, seconds, buttons=0, accel=(0, 0, 1000)):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        k.set_raw_input(buttons, accel)
        k.poll()
        time.sleep(0.01)


def click(k):
    pump(k, 0.06, P.BTN_FIRE)
    pump(k, 0.03)


def double_click(k):
    click(k)
    click(k)


def long_press(k):
    pump(k, 1.0, P.BTN_FIRE)
    pump(k, 0.05)


def test_catalogues_and_reel():
    del started[:]
    k = K.KioskSource(GAMES, tilt_kw=TILT, build_source=build,
                      settings={"reel": {"advance_s": 0.6, "idle_s": 0.3,
                                         "double_click": "next"}})
    k.start()
    k.poll()

    # The menu lists what can be played, with the reel as its last row.
    titles = [r["title"] for r in k.rows]
    assert titles == ["Stunts", "DOOM", "DEMO REEL"], titles
    assert [g["id"] for g in k.reel_games] == ["stunts", "dlair", "skyrim"]

    # An idle menu starts the reel by itself.
    pump(k, 0.5)
    assert k.mode == "reel" and started[-1] == "stunts", (k.mode, started)

    # The timer moves it on, live launch after live launch.
    pump(k, 0.6)
    assert started[-1] == "dlair", started

    # Double click skips without waiting for the timer.
    double_click(k)
    pump(k, 0.1)
    assert started[-1] == "skyrim", started

    # A title's own demo_s wins over the reel's, and the reel wraps.
    pump(k, 0.4)
    assert started[-1] == "stunts", started

    # Browsing owns the double click, so the game does not also get it.
    assert profiles[-1].double_click_enabled is False

    # Long press always goes home.
    long_press(k)
    assert k.mode == "menu" and k.child is None, (k.mode, k.child)


def test_reel_without_timer():
    del started[:]
    k = K.KioskSource(GAMES, tilt_kw=TILT, build_source=build,
                      settings={"reel": {"advance_s": 0, "idle_s": 0,
                                         "double_click": "next"}},
                      reel=True)
    k.start()
    pump(k, 0.3)
    assert started == ["stunts"], started
    pump(k, 0.8)
    assert started == ["stunts"], "advance_s 0 must wait for a double click"
    double_click(k)
    pump(k, 0.1)
    assert started[-1] == "dlair", started


def test_browse_in_kiosk():
    del started[:]
    k = K.KioskSource(GAMES, tilt_kw=TILT, build_source=build,
                      settings={"kiosk": {"double_click": "next"},
                                "reel": {"idle_s": 0}})
    k.start()
    k.poll()
    click(k)
    pump(k, 0.1)
    assert k.mode == "play" and started[-1] == "stunts", started
    double_click(k)
    pump(k, 0.1)
    assert started[-1] == "doom", started            # the next playable title
    double_click(k)
    pump(k, 0.1)
    assert started[-1] == "stunts", started          # wraps, never into demo-only


def test_double_click_stays_with_game_by_default():
    del started[:]
    del profiles[:]
    k = K.KioskSource(GAMES, tilt_kw=TILT, build_source=build,
                      settings={"reel": {"idle_s": 0}})
    k.start()
    k.poll()
    click(k)
    pump(k, 0.1)
    double_click(k)
    pump(k, 0.1)
    assert started == ["stunts"], started
    assert profiles[-1].double_click_enabled is True


def test_autopilot():
    from microstream.profiles import AutopilotProfile
    inner = KeymapProfile(keymap={}, **TILT)
    auto = AutopilotProfile(inner, {"tap_ms": 600, "tap_hold_ms": 150, "weave": []},
                            start_after_ms=1000)
    up, fire, left = inner.map["up"], inner.map["fire"], inner.map["left"]
    rest = (0, 0, 1000)

    assert auto.held_keys(0, rest, 0, 10000) == set()      # boot keys still playing
    keys = auto.held_keys(0, rest, 0, 11050)
    assert up in keys and fire in keys, keys               # charging, trigger down
    assert fire not in auto.held_keys(0, rest, 0, 11400)   # between shots

    # Someone picks the cabinet up: their input wins at once.
    real = auto.held_keys(P.BTN_LEFT, rest, 0, 12000)
    assert left in real and up not in real, real
    assert up not in auto.held_keys(0, rest, 0, 15000)     # still their turn
    assert up in auto.held_keys(0, rest, 0, 18500)         # left alone: autopilot again


def test_reel_uses_autopilot():
    del started[:]
    del profiles[:]
    games = [{"id": "doom", "title": "DOOM", "boot_keys": [{"wait": 2000.0, "key": "1"}]},
             {"id": "movie", "title": "Movie", "catalog": "demo", "demo_input": False}]
    k = K.KioskSource(games, tilt_kw=TILT, build_source=build,
                      settings={"reel": {"idle_s": 0, "advance_s": 0}}, reel=True)
    k.start()
    pump(k, 0.2)
    assert type(k.profile).__name__ == "AutopilotProfile", type(k.profile)
    assert k.profile.start_after_ms == 6000              # boot keys, plus settling time
    # Drive it through a weave with float boot-key waits, as a real library
    # entry has: this is the call that crashed the server.
    keys = set()
    for now in range(0, 20000, 50):
        keys |= k.profile.held_keys(0, (0, 0, 1000), 0, 100000 + now)
    for role in ("up", "fire", "left", "right"):
        assert k.profile.inner.map[role] in keys, (role, keys)
    double_click(k)
    pump(k, 0.2)
    assert started[-1] == "movie" and k.profile is profiles[-1]   # demo_input false: untouched


if __name__ == "__main__":
    for test in (test_catalogues_and_reel, test_reel_without_timer,
                 test_browse_in_kiosk, test_double_click_stays_with_game_by_default,
                 test_autopilot, test_reel_uses_autopilot):
        test()
        print("PASS  %s" % test.__name__)
