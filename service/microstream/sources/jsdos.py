"""DOS games as a frame source, via js-dos running headless in Node.

This is what makes the kiosk worth building: DOSBox in WebAssembly is wildly
beyond what an ESP32-S3 could run locally, and there are ~1900 ready-made
`.jsdos` bundles to draw on, so the library is a configuration problem rather
than a porting problem.

The Node side (server/jsdos/jsdos-source.js) speaks exactly the same loopback
protocol as the native DOOM backend, so everything downstream -- scaling,
dirty rectangles, encoding, flow control, the terminal itself -- is unchanged
and shared.
"""

import os
import shutil
import tempfile
import time

from ..keys import code as key_code
from .pipe import PipeSource


class JsDosSource(PipeSource):
    name = "jsdos"
    width = 320
    height = 200
    pixel_aspect = 1.2         # DOS 320x200 was also displayed as 4:3

    #: DOS games use the whole screen; there is no status bar to crop.
    default_crop = None

    #: js-dos takes GLFW-style key codes, which do not fit in a byte.
    key_width = 2

    #: DOSBox needs a moment to boot before the first frame appears.
    connect_timeout = 30.0

    def __init__(self, bundle=None, script=None, node=None, backend="dosboxNode",
                 quiet=True, verbose_dos=False, boot_keys=None,
                 exodos_root=None, exodos_title=None):
        PipeSource.__init__(self, quiet=quiet)
        self.bundle = bundle
        # Or run a title in place, straight from an eXoDOS collection: no
        # bundle on disk, just a small generated zip in the temp folder that
        # is deleted again when the game stops.
        self.exodos_root = exodos_root
        self.exodos_title = exodos_title
        self._skeleton = None
        self.script = script or self._default_script()
        self.node = node or shutil.which("node") or "node"
        self.backend = backend
        self.verbose_dos = verbose_dos

        # Most DOS games open with a setup question -- "select sound card",
        # "press any key" -- and a cabinet with no keyboard would sit there
        # forever. The library answers them as data: a list of
        # {"wait": ms, "key": name} played once frames start flowing.
        self.boot_keys = list(boot_keys or [])
        self._boot_queue = []
        self._boot_armed = False

    @staticmethod
    def _default_script():
        here = os.path.dirname(os.path.abspath(__file__))
        root = os.path.abspath(os.path.join(here, "..", "..", ".."))
        return os.path.join(root, "server", "jsdos", "jsdos-source.js")

    def build_command(self, port):
        if not os.path.exists(self.script):
            raise RuntimeError("no js-dos backend at %s" % self.script)
        if self.exodos_title:
            from ..exodos import Collection
            title = Collection(self.exodos_root).title(self.exodos_title)
            fd, self._skeleton = tempfile.mkstemp(prefix="microarcade-", suffix=".zip")
            with os.fdopen(fd, "wb") as fh:
                fh.write(title.skeleton())
            bundles = [self._skeleton, title.zip_path]
        else:
            if not self.bundle or not os.path.exists(self.bundle):
                raise RuntimeError(
                    "no bundle at %s -- fetch one with server/jsdos/fetch-bundle.sh"
                    % self.bundle)
            bundles = [os.path.abspath(self.bundle)]

        node_modules = os.path.join(os.path.dirname(self.script), "node_modules")
        if not os.path.isdir(node_modules):
            raise RuntimeError(
                "js-dos is not installed -- run: cd %s && npm install"
                % os.path.dirname(self.script))

        if self.verbose_dos:
            os.environ["MD_JSDOS_VERBOSE"] = "1"

        argv = [self.node, self.script, "--connect", "127.0.0.1:%d" % port]
        for path in bundles:
            argv += ["--bundle", path]
        argv += ["--backend", self.backend]
        return argv, os.path.dirname(self.script)

    def describe_command(self, argv):
        if self.exodos_title:
            return "%s, in place from eXoDOS (%s)" % (self.exodos_title, self.backend)
        return "%s (%s)" % (os.path.basename(self.bundle), self.backend)

    def stop(self):
        PipeSource.stop(self)
        self._remove_skeleton()

    def _remove_skeleton(self):
        if self._skeleton:
            try:
                os.remove(self._skeleton)
            except OSError:
                pass
            self._skeleton = None

    # --- unattended start -------------------------------------------------

    def poll(self):
        frame = PipeSource.poll(self)
        if frame is not None and not self._boot_armed:
            # Arm on the first frame, not on connect: the clock should start
            # when DOSBox is actually drawing, however long the bundle took to
            # unpack.
            self._boot_armed = True
            self._arm_boot_keys()
            # js-dos read its zips before drawing anything, so the generated
            # one is no longer needed -- and deleting it now means a server
            # killed mid-game leaves nothing behind in the temp folder.
            self._remove_skeleton()
        self._pump_boot()
        return frame

    def _arm_boot_keys(self):
        if not self.boot_keys:
            return
        now = time.monotonic() * 1000.0
        at = now
        for step in self.boot_keys:
            at += float(step.get("wait", 600))
            code = key_code(step["key"])
            hold = float(step.get("hold", 90))
            self._boot_queue.append((at, True, code))
            self._boot_queue.append((at + hold, False, code))
        print("jsdos: %d scripted keypresses queued for startup"
              % len(self.boot_keys))

    def _pump_boot(self):
        if not self._boot_queue:
            return
        now = time.monotonic() * 1000.0
        while self._boot_queue and self._boot_queue[0][0] <= now:
            _, pressed, code = self._boot_queue.pop(0)
            self.send_key(pressed, code)
