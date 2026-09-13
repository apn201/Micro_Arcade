"""Headless DOOM as a frame source.

Spawns the doomgeneric build from server/build-source.sh, which connects back
over loopback TCP and does nothing but ship 320x200 frames and accept DOOM key
codes. All the framing lives in PipeSource; this file is just the command line
and DOOM's own quirks.
"""

import os

from .pipe import PipeSource


class DoomSource(PipeSource):
    name = "doom"
    width = 320
    height = 200
    pixel_aspect = 1.2                 # 320x200 was always shown as 4:3
    key_width = 1                      # DOOM key codes fit in a byte

    #: DOOM's status bar is the bottom 32 rows. At 128x128 it would eat a sixth
    #: of the screen to show numbers too small to read, so it goes by default.
    default_crop = (0, 0, 320, 168)

    def __init__(self, exe=None, wad=None, warp="1 1", skill=3, extra_args=(),
                 quiet=True):
        PipeSource.__init__(self, quiet=quiet)
        self.exe = exe or self._default_exe()
        self.wad = wad
        self.warp = warp
        self.skill = skill
        self.extra_args = list(extra_args)

    @staticmethod
    def _default_exe():
        here = os.path.dirname(os.path.abspath(__file__))
        root = os.path.abspath(os.path.join(here, "..", "..", ".."))
        return os.path.join(root, "server",
                            "doom-source.exe" if os.name == "nt" else "doom-source")

    def build_command(self, port):
        if not os.path.exists(self.exe):
            raise RuntimeError(
                "no DOOM binary at %s -- build it with server/build-source.sh"
                % self.exe)
        if self.wad and not os.path.exists(self.wad):
            raise RuntimeError("no WAD at %s -- run server/fetch-wad.sh" % self.wad)

        argv = [self.exe, "--connect", "127.0.0.1:%d" % port]
        if self.wad:
            argv += ["-iwad", self.wad]
        if self.warp:
            argv += ["-warp"] + self.warp.split()
        if self.skill:
            argv += ["-skill", str(self.skill)]
        argv += self.extra_args

        # DOOM writes default.cfg and savegames next to its own binary.
        return argv, os.path.dirname(self.exe) or None
