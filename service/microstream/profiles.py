"""Input profiles: raw terminal state -> game key events.

This is where the device's stupidity pays off. The terminal ships button bits
and uncalibrated accelerometer counts; the profile decides what a tilt means,
how far you have to lean, and which key that becomes. Retuning the controls is
a server restart, not a reflash.

Three layers:

* `TiltProfile` -- turning an accelerometer into directions. Calibration,
  smoothing, hysteresis and double-click detection: input shape, no game
  knowledge at all.
* `DoomProfile` -- DOOM's own semantics, which need the game's state byte.
* `KeymapProfile` -- an arbitrary game described by data, which is what the
  DOS library needs: adding a title is a config entry, not code.
"""

from . import protocol as P
from .keys import code as key_code

# DOOM key codes (doomkeys.h)
KEY_RIGHTARROW = 0xAE
KEY_LEFTARROW = 0xAC
KEY_UPARROW = 0xAD
KEY_DOWNARROW = 0xAF
KEY_STRAFE_L = 0xA0
KEY_STRAFE_R = 0xA1
KEY_USE = 0xA2
KEY_FIRE = 0xA3
KEY_ESCAPE = 27
KEY_ENTER = 13
KEY_TAB = 9
KEY_RSHIFT = 0x80 + 0x36
KEY_PAUSE = 0xFF
KEY_Y = ord("y")


class Profile:
    """Base: maps (buttons, accel, state) to the set of keys held right now."""

    name = "base"

    def held_keys(self, buttons, accel, state, now_ms):
        return set()

    def reset(self):
        pass


class TiltProfile(Profile):
    """Accelerometer and button plumbing, with no opinion about the game."""

    name = "tilt"

    #: Two presses inside this window mean "the other action".
    DOUBLE_CLICK_MS = 400
    #: How long that action is then held. A game samples input once per frame,
    #: so this has to span several of them to be seen at all.
    CLICK_HOLD_MS = 300

    # Defaults measured on an AtomS3R held screen-up: tilting left drives x
    # positive, tilting forward drives y negative, so both axes are inverted.
    # These must match run.py's command-line defaults -- when they drifted
    # apart, constructing a profile directly silently gave different controls
    # than the running service.
    def __init__(self, tilt=True, turn_axis=0, turn_invert=True,
                 move_axis=1, move_invert=True, deadzone=180, run=0,
                 hysteresis=60, calibrate=True, smooth=0.4):
        # Exponential smoothing of the raw accelerometer, 0 to disable. A hand
        # holding a device shakes at tens of Hz; sampled at 30Hz that noise
        # crosses the deadzone repeatedly and reads as twitchy controls. 0.4
        # settles in ~3 samples (~100ms), which costs a little response and
        # removes most of the tremor.
        self.smooth = smooth
        self.tilt = tilt
        self.turn_axis = turn_axis
        self.turn_invert = turn_invert
        self.move_axis = move_axis
        self.move_invert = move_invert
        self.deadzone = deadzone
        self.run = run
        self.hysteresis = min(hysteresis, max(1, deadzone - 1))
        self.calibrate = calibrate

        self.reset()

    def reset(self):
        self.neutral = [0, 0, 0]
        self.calibrated = not self.calibrate
        self._cal_n = 0
        self._cal_sum = [0, 0, 0]
        self._turn_dir = 0
        self._move_dir = 0
        self._smoothed = None
        self._prev_fire = False
        self._last_click = 0
        self._click_until = 0
        # Last values that fed the decision, for --debug-keys.
        self.last_turn = 0
        self.last_move = 0

    def describe(self):
        """One line for the startup log. On a deployed box this is the only
        way to see what control configuration is actually running."""
        axes = "xyz"
        if not self.tilt:
            return "controls: %s, tilt disabled" % self.name
        return ("controls: %s tilt turn=%s%s move=%s%s deadzone=%d run=%d "
                "smooth=%.2f calibrate=%d" % (
                    self.name,
                    axes[self.turn_axis], "-" if self.turn_invert else "+",
                    axes[self.move_axis], "-" if self.move_invert else "+",
                    self.deadzone, self.run, self.smooth,
                    int(self.calibrate)))

    # --- tilt ------------------------------------------------------------

    def _schmitt(self, value, prev_dir):
        """Once a direction engages it stays engaged until the tilt falls well
        back inside the deadzone. Without hysteresis, holding the device near
        the threshold makes the game stutter between turning and not turning."""
        on = self.deadzone
        off = self.deadzone - self.hysteresis
        if prev_dir > 0:
            return 1 if value > off else (-1 if value < -on else 0)
        if prev_dir < 0:
            return -1 if value < -off else (1 if value > on else 0)
        if value > on:
            return 1
        if value < -on:
            return -1
        return 0

    def _axis(self, accel, axis, invert):
        v = accel[axis] - self.neutral[axis]
        return -v if invert else v

    def calibrating(self, accel):
        """Feeds the neutral-pose average. True while still sampling."""
        if self.calibrated:
            return False
        for i in range(3):
            self._cal_sum[i] += accel[i]
        self._cal_n += 1
        if self._cal_n >= 15:
            self.neutral = [v // self._cal_n for v in self._cal_sum]
            self.calibrated = True
            print("tilt neutral = %d, %d, %d (milli-g)" % tuple(self.neutral))
        return not self.calibrated

    def directions(self, buttons, accel):
        """(turn, move, turn_magnitude, move_magnitude), or None while still
        calibrating. turn/move are -1, 0 or 1; a joystick overrides the tilt."""
        if self.tilt and self.calibrating(accel):
            return None

        if self.smooth:
            if self._smoothed is None:
                self._smoothed = list(accel)
            else:
                a = self.smooth
                self._smoothed = [s + a * (v - s)
                                  for s, v in zip(self._smoothed, accel)]
            accel = self._smoothed

        turn = move = 0
        turn_mag = move_mag = 0
        if self.tilt:
            tv = self._axis(accel, self.turn_axis, self.turn_invert)
            mv = self._axis(accel, self.move_axis, self.move_invert)
            self.last_turn, self.last_move = tv, mv
            turn = self._turn_dir = self._schmitt(tv, self._turn_dir)
            move = self._move_dir = self._schmitt(mv, self._move_dir)
            turn_mag, move_mag = abs(tv), abs(mv)

        # A real joystick, when one exists, simply overrides the tilt.
        if buttons & P.BTN_LEFT:  turn = -1
        if buttons & P.BTN_RIGHT: turn = 1
        if buttons & P.BTN_UP:    move = 1
        if buttons & P.BTN_DOWN:  move = -1

        return turn, move, turn_mag, move_mag

    def running(self, buttons, turn_mag, move_mag):
        if buttons & P.BTN_RUN:
            return True
        return bool(self.tilt and self.run > 0 and
                    (turn_mag > self.run or move_mag > self.run))

    def double_click(self, buttons, now_ms):
        """True while a double-click's action window is open.

        The second press still counts as a press -- deferring the first one to
        find out whether a second is coming would put 400ms of latency on
        every shot, which is a much worse trade than one wasted bullet.
        """
        fire = bool(buttons & P.BTN_FIRE)
        if fire and not self._prev_fire:
            if self._last_click and now_ms - self._last_click <= self.DOUBLE_CLICK_MS:
                self._click_until = now_ms + self.CLICK_HOLD_MS
                self._last_click = 0        # a triple-click is not two doors
            else:
                self._last_click = now_ms
        self._prev_fire = fire
        return now_ms < self._click_until


class DoomProfile(TiltProfile):
    name = "doom"

    #: auto-USE pulse timing, in ms
    USE_PERIOD = 250
    USE_HOLD = 120

    def __init__(self, auto_use=True, **kw):
        self.auto_use = auto_use
        TiltProfile.__init__(self, **kw)

    def reset(self):
        TiltProfile.reset(self)
        self._use_start = 0
        self._use_on = False

    def describe(self):
        return TiltProfile.describe(self) + " auto_use=%d" % int(self.auto_use)

    def held_keys(self, buttons, accel, state, now_ms):
        keys = set()

        dirs = self.directions(buttons, accel)
        if dirs is None:
            return keys                      # hold still, nothing moves yet
        turn, move, turn_mag, move_mag = dirs

        if turn < 0: keys.add(KEY_LEFTARROW)
        if turn > 0: keys.add(KEY_RIGHTARROW)
        if move > 0: keys.add(KEY_UPARROW)
        if move < 0: keys.add(KEY_DOWNARROW)

        if buttons & P.BTN_STRAFE_L: keys.add(KEY_STRAFE_L)
        if buttons & P.BTN_STRAFE_R: keys.add(KEY_STRAFE_R)
        if buttons & P.BTN_MAP:      keys.add(KEY_TAB)
        if buttons & P.BTN_PAUSE:    keys.add(KEY_PAUSE)
        if buttons & P.BTN_YES:      keys.add(KEY_Y)
        if buttons & P.BTN_ESCAPE:   keys.add(KEY_ESCAPE)
        if buttons & P.BTN_ENTER:    keys.add(KEY_ENTER)

        # Tilting past the run threshold means run: a second analog step out of
        # a sensor DOOM can only read as on or off. `run = 0` disables it,
        # which is often what you want -- full tilt measures about 700 milli-g,
        # so a threshold much below that turns every committed lean into a
        # sprint, and sprinting into walls is most of what "too sensitive"
        # feels like on a 128x128 screen.
        if self.running(buttons, turn_mag, move_mag):
            keys.add(KEY_RSHIFT)

        fire = bool(buttons & P.BTN_FIRE)
        use = bool(buttons & P.BTN_USE)
        in_menu = state in (P.STATE_MENU, P.STATE_NONLEVEL)

        # Double-click the one button to open a door.
        if self.double_click(buttons, now_ms):
            use = True

        if in_menu:
            # One button has to double as "confirm" or the player can never get
            # past the title screen.
            if fire:
                keys.add(KEY_ENTER)
            keys.discard(KEY_RSHIFT)
        elif state == P.STATE_DEAD:
            # Respawn is bound to USE, not FIRE.
            if fire:
                use = True
        elif fire:
            keys.add(KEY_FIRE)

        # Auto-USE: with a single button there is nothing left to open doors
        # with, so pulse USE while walking forward. It also matches how people
        # actually play -- you walk into the door you want to open.
        if self.auto_use and not in_menu and move > 0:
            phase = now_ms - self._use_start
            if not self._use_on:
                self._use_start, self._use_on = now_ms, True
                use = True
            elif phase < self.USE_HOLD:
                use = True
            elif phase >= self.USE_PERIOD:
                self._use_start = now_ms
                use = True
        else:
            self._use_on = False

        if use:
            keys.add(KEY_USE)

        return keys


#: What a DOS platformer or shooter usually wants from six inputs.
DEFAULT_DOS_KEYMAP = {
    "left": "left",
    "right": "right",
    "up": "up",
    "down": "down",
    "fire": "leftctrl",
    "double": "leftalt",     # double-click: the second action (jump, usually)
    "use": "space",
    "run": "leftshift",
    "start": "enter",
    "menu": "esc",
}


class KeymapProfile(TiltProfile):
    """An arbitrary game, described by data.

    The map is a dict of role -> key name, so the library is JSON: adding a
    title means describing its controls, not extending this class. Roles left
    out simply do nothing, which is the right behaviour for a game that has no
    second action.
    """

    name = "keymap"

    def __init__(self, keymap=None, title=None, **kw):
        self.title = title
        raw = dict(DEFAULT_DOS_KEYMAP)
        raw.update(keymap or {})
        # Resolve names to codes once, so a typo fails at startup rather than
        # silently doing nothing on the tenth minute of play.
        self.map = {}
        for role, name in raw.items():
            if name is None or name == "":
                continue
            self.map[role] = key_code(name)
        TiltProfile.__init__(self, **kw)

    def describe(self):
        roles = " ".join("%s=%s" % (r, k) for r, k in sorted(self.map.items()))
        return "%s\ncontrols: %s -> %s" % (
            TiltProfile.describe(self), self.title or "keymap", roles)

    def held_keys(self, buttons, accel, state, now_ms):
        keys = set()
        m = self.map

        dirs = self.directions(buttons, accel)
        if dirs is None:
            return keys
        turn, move, turn_mag, move_mag = dirs

        def add(role):
            if role in m:
                keys.add(m[role])

        if turn < 0: add("left")
        if turn > 0: add("right")
        if move > 0: add("up")
        if move < 0: add("down")

        if buttons & P.BTN_FIRE:   add("fire")
        if buttons & P.BTN_USE:    add("use")
        if buttons & P.BTN_ENTER:  add("start")
        if buttons & P.BTN_ESCAPE: add("menu")

        if self.running(buttons, turn_mag, move_mag):
            add("run")

        # Double-click reaches the second action, which for most DOS
        # platformers is jump.
        if self.double_click(buttons, now_ms):
            add("double")

        return keys


PROFILES = {"doom": DoomProfile, "keymap": KeymapProfile}
