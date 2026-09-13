#!/usr/bin/env python3
"""Unit tests for the DOOM input profile: no network, no game, instant.

The profile is where control feel lives, so it is the part most likely to be
changed on a hunch. These pin down the behaviours that are easy to break by
accident -- particularly the ones that were wrong on hardware before anyone
noticed.

    python service/tests/test_profile.py
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from microstream import protocol as P
from microstream import profiles
from microstream.profiles import DoomProfile

failures = []


def check(name, ok, detail=""):
    print("%-46s %s%s" % (name, "PASS" if ok else "FAIL", "  " + detail if detail else ""))
    if not ok:
        failures.append(name)


REST = (0, 0, 1000)


def settled(profile, accel=REST, buttons=0, t0=0, steps=25, step_ms=33):
    """Feed enough packets to finish calibration and settle the smoothing."""
    keys = set()
    t = t0
    for _ in range(steps):
        keys = profile.held_keys(buttons, accel, P.STATE_LEVEL, t)
        t += step_ms
    return keys, t


def test_calibration_holds_still():
    p = DoomProfile()
    keys = p.held_keys(0, (0, 0, 1000), P.STATE_LEVEL, 0)
    check("nothing moves during calibration", keys == set())
    _, t = settled(p)
    check("calibration completes", p.calibrated, "neutral %s" % (p.neutral,))


def test_deadzone_and_run():
    p = DoomProfile(deadzone=180, run=0)
    _, t = settled(p)

    keys, t = settled(p, accel=(0, 100, 1000), t0=t, steps=8)
    check("inside the deadzone nothing fires", keys == set(), "100 milli-g")

    keys, t = settled(p, accel=(0, -700, 700), t0=t, steps=8)
    check("a committed tilt moves forward", profiles.KEY_UPARROW in keys)
    check("run is off by default", profiles.KEY_RSHIFT not in keys,
          "full tilt must not sprint")

    p2 = DoomProfile(deadzone=180, run=600)
    _, t2 = settled(p2)
    keys2, _ = settled(p2, accel=(0, -700, 700), t0=t2, steps=8)
    check("run engages past an explicit threshold",
          profiles.KEY_RSHIFT in keys2)


def test_hysteresis():
    p = DoomProfile(deadzone=180, hysteresis=60, smooth=0)
    _, t = settled(p)

    keys, t = settled(p, accel=(0, -200, 1000), t0=t, steps=3)
    check("just past the deadzone engages", profiles.KEY_UPARROW in keys)

    # Falling back to just inside the deadzone must NOT release: that flicker
    # is what makes a hand-held tilt feel like a stuttering key.
    keys, t = settled(p, accel=(0, -150, 1000), t0=t, steps=3)
    check("holding near the threshold does not chatter",
          profiles.KEY_UPARROW in keys, "150 < deadzone but > release point")

    keys, t = settled(p, accel=(0, -100, 1000), t0=t, steps=3)
    check("well inside the deadzone releases", profiles.KEY_UPARROW not in keys)


def test_double_click_opens_doors():
    p = DoomProfile(auto_use=False)      # isolate the click from auto-USE
    _, t = settled(p)

    # One press: fires, does not open.
    keys = p.held_keys(P.BTN_FIRE, REST, P.STATE_LEVEL, t)
    check("a single press fires", profiles.KEY_FIRE in keys)
    check("a single press does not open a door", profiles.KEY_USE not in keys)
    keys = p.held_keys(0, REST, P.STATE_LEVEL, t + 40)

    # Second press 150ms later: that is a double-click.
    keys = p.held_keys(P.BTN_FIRE, REST, P.STATE_LEVEL, t + 150)
    check("a double-click opens a door", profiles.KEY_USE in keys)

    # USE is held long enough for DOOM to sample it across several 28.5ms tics.
    keys = p.held_keys(0, REST, P.STATE_LEVEL, t + 150 + 200)
    check("USE is held across several tics", profiles.KEY_USE in keys)
    keys = p.held_keys(0, REST, P.STATE_LEVEL, t + 150 + 400)
    check("USE releases afterwards", profiles.KEY_USE not in keys)

    # Two slow presses are two shots, not a door.
    p2 = DoomProfile(auto_use=False)
    _, t2 = settled(p2)
    p2.held_keys(P.BTN_FIRE, REST, P.STATE_LEVEL, t2)
    p2.held_keys(0, REST, P.STATE_LEVEL, t2 + 40)
    keys2 = p2.held_keys(P.BTN_FIRE, REST, P.STATE_LEVEL, t2 + 900)
    check("slow presses are not a double-click", profiles.KEY_USE not in keys2)


def test_one_button_states():
    p = DoomProfile()
    _, t = settled(p)

    keys = p.held_keys(P.BTN_FIRE, REST, P.STATE_MENU, t)
    check("the button confirms in menus", profiles.KEY_ENTER in keys)
    check("and does not fire in menus", profiles.KEY_FIRE not in keys)

    keys = p.held_keys(P.BTN_FIRE, REST, P.STATE_DEAD, t + 100)
    check("the button respawns when dead", profiles.KEY_USE in keys,
          "respawn is bound to USE, not FIRE")


def test_auto_use_while_walking():
    p = DoomProfile(auto_use=True)
    _, t = settled(p)

    seen = set()
    for i in range(40):                       # ~1.3s of walking forward
        seen |= p.held_keys(0, (0, -700, 700), P.STATE_LEVEL, t + i * 33)
    check("walking forward pulses USE", profiles.KEY_USE in seen,
          "otherwise a one-button cabinet cannot open a door at all")


def main():
    for fn in (test_calibration_holds_still, test_deadzone_and_run,
               test_hysteresis, test_double_click_opens_doors,
               test_one_button_states, test_auto_use_while_walking):
        print("\n--- %s ---" % fn.__name__.replace("test_", "").replace("_", " "))
        fn()

    print()
    if failures:
        print("%d check(s) failed: %s" % (len(failures), ", ".join(failures)))
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
