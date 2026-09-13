"""Key codes for the js-dos (DOSBox) backend, and name lookup for game configs.

Transcribed from `emulators-ui/dist/types/dom/keys.d.ts` (js-dos 8.4.2), which
is the authoritative table for `CommandInterface.sendKeyEvent`. These are
GLFW-style codes, *not* the DOSBox `KBD_KEYS` enum ordinals -- guessing the
latter would have produced a mapping that looked plausible and typed garbage.

Game configs name keys as strings ("left", "ctrl", "esc"), so the library is
data rather than code: adding a title means adding a JSON entry, not touching
the input path.
"""

KBD = {
    "none": 0,

    "0": 48, "1": 49, "2": 50, "3": 51, "4": 52,
    "5": 53, "6": 54, "7": 55, "8": 56, "9": 57,

    "a": 65, "b": 66, "c": 67, "d": 68, "e": 69, "f": 70, "g": 71, "h": 72,
    "i": 73, "j": 74, "k": 75, "l": 76, "m": 77, "n": 78, "o": 79, "p": 80,
    "q": 81, "r": 82, "s": 83, "t": 84, "u": 85, "v": 86, "w": 87, "x": 88,
    "y": 89, "z": 90,

    "f1": 290, "f2": 291, "f3": 292, "f4": 293, "f5": 294, "f6": 295,
    "f7": 296, "f8": 297, "f9": 298, "f10": 299, "f11": 300, "f12": 301,

    "kp0": 320, "kp1": 321, "kp2": 322, "kp3": 323, "kp4": 324,
    "kp5": 325, "kp6": 326, "kp7": 327, "kp8": 328, "kp9": 329,
    "kpperiod": 330, "kpdivide": 331, "kpmultiply": 332,
    "kpminus": 333, "kpplus": 334, "kpenter": 335,

    "esc": 256, "tab": 258, "backspace": 259, "enter": 257, "space": 32,

    "leftalt": 342, "rightalt": 346,
    "leftctrl": 341, "rightctrl": 345,
    "leftshift": 340, "rightshift": 344,
    "capslock": 280, "scrolllock": 281, "numlock": 282,

    "grave": 96, "minus": 45, "equals": 61, "backslash": 92,
    "leftbracket": 91, "rightbracket": 93, "semicolon": 59, "quote": 39,
    "period": 46, "comma": 44, "slash": 47, "extra_lt_gt": 348,

    "printscreen": 283, "pause": 284,
    "insert": 260, "home": 268, "pageup": 266,
    "delete": 261, "end": 269, "pagedown": 267,

    "left": 263, "up": 265, "down": 264, "right": 262,
}

#: Friendlier spellings a game config is likely to use.
ALIASES = {
    "ctrl": "leftctrl", "control": "leftctrl",
    "alt": "leftalt",
    "shift": "leftshift",
    "escape": "esc",
    "return": "enter",
    "arrowleft": "left", "arrowright": "right",
    "arrowup": "up", "arrowdown": "down",
    "pgup": "pageup", "pgdn": "pagedown",
    "del": "delete", "ins": "insert",
}


def code(name):
    """Resolve a key name to a js-dos key code, or raise with a clear message."""
    if isinstance(name, int):
        return name
    key = str(name).strip().lower()
    key = ALIASES.get(key, key)
    if key not in KBD:
        raise KeyError("unknown key name %r (see microstream/keys.py)" % name)
    return KBD[key]


def name_of(value):
    for k, v in KBD.items():
        if v == value:
            return k
    return str(value)
