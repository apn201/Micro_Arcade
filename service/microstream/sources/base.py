"""What the service needs from a game.

A source produces frames and accepts key events. That is the whole contract,
and it is what makes the service reusable: DOOM, a headless browser, a VNC
connection and a test pattern all look identical from here.
"""


class Source:
    #: native resolution of the frames this source produces
    width = 320
    height = 200

    #: 1.2 for DOOM's 320x200-shown-as-4:3; 1.0 for anything with square pixels
    pixel_aspect = 1.0

    #: (x, y, w, h) worth showing on a tiny screen, or None for all of it
    default_crop = None

    #: human-readable, used in logs and the /api listing
    name = "source"

    def start(self):
        raise NotImplementedError

    def poll(self):
        """Return (rgb_array, state) for the newest frame, or None if there is
        no new one. Implementations must discard stale frames rather than
        queueing them: on a slow link, the freshest frame is the only one worth
        sending."""
        raise NotImplementedError

    def send_key(self, pressed, key):
        """Inject one key transition. `key` is a DOOM key code -- the input
        profile has already done the mapping."""

    def set_raw_input(self, buttons, accel):
        """Optional: raw terminal state, for sources that want to display it
        (the calibration pattern) rather than play with it."""

    def fileno(self):
        """A selectable fd, or None if this source cannot be waited on."""
        return None

    def alive(self):
        return True

    def stop(self):
        pass
