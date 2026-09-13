"""Shared plumbing for out-of-process game engines.

Both the native DOOM backend (C) and the js-dos backend (Node) are separate
processes that dial back over loopback TCP and do nothing but ship frames and
accept key events. That contract is identical for both, so it lives here once:
see docs/SOURCE_PROTOCOL.md for the wire format.

A subclass supplies `build_command(port)` and a few attributes. Everything
about framing, resynchronisation, frame-dropping and shutdown is inherited.
"""

import os
import socket
import struct
import subprocess

import numpy as np

from .base import Source

FRAME_MAGIC = b"DGF1"
FRAME_HDR = 16

MSG_KEY = 1        # [type][pressed][key]        8-bit key codes  (DOOM)
MSG_QUIT = 2       # [type][0][0]
MSG_KEY16 = 3      # [type][pressed][lo][hi]     16-bit key codes (js-dos)


class PipeSource(Source):
    #: 1 for native 8-bit key codes, 2 for 16-bit (js-dos)
    key_width = 1

    #: seconds to wait for the engine to connect back
    connect_timeout = 20.0

    def __init__(self, quiet=True):
        self.quiet = quiet
        self.listener = None
        self.conn = None
        self.proc = None
        self._buf = bytearray()
        self._state = 0

    # --- subclass hooks ---------------------------------------------------

    def build_command(self, port):
        """Return (argv, cwd) for the engine process."""
        raise NotImplementedError

    def describe_command(self, argv):
        return " ".join(argv)

    # --- lifecycle --------------------------------------------------------

    def start(self):
        # An ephemeral port means nothing has to agree on a number up front
        # and two sessions can never collide.
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        port = self.listener.getsockname()[1]

        argv, cwd = self.build_command(port)
        print("%s: %s" % (self.name, self.describe_command(argv)))

        self.proc = subprocess.Popen(
            argv, cwd=cwd,
            stdout=subprocess.DEVNULL if self.quiet else None,
            stderr=subprocess.DEVNULL if self.quiet else None)

        self.listener.settimeout(self.connect_timeout)
        try:
            self.conn, _ = self.listener.accept()
        except socket.timeout:
            self.stop()
            raise RuntimeError("%s did not connect back within %.0fs"
                               % (self.name, self.connect_timeout))

        self.conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.conn.setblocking(False)
        print("%s: connected" % self.name)

    def fileno(self):
        return self.conn.fileno() if self.conn else None

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.conn:
            try:
                self.conn.sendall(bytes((MSG_QUIT, 0, 0)))
            except OSError:
                pass
            try:
                self.conn.close()
            except OSError:
                pass
            self.conn = None
        if self.listener:
            self.listener.close()
            self.listener = None
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    # --- frames -----------------------------------------------------------

    def poll(self):
        if not self.conn:
            return None

        while True:
            try:
                chunk = self.conn.recv(262144)
            except (BlockingIOError, InterruptedError):
                break
            except (ConnectionResetError, OSError):
                self.conn = None
                return None
            if not chunk:
                self.conn = None
                return None
            self._buf += chunk

        latest = None
        while len(self._buf) >= FRAME_HDR:
            if self._buf[:4] != FRAME_MAGIC:
                # Resynchronise rather than die: find the next plausible header.
                idx = self._buf.find(FRAME_MAGIC, 1)
                if idx < 0:
                    self._buf.clear()
                    return None
                del self._buf[:idx]
                continue

            w, h = struct.unpack_from("<HH", self._buf, 4)
            channels = self._buf[8]
            state = self._buf[9]
            length = struct.unpack_from("<I", self._buf, 12)[0]

            if len(self._buf) < FRAME_HDR + length:
                break

            payload = bytes(self._buf[FRAME_HDR:FRAME_HDR + length])
            del self._buf[:FRAME_HDR + length]
            latest = (payload, w, h, channels, state)

        if latest is None:
            return None

        payload, w, h, channels, state = latest
        if channels not in (3, 4) or len(payload) != w * h * channels:
            return None

        arr = np.frombuffer(payload, dtype=np.uint8).reshape(h, w, channels)
        if channels == 4:
            # DOOM's buffer is 0x00RRGGBB, which on a little-endian machine is
            # B,G,R,X in memory.
            rgb = np.ascontiguousarray(arr[:, :, 2::-1])
        else:
            rgb = arr                       # js-dos hands over RGB24 already

        self._state = state
        return rgb, state

    # --- input ------------------------------------------------------------

    def send_key(self, pressed, key):
        if not self.conn:
            return
        down = 1 if pressed else 0
        if self.key_width == 2:
            msg = bytes((MSG_KEY16, down, key & 0xFF, (key >> 8) & 0xFF))
        else:
            msg = bytes((MSG_KEY, down, key & 0xFF))
        try:
            self.conn.sendall(msg)
        except OSError:
            self.conn = None
