"""The streaming service: one source, one client, rectangle updates over UDP.

Deliberately single-threaded. Everything expensive (resize, JPEG, deflate,
frame parsing) happens inside numpy/Pillow/zlib C code, and the loop itself
just moves bytes, so threads would buy contention rather than throughput.
"""

import selectors
import socket
import time

from . import keys
from . import protocol as P
from . import profiles
from .codec import DeviceCost, Encoder, Scaler

#: Readable names for the key transitions --debug-keys prints.
KEY_NAMES = {
    profiles.KEY_LEFTARROW: "LEFT", profiles.KEY_RIGHTARROW: "RIGHT",
    profiles.KEY_UPARROW: "FWD", profiles.KEY_DOWNARROW: "BACK",
    profiles.KEY_FIRE: "FIRE", profiles.KEY_USE: "USE",
    profiles.KEY_RSHIFT: "RUN", profiles.KEY_ENTER: "ENTER",
    profiles.KEY_ESCAPE: "ESC", profiles.KEY_TAB: "MAP",
    profiles.KEY_STRAFE_L: "STRAFE_L", profiles.KEY_STRAFE_R: "STRAFE_R",
}


def key_label(code):
    """Name a key for the log, whichever engine's codes it belongs to.

    DOOM's codes and js-dos's overlap numerically, so this prefers DOOM's
    names (0xa0-0xaf) and falls back to the js-dos table, which covers the
    DOS library. A raw hex code in the log means neither table knew it.
    """
    if code in KEY_NAMES:
        return KEY_NAMES[code]
    name = keys.name_of(code)
    return name if name != str(code) else hex(code)


def now_ms():
    return int(time.monotonic() * 1000) & 0xFFFFFFFF


def newer(a, b):
    """Is frame id `a` newer than `b`, allowing for 16-bit wraparound?"""
    return a != b and ((a - b) & 0xFFFF) < 0x8000


class Config:
    def __init__(self, **kw):
        self.bind = "0.0.0.0"
        self.port = 20002
        self.token = ""
        self.width = 128
        self.height = 128
        self.fps = 20
        self.quality = 55
        self.chroma444 = False
        self.tile = 32
        # 1400 keeps a datagram under a 1500-byte MTU while cutting a typical
        # DOOM frame from three fragments to two. Every fragment is another
        # chance for the device to miss one.
        self.frag = 1400
        # How many updates may be unacknowledged at once. The device is blind
        # while it decodes, so pipelining more than this just overflows lwIP's
        # UDP queue and nothing completes at all.
        self.window = 2
        # How long to wait for an acknowledgement before sending anyway. This
        # is the length of the freeze after a lost update, so a fixed generous
        # value is exactly wrong: on a quiet LAN (round trip ~10ms) 300ms is
        # twenty lost frames. 0 means adapt to the measured round trip,
        # clamped to [ACK_MIN, ACK_MAX].
        self.ack_timeout_ms = 0
        self.ack_min_ms = 80
        self.ack_max_ms = 400
        # Milliseconds between the fragments of one update. Devices whose
        # socket holds a single datagram drop anything that arrives while they
        # are still holding the previous one.
        self.frag_gap_ms = 3
        # Trade JPEG quality to keep an update inside one datagram.
        self.fit_datagram = True
        # Even with acknowledged references, a periodic full frame is cheap
        # insurance against any state divergence at all.
        self.keyframe_ms = 3000
        self.select = "cost"       # "cost" = device decode time, "bytes" = payload
        self.link_kbps = 4000
        self.letterbox = False
        self.brighten = 1.0
        self.crop_statusbar = True
        self.input_timeout_ms = 600
        self.keepalive_ms = 500
        self.stats_ms = 5000
        self.debug_input = False
        self.debug_keys = False
        self.__dict__.update(kw)


class Client:
    def __init__(self, addr, session, caps):
        self.addr = addr
        self.session = session
        self.caps = caps
        self.last_seq = 0
        self.last_input_ms = now_ms()
        self.buttons = 0
        self.accel = (0, 0, 0)
        self.last_frame_id = 0


class StreamServer:
    def __init__(self, source, profile, cfg):
        self.source = source
        self.profile = profile
        self.cfg = cfg

        self.scaler = None
        self._geometry = None
        self.sync_geometry()

        self.encoder = None
        self.client = None
        self.session_counter = 0
        self.held_keys = set()
        self.frame_id = 1        # 0 means "nothing received yet" to a client
        self.last_sent_id = None
        self.state = P.STATE_LEVEL
        self.latest = None

        # The screen the client is known to be showing, and the updates it has
        # not confirmed yet. Diffing against anything else is how a lost packet
        # turns into a permanently frozen region.
        self.ref_frame = None
        self.pending = {}
        self.sent_times = {}
        self.last_keyframe_ms = 0
        self._last_profile = self.active_profile()

        # Fragments waiting to be released, one every frag_gap_ms.
        self.tx_queue = []
        self.last_frag_ms = 0

        # Link diagnostics, reset each stats interval.
        self.blocked = 0
        self.ack_count = 0
        self.ack_sum = 0
        self.ack_max = 0
        self.ack_ewma = None     # smoothed ack latency, drives the timeout
        self.ack_misses = 0      # consecutive timeouts without an ack

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((cfg.bind, cfg.port))
        self.sock.setblocking(False)

        self.sel = selectors.DefaultSelector()
        self.sel.register(self.sock, selectors.EVENT_READ)

        self.last_sent_ms = 0
        self.last_stats_ms = now_ms()
        self.stats_t0 = time.monotonic()

    def sync_geometry(self):
        """Rebuild the scaler when the source's shape changes.

        The kiosk swaps games underneath us, and a DOS game, DOOM (which gets
        its status bar cropped) and the menu itself are three different
        shapes. Comparing a small tuple each frame is cheaper than asking, and
        it means a source can change shape whenever it likes.
        """
        geom = self.source_geometry()
        if geom == self._geometry:
            return
        self._geometry = geom
        width, height, crop, aspect = geom
        if not self.cfg.crop_statusbar:
            crop = None
        self.scaler = Scaler(self.cfg.width, self.cfg.height, crop=crop,
                             letterbox=self.cfg.letterbox,
                             pixel_aspect=aspect, brighten=self.cfg.brighten)
        # The client's screen no longer matches anything we have sent.
        self.ref_frame = None
        self.pending = {}
        print("geometry: %dx%d crop=%s aspect=%.2f" % (width, height, crop, aspect))

    def source_geometry(self):
        geometry = getattr(self.source, "geometry", None)
        if callable(geometry):
            return geometry()
        return (self.source.width, self.source.height,
                self.source.default_crop, self.source.pixel_aspect)

    def active_profile(self):
        """A source may carry its own profile -- the kiosk swaps one in per
        game, so controls are per-title without the server knowing why."""
        return getattr(self.source, "profile", None) or self.profile

    # --- client protocol --------------------------------------------------

    def _new_encoder(self, caps):
        cost = DeviceCost(link_kbps=self.cfg.link_kbps, frag_payload=self.cfg.frag)
        self.encoder = Encoder(self.cfg.width, self.cfg.height, tile=self.cfg.tile,
                               quality=self.cfg.quality, caps=caps,
                               chroma444=self.cfg.chroma444,
                               cost=cost, select=self.cfg.select)

    def handle_hello(self, data, addr):
        msg = P.parse_hello(data)
        if not msg:
            return

        status = P.STATUS_OK
        if msg["ver"] != P.PROTO_VER:
            status = P.STATUS_BAD_VERSION
        elif self.cfg.token and msg["token"] != self.cfg.token:
            status = P.STATUS_BAD_TOKEN

        if status != P.STATUS_OK:
            print("rejected %s:%d (%s)" % (addr[0], addr[1],
                                           P.STATUS_TEXT.get(status, status)))
            self.sock.sendto(P.pack_hello_ack(0, status, self.cfg.width,
                                              self.cfg.height, self.cfg.frag,
                                              self.cfg.tile), addr)
            return

        if not self.client or self.client.addr != addr:
            if self.client:
                self.sock.sendto(P.pack_kick(self.client.session, 0),
                                 self.client.addr)
            self.session_counter = (self.session_counter + 1) & 0xFFFF or 1
            self.client = Client(addr, self.session_counter, msg["caps"])
            self.profile.reset()
            self.release_all_keys()
            caps = " ".join(n for b, n in ((P.CAP_JPEG, "jpeg"),
                                           (P.CAP_DEFLATE, "deflate"))
                            if msg["caps"] & b) or "raw only"
            print("client %s:%d connected (session %d, caps: %s)" % (
                addr[0], addr[1], self.client.session, caps))
        else:
            self.client.caps = msg["caps"]

        self._new_encoder(self.client.caps)
        self.last_sent_id = None
        self.ref_frame = None       # new session: the client's screen is unknown
        self.pending = {}
        self.client.last_input_ms = now_ms()

        self.sock.sendto(P.pack_hello_ack(self.client.session, P.STATUS_OK,
                                          self.cfg.width, self.cfg.height,
                                          self.cfg.frag, self.cfg.tile), addr)

    def handle_packet(self, data, addr):
        ptype = P.packet_type(data)
        if ptype is None:
            return

        if ptype == P.PKT_HELLO:
            self.handle_hello(data, addr)
            return

        c = self.client
        if not c or addr != c.addr or P.packet_session(data) != c.session:
            return

        if ptype == P.PKT_INPUT:
            msg = P.parse_input(data)
            if not msg:
                return
            # UDP reorders; an older snapshot would undo the newer one.
            if c.last_seq and ((msg["seq"] - c.last_seq) & 0xFFFFFFFF) > 0x7FFFFFFF:
                return
            c.last_seq = msg["seq"]
            c.buttons = msg["buttons"]
            c.accel = msg["accel"]
            c.last_frame_id = msg["last_frame_id"]
            c.last_input_ms = now_ms()
            self.on_ack(msg["last_frame_id"])
            # Sources that display input rather than play with it (the
            # calibration pattern) want it raw, before any mapping.
            self.source.set_raw_input(msg["buttons"], msg["accel"])
            if self.cfg.debug_input:
                print("input seq=%d btn=%04x accel=%s state=%d" % (
                    msg["seq"], msg["buttons"], msg["accel"], self.state))

        elif ptype == P.PKT_PING:
            t_client = int.from_bytes(data[4:8], "little")
            self.sock.sendto(P.pack_pong(c.session, t_client, now_ms()), addr)

        elif ptype == P.PKT_BYE:
            print("client said goodbye")
            self.release_all_keys()
            self.client = None

    def on_ack(self, frame_id):
        """The client has fully decoded `frame_id`, so that is now what its
        screen shows, and everything older can be forgotten."""
        if frame_id in self.pending:
            self.ref_frame = self.pending.pop(frame_id)
        for fid in [f for f in self.pending if not newer(f, frame_id)]:
            del self.pending[fid]

        sent = self.sent_times.pop(frame_id, None)
        if sent is not None:
            latency = (now_ms() - sent) & 0xFFFFFFFF
            if latency < 5000:
                self.ack_count += 1
                self.ack_sum += latency
                self.ack_max = max(self.ack_max, latency)
                self.ack_ewma = (latency if self.ack_ewma is None
                                 else 0.8 * self.ack_ewma + 0.2 * latency)
                self.ack_misses = 0      # the link is answering again
        for fid in [f for f in self.sent_times if not newer(f, frame_id)]:
            del self.sent_times[fid]

    # --- input ------------------------------------------------------------

    def release_all_keys(self):
        for key in self.held_keys:
            self.source.send_key(0, key)
        self.held_keys = set()

    def check_profile_swap(self):
        """When the kiosk starts a different game, forget the keys the last
        one had held -- they mean something else now, or nothing."""
        profile = self.active_profile()
        if profile is not self._last_profile:
            self._last_profile = profile
            if self.held_keys:
                self.held_keys = set()
            if profile is not None:
                profile.reset()

    def apply_input(self, now):
        c = self.client
        if not c:
            return

        # A device that walks out of WiFi range must not leave DOOM sprinting
        # into a wall.
        if now - c.last_input_ms > self.cfg.input_timeout_ms:
            if self.held_keys:
                print("input timeout, releasing keys")
                self.release_all_keys()
            return

        profile = self.active_profile()
        want = profile.held_keys(c.buttons, c.accel, self.state, now)

        if self.cfg.debug_keys and want != self.held_keys:
            # Only transitions, with the tilt values that caused them. Unlike
            # --debug-input this is a few lines a second, so it can be left on
            # while actually playing -- which is the only way to see whether
            # the controls are firing when you meant them to.
            changed = " ".join(
                sorted(["+" + key_label(k) for k in want - self.held_keys] +
                       ["-" + key_label(k) for k in self.held_keys - want]))
            print("keys %-22s turn=%+5d move=%+5d raw=%s state=%d" % (
                changed, getattr(profile, "last_turn", 0),
                getattr(profile, "last_move", 0), c.accel, self.state))

        for key in want - self.held_keys:
            self.source.send_key(1, key)
        for key in self.held_keys - want:
            self.source.send_key(0, key)
        self.held_keys = want

    # --- frames -----------------------------------------------------------

    def send_update(self, rects):
        payload = P.pack_update(rects, self.state)
        try:
            frags = P.pack_frame_fragments(self.client.session, self.frame_id,
                                           payload, self.cfg.frag)
        except ValueError as exc:
            print("dropping frame: %s" % exc)
            return

        # Send the first fragment now and queue the rest, spaced out.
        #
        # Measured on the AtomS3R: two datagrams sent back to back produce
        # exactly one received. Its socket holds a single datagram, and the
        # second arrives microseconds later with nowhere to go -- so every
        # update needing two fragments failed forever while every update that
        # fit in one worked. Spacing them by a few milliseconds gives the
        # device time to drain one before the next lands.
        if self.cfg.frag_gap_ms < 0:
            # Unpaced: everything at once. Right for a client that can absorb a
            # burst (a PC), wrong for one whose socket holds one datagram.
            for pkt in frags:
                try:
                    self.sock.sendto(pkt, self.client.addr)
                except OSError:
                    return
            self.tx_queue = []
        else:
            try:
                self.sock.sendto(frags[0], self.client.addr)
            except OSError:
                return
            self.tx_queue = frags[1:]
        self.last_frag_ms = now_ms()
        self.last_sent_id = self.frame_id
        self.sent_times[self.frame_id] = now_ms()
        while len(self.sent_times) > 16:
            del self.sent_times[min(self.sent_times)]
        self.frame_id = (self.frame_id + 1) & 0xFFFF
        self.last_sent_ms = now_ms()

    def pump_tx(self, now):
        """Release one queued fragment when its spacing has elapsed."""
        if not self.tx_queue or not self.client:
            return
        if now - self.last_frag_ms < self.cfg.frag_gap_ms:
            return
        pkt = self.tx_queue.pop(0)
        try:
            self.sock.sendto(pkt, self.client.addr)
        except OSError:
            self.tx_queue = []
            return
        self.last_frag_ms = now

    def ack_timeout(self):
        """How long to wait on an acknowledgement before giving up on it.

        Fixed if configured; otherwise three times the smoothed round trip,
        clamped. Three is enough headroom that normal jitter does not trip it,
        while keeping the freeze after a genuinely lost update proportional to
        the link rather than to a constant picked for the worst case.
        """
        if self.cfg.ack_timeout_ms:
            base = self.cfg.ack_timeout_ms
        elif self.ack_ewma is None:
            base = self.cfg.ack_min_ms
        else:
            base = max(self.cfg.ack_min_ms, 3.0 * self.ack_ewma)

        # Back off while acknowledgements keep failing to arrive. Without this
        # a client that has stopped acking altogether gets sent a fresh update
        # every 80ms forever, which is precisely the flooding flow control
        # exists to prevent. Resets on the first ack.
        return int(min(self.cfg.ack_max_ms, base * (1 << min(self.ack_misses, 3))))

    def in_flight(self):
        """Updates sent but not yet reported complete by the client."""
        if self.last_sent_id is None:
            return 0
        gap = (self.last_sent_id - self.client.last_frame_id) & 0xFFFF
        # A client that is somehow ahead (restart, wrap) counts as caught up.
        return 0 if gap > 0x7FFF else gap

    def maybe_send_frame(self, now):
        """Returns False when flow control blocked this attempt.

        The caller uses that to retry promptly instead of burning the whole
        frame slot: waiting a full period for the next slot quantises the real
        frame rate down to the ack timeout, which at 12fps measured 3/s.
        """
        if not self.client or self.latest is None:
            return True

        # Flow control. Checked *before* encoding, deliberately: encoding an
        # update we then drop would advance nothing but waste the work, and
        # the reference frame must only move when the client confirms. A lost
        # ack is covered by the timeout escape.
        # Never start a new update while the previous one is still going out.
        if self.tx_queue:
            return False

        over_window = self.in_flight() >= self.cfg.window
        if over_window and now - self.last_sent_ms < self.ack_timeout():
            self.blocked += 1
            return False
        if over_window:
            # Sending despite an unacknowledged update: the ack was lost, or
            # the client cannot keep up. Either way, wait longer next time.
            self.ack_misses += 1

        cur = self.scaler.scale(self.latest)

        # A keyframe when the client's screen is unknown, and periodically
        # regardless: one 2kB frame every few seconds is a cheap floor under
        # any divergence this scheme could still produce.
        keyframe = (self.ref_frame is None or
                    now - self.last_keyframe_ms >= self.cfg.keyframe_ms)

        rects = self.encoder.encode(cur, None if keyframe else self.ref_frame,
                                    max_bytes=self.cfg.frag if self.cfg.fit_datagram else 0)
        if rects:
            self.send_update(rects)
            self.pending[self.last_sent_id] = cur
            if keyframe:
                self.last_keyframe_ms = now
            # Unbounded only if the client stops acking entirely; keep the
            # newest few so a late ack can still be honoured.
            while len(self.pending) > 8:
                del self.pending[min(self.pending)]
        elif now - self.last_sent_ms >= self.cfg.keepalive_ms:
            # Nothing changed for a while: an empty update tells the client the
            # link is alive, so a static screen is not mistaken for a dead one.
            self.send_update([])

        return True

    # --- loop -------------------------------------------------------------

    def run(self):
        print("listening on %s:%d  %dx%d  %dfps  tile=%d  q%d  auth=%s" % (
            self.cfg.bind, self.cfg.port, self.cfg.width, self.cfg.height,
            self.cfg.fps, self.cfg.tile, self.cfg.quality,
            "token" if self.cfg.token else "NONE"))
        print("source=%s  select=%s  window=%d  keyframe=%dms  frag=%d "
              "gap=%dms fit1=%d" % (
                  getattr(self.source, "name", "?"), self.cfg.select,
                  self.cfg.window, self.cfg.keyframe_ms, self.cfg.frag,
                  self.cfg.frag_gap_ms, int(self.cfg.fit_datagram)))

        describe = getattr(self.active_profile(), "describe", None)
        if describe:
            print(describe())

        src_fd = self.source.fileno()
        if src_fd is not None:
            self.sel.register(src_fd, selectors.EVENT_READ)

        period = 1.0 / self.cfg.fps
        next_frame = time.monotonic()

        while True:
            if not self.source.alive():
                print("source exited")
                return

            # Wake sooner while fragments are waiting, so the spacing
            # between them is the configured gap and not the loop period.
            slice_s = 0.002 if self.tx_queue else 0.005
            timeout = max(0.0, min(slice_s, next_frame - time.monotonic()))
            for key, _ in self.sel.select(timeout):
                if key.fileobj is self.sock:
                    while True:
                        try:
                            data, addr = self.sock.recvfrom(2048)
                        except (BlockingIOError, ConnectionResetError):
                            break
                        self.handle_packet(data, addr)

            frame = self.source.poll()
            if frame is not None:
                self.latest, self.state = frame
                self.sync_geometry()
                self.check_profile_swap()

            now = now_ms()
            self.apply_input(now)
            self.pump_tx(now)

            t = time.monotonic()
            if t >= next_frame:
                # A slot blocked by flow control is retried as soon as the ack
                # could plausibly have landed, not a whole frame period later:
                # the client's acknowledgement rate should set the frame rate,
                # not get rounded down to the slot grid.
                next_frame = t + (period if self.maybe_send_frame(now) else 0.004)

            if now - self.last_stats_ms >= self.cfg.stats_ms:
                elapsed = time.monotonic() - self.stats_t0
                if self.encoder and self.client:
                    line = self.encoder.stats_line(elapsed)
                    if line:
                        print(line)
                    # The other half of the picture: how often the client was
                    # the thing holding the stream up, and by how long. A high
                    # blocked count with a high ack latency means the device is
                    # the bottleneck; a low one means the game is.
                    print("link: blocked %d, acks %d, ack latency avg %d ms "
                          "max %d ms, timeout %d ms, in flight %d" % (
                              self.blocked, self.ack_count,
                              self.ack_sum // max(1, self.ack_count),
                              self.ack_max, self.ack_timeout(),
                              self.in_flight()))
                self.blocked = self.ack_count = self.ack_sum = self.ack_max = 0
                self.last_stats_ms = now
                self.stats_t0 = time.monotonic()

    def close(self):
        if self.client:
            try:
                self.sock.sendto(P.pack_kick(self.client.session, 1),
                                 self.client.addr)
            except OSError:
                pass
        self.sel.close()
        self.sock.close()
