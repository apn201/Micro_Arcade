"""micro-doom terminal firmware for the M5Stack AtomS3R (UIFlow2 MicroPython).

Speaks MDOOM/2: sends raw button bits and raw accelerometer counts up at 30Hz,
draws the rectangles that come back. It knows nothing about DOOM -- no tilt
thresholds, no key mapping, no game state -- which is what makes the control
feel tunable from the server without reflashing.

Measured on this device with caps_test.py (UIFlow2 MicroPython v1.27):

    drawJpg, 128x128, decode + blit   10.94 ms
    drawRawBuf, 128x32, blit           3.68 ms
    deflate, 1066 -> 8192 bytes       15.25 ms
    free heap                          8.29 MB

Those numbers are why this advertises JPEG *and* deflate but will mostly be
sent JPEG: the server prices encodings by what they cost here, and a JPEG
decode is about seven times cheaper per pixel than a zlib inflate. Both paths
are implemented anyway -- the server decides, and a rect can arrive in any
encoding the caps allow.

Copy config.example.py to config.py first.
"""

import gc
import network
import socket
import struct
import time

import M5
from M5 import *

try:
    import config
except ImportError:
    raise SystemExit("copy config.example.py to config.py and fill it in")


# --- protocol (mirrors service/microstream/protocol.py) ------------------

MAGIC = 0x4D
PROTO_VER = 2

PKT_HELLO = 0x01
PKT_INPUT = 0x02
PKT_PING = 0x03
PKT_BYE = 0x04
PKT_HELLO_ACK = 0x81
PKT_FRAME = 0x82
PKT_PONG = 0x83
PKT_KICK = 0x85

ENC_SOLID = 0
ENC_RAW565 = 1
ENC_DEFLATE565 = 2
ENC_JPEG = 3

CAP_JPEG = 1 << 0
CAP_DEFLATE = 1 << 1

BTN_FIRE = 1 << 0
BTN_USE = 1 << 1
BTN_UP = 1 << 2
BTN_DOWN = 1 << 3
BTN_LEFT = 1 << 4
BTN_RIGHT = 1 << 5

FRAME_HDR_LEN = 11
UPDATE_HDR_LEN = 4
RECT_HDR_LEN = 11

HELLO_RETRY_MS = 500
STALE_MS = 1000          # no complete update: show a corner marker
LINK_TIMEOUT_MS = 5000   # no packets at all: really reconnect
PING_PERIOD_MS = 250     # often enough to catch a latency spike, 12 bytes each
STALL_REPORT_MS = 200    # a gap this long is visible as a judder; count it


# --- deflate, if this firmware has it -----------------------------------

def _find_inflate():
    """Returns (fn, name) or (None, None). Set by caps, not assumed."""
    try:
        import deflate
        import io as _io

        def fn(data):
            return deflate.DeflateIO(_io.BytesIO(data), deflate.ZLIB).read()

        return fn, "deflate"
    except ImportError:
        pass
    try:
        import zlib

        return zlib.decompress, "zlib"
    except ImportError:
        return None, None


inflate, inflate_name = _find_inflate()


# --- helpers -------------------------------------------------------------

def status(line1, line2=""):
    """Screen output for everything that is not a game frame.

    Wrapped in try/except because the text API varies between UIFlow2 builds,
    and a cabinet that cannot draw a status line should still play.
    """
    try:
        M5.Display.fillScreen(0x000000)
        M5.Display.setTextSize(1)
        M5.Display.setCursor(4, 50)
        M5.Display.print(line1, 0xFF6600)
        if line2:
            M5.Display.setCursor(4, 66)
            M5.Display.print(line2, 0x888888)
    except Exception:
        pass
    print(line1, line2)


class Piezo:
    """Local sound effects. Silent unless a pin is configured."""

    def __init__(self, pin):
        self.pwm = None
        self.off_at = 0
        if pin is None:
            return
        try:
            from machine import Pin, PWM
            self.pwm = PWM(Pin(pin), freq=1000, duty=0)
        except Exception as exc:
            print("piezo disabled:", exc)
            self.pwm = None

    def beep(self, freq, ms):
        if not self.pwm:
            return
        try:
            self.pwm.freq(freq)
            self.pwm.duty(400)
            self.off_at = time.ticks_add(time.ticks_ms(), ms)
        except Exception:
            self.pwm = None

    def update(self):
        if self.pwm and self.off_at and time.ticks_diff(time.ticks_ms(), self.off_at) >= 0:
            try:
                self.pwm.duty(0)
            except Exception:
                pass
            self.off_at = 0


class Inputs:
    """Onboard button, optional GPIO joystick, IMU."""

    def __init__(self):
        self.pins = []
        try:
            from machine import Pin
            for pin, bit in ((config.PIN_UP, BTN_UP),
                             (config.PIN_DOWN, BTN_DOWN),
                             (config.PIN_LEFT, BTN_LEFT),
                             (config.PIN_RIGHT, BTN_RIGHT),
                             (config.PIN_FIRE, BTN_FIRE),
                             (config.PIN_USE, BTN_USE)):
                if pin is not None:
                    self.pins.append((Pin(pin, Pin.IN, Pin.PULL_UP), bit))
        except Exception as exc:
            print("gpio inputs disabled:", exc)

    def read(self):
        """(buttons, ax, ay, az) with acceleration in milli-g."""
        M5.update()

        buttons = 0
        try:
            if BtnA.isPressed():
                buttons |= BTN_FIRE
        except Exception:
            pass

        # Switches are wired active-low against the internal pull-ups.
        for pin, bit in self.pins:
            if not pin.value():
                buttons |= bit

        try:
            ax, ay, az = Imu.getAccel()
        except Exception:
            ax = ay = az = 0.0

        return buttons, int(ax * 1000.0), int(ay * 1000.0), int(az * 1000.0)


# --- the terminal --------------------------------------------------------

class Terminal:
    def __init__(self):
        self.width = config.WIDTH
        self.height = config.HEIGHT

        self.caps = CAP_JPEG
        if inflate is not None:
            self.caps |= CAP_DEFLATE

        self.sock = None
        self.recv_packet = None
        self.recv_kind = "?"
        self.session = 0
        self.connected = False
        self.seq = 0
        self.last_frame_id = 0
        self.state = 0

        # Allocated once. There is 8MB of heap, but allocating per frame would
        # have the GC running in the middle of every update.
        self.buf = bytearray(40000)
        self.mv = memoryview(self.buf)
        self.rx = bytearray(1500)
        self.rx_mv = memoryview(self.rx)

        self.frag_size = 1024
        self.asm_id = -1
        self.asm_mask = 0
        self.asm_count = 0
        self.asm_len = 0

        self.last_hello = 0
        self.last_input = 0
        self.last_ping = 0
        self.last_frame_ms = 0      # last *complete* update
        self.last_rx_ms = 0         # last packet of any kind from the server
        self.stale = False

        # Diagnostics. These exist to answer one question: when the picture
        # judders, is the link dropping fragments or is the device too slow?
        # Those have opposite fixes, and guessing between them wastes hours.
        self.frames = 0          # complete updates drawn
        self.bytes = 0
        self.rects = 0
        self.draw_us = 0         # time inside drawJpg/drawRawBuf/fillRect
        self.frags = 0           # fragments received
        self.abandoned = 0       # updates dropped half-assembled
        self.gap_sum = 0         # time between complete updates
        self.gap_max = 0
        self.stalls = 0          # gaps over STALL_REPORT_MS
        self.rtt_n = 0           # ping/pong round trips, measured here
        self.rtt_sum = 0
        self.rtt_max = 0
        self.stats_at = 0

        self.inputs = Inputs()
        self.piezo = Piezo(config.PIN_PIEZO)
        self.prev_buttons = 0

    # --- connection ------------------------------------------------------

    def wifi(self):
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        if wlan.isconnected():
            return wlan

        status("WIFI", config.WIFI_SSID[:18])
        wlan.connect(config.WIFI_SSID, config.WIFI_PASS)
        for _ in range(60):
            if wlan.isconnected():
                break
            time.sleep_ms(250)

        if not wlan.isconnected():
            status("NO WIFI", "retrying")
            return None

        # Disable WiFi modem sleep. This is the single biggest source of the
        # "smooth for a second, then frozen for half a second" behaviour: by
        # default the ESP32 powers the radio down between DTIM beacons, so a
        # downstream packet can wait 100-300ms for the next wake-up. For a
        # stream that only keeps one or two updates in flight, that stall is
        # the whole frame budget several times over -- and it happens even
        # when the picture is nearly static, which is exactly when it is most
        # visible.
        try:
            wlan.config(pm=network.WLAN.PM_NONE)
            print("wifi: power save disabled")
        except Exception as exc:
            try:
                wlan.config(ps_mode=network.WIFI_PS_NONE)
                print("wifi: power save disabled (ps_mode)")
            except Exception:
                print("wifi: could not disable power save:", exc)

        print("wifi:", wlan.ifconfig())
        return wlan

    def open_socket(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass

        addr = socket.getaddrinfo(config.SERVER_HOST, config.SERVER_PORT)[0][-1]
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # A connected UDP socket gives us send()/recv() and avoids the
        # per-packet address tuple recvfrom() would allocate.
        self.sock.connect(addr)
        self.sock.setblocking(False)
        try:
            # While a JPEG is decoding (~11ms) nothing is being received, so
            # the next frame's fragments have to queue somewhere. lwIP's
            # default UDP queue is a couple of packets deep, which is not
            # enough; not every build allows changing it.
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16384)
        except Exception as exc:
            print("SO_RCVBUF not settable:", exc)
        self.recv_packet = self._pick_recv()

    def _pick_recv(self):
        """Pick a receive call this firmware actually has.

        UIFlow2's socket has no recv_into(), other MicroPython builds do, and
        some only offer the stream readinto(). Probing beats assuming: all
        three fill self.rx and return a length, and only the last one copies.
        """
        sock = self.sock
        rx, rx_mv = self.rx, self.rx_mv

        if hasattr(sock, "recv_into"):
            self.recv_kind = "recv_into"

            def recv_packet():
                return sock.recv_into(rx) or 0

        elif hasattr(sock, "readinto"):
            self.recv_kind = "readinto"

            def recv_packet():
                return sock.readinto(rx) or 0

        elif hasattr(sock, "recv"):
            self.recv_kind = "recv"
            limit = len(rx)

            def recv_packet():
                data = sock.recv(limit)
                n = len(data)
                if n:
                    rx_mv[0:n] = data
                return n

        elif hasattr(sock, "recvfrom"):
            self.recv_kind = "recvfrom"
            limit = len(rx)

            def recv_packet():
                data, _ = sock.recvfrom(limit)
                n = len(data)
                if n:
                    rx_mv[0:n] = data
                return n

        else:
            raise RuntimeError("socket has no usable receive call")

        print("socket receive:", self.recv_kind)
        return recv_packet

    def send_hello(self):
        token = config.TOKEN.encode()[:32]
        pkt = (struct.pack("<BBH", MAGIC, PKT_HELLO, 0)
               + struct.pack("<BBHHB", PROTO_VER, self.caps,
                             self.width, self.height, len(token))
               + token)
        try:
            self.sock.send(pkt)
        except OSError:
            pass
        self.last_hello = time.ticks_ms()

    def on_hello_ack(self, n):
        if n < 13:
            return
        session = struct.unpack_from("<H", self.rx, 2)[0]
        ver, st, w, h, max_payload, tile = struct.unpack_from("<BBHHHB", self.rx, 4)

        if st != 0:
            status("REJECTED", "status %d" % st)
            self.connected = False
            time.sleep_ms(1000)
            return

        self.session = session
        self.width, self.height = w, h
        self.frag_size = max_payload or 1024

        need = w * h * 2 + 512
        if len(self.buf) < need:
            self.buf = bytearray(need)
            self.mv = memoryview(self.buf)

        self.connected = True
        self.asm_id = -1
        self.last_frame_ms = time.ticks_ms()
        self.last_rx_ms = self.last_frame_ms
        self.stale = False
        status("CONNECTED", "%dx%d tile %d" % (w, h, tile))
        print("caps advertised: jpeg%s (inflate: %s, receive: %s)" % (
            " deflate" if self.caps & CAP_DEFLATE else "", inflate_name,
            self.recv_kind))

    # --- frames ----------------------------------------------------------

    def on_fragment(self, n):
        if n < FRAME_HDR_LEN:
            return
        frame_id, idx, count, flags, length = struct.unpack_from("<HBBBH", self.rx, 4)
        if n < FRAME_HDR_LEN + length or count == 0:
            return

        self.frags += 1

        if frame_id != self.asm_id:
            # Only a *newer* update may displace the one being assembled. A
            # late fragment of an older update arriving mid-reassembly would
            # otherwise reset it, and with two fragments per frame in flight
            # that can stop anything from ever completing.
            if self.asm_id >= 0 and ((frame_id - self.asm_id) & 0xFFFF) >= 0x8000:
                return
            if self.asm_id >= 0:
                # Displacing an incomplete update means a fragment of it was
                # lost. This counter is the difference between "the link is
                # dropping packets" and "the device cannot keep up".
                self.abandoned += 1
            self.asm_id = frame_id
            self.asm_mask = 0
            self.asm_count = count
            self.asm_len = 0

        off = idx * self.frag_size
        if off + length > len(self.buf):
            return

        self.mv[off:off + length] = self.rx_mv[FRAME_HDR_LEN:FRAME_HDR_LEN + length]
        self.asm_mask |= 1 << idx
        if off + length > self.asm_len:
            self.asm_len = off + length

        if self.asm_mask != (1 << self.asm_count) - 1:
            return

        self.asm_id = -1
        self.last_frame_id = frame_id

        now = time.ticks_ms()
        gap = time.ticks_diff(now, self.last_frame_ms)
        if self.last_frame_ms and 0 < gap < 10000:
            self.gap_sum += gap
            if gap > self.gap_max:
                self.gap_max = gap
            if gap > STALL_REPORT_MS:
                self.stalls += 1
        self.last_frame_ms = now

        self.bytes += self.asm_len
        self.apply_update(self.asm_len)

        # Acknowledge immediately rather than waiting for the next input tick.
        # The server will not send the following update until this one is
        # acknowledged, so up to 33ms of ack latency would come straight off
        # the frame rate.
        self.last_input = time.ticks_ms()
        self.send_input()

    def apply_update(self, length):
        if length < UPDATE_HDR_LEN:
            return
        count = self.buf[0]
        self.state = self.buf[1]

        if count == 0:
            return          # keepalive: the screen is unchanged, link is alive

        t0 = time.ticks_us()
        off = UPDATE_HDR_LEN

        # One SPI transaction for the whole update rather than one per rect.
        try:
            M5.Display.startWrite()
        except Exception:
            pass

        for _ in range(count):
            if off + RECT_HDR_LEN > length:
                break
            x, y, w, h, enc, n = struct.unpack_from("<HHHHBH", self.buf, off)
            off += RECT_HDR_LEN
            if off + n > length:
                break
            try:
                self.draw_rect(x, y, w, h, enc, off, n)
            except Exception as exc:
                print("rect %d,%d %dx%d enc %d failed: %s" % (x, y, w, h, enc, exc))
            off += n
            self.rects += 1

        try:
            M5.Display.endWrite()
        except Exception:
            pass

        self.frames += 1
        self.draw_us += time.ticks_diff(time.ticks_us(), t0)

    def draw_rect(self, x, y, w, h, enc, off, n):
        if enc == ENC_JPEG:
            # Measured: this firmware accepts a memoryview slice, so the
            # payload goes to the decoder without a copy.
            M5.Display.drawJpg(self.mv[off:off + n], x, y)

        elif enc == ENC_SOLID:
            v = (self.buf[off] << 8) | self.buf[off + 1]
            rgb = ((((v >> 11) & 0x1F) << 19) |
                   (((v >> 5) & 0x3F) << 10) |
                   ((v & 0x1F) << 3))
            M5.Display.fillRect(x, y, w, h, rgb)

        elif enc == ENC_RAW565:
            M5.Display.drawRawBuf(self.mv[off:off + n], x, y, w, h, n, False)

        elif enc == ENC_DEFLATE565:
            if inflate is None:
                return
            raw = inflate(bytes(self.mv[off:off + n]))
            M5.Display.drawRawBuf(raw, x, y, w, h, len(raw), False)

    # --- input -----------------------------------------------------------

    def send_input(self):
        buttons, ax, ay, az = self.inputs.read()

        if (buttons & BTN_FIRE) and not (self.prev_buttons & BTN_FIRE):
            self.piezo.beep(config.PIEZO_FIRE_HZ, config.PIEZO_FIRE_MS)
        self.prev_buttons = buttons

        self.seq += 1
        pkt = (struct.pack("<BBH", MAGIC, PKT_INPUT, self.session)
               + struct.pack("<IHhhhH", self.seq, buttons, ax, ay, az,
                             self.last_frame_id))
        try:
            self.sock.send(pkt)
        except OSError:
            pass

    # --- loop ------------------------------------------------------------

    def pump_rx(self):
        while True:
            try:
                n = self.recv_packet()
            except OSError:
                return                  # EAGAIN: nothing left to read
            if not n:
                return                  # readinto() reports "no data" as None
            if n < 4 or self.rx[0] != MAGIC:
                continue                # junk packet, keep draining

            # Any valid packet means the link is alive, even if the update it
            # belongs to never completes.
            self.last_rx_ms = time.ticks_ms()

            ptype = self.rx[1]
            if ptype == PKT_FRAME:
                if self.connected and struct.unpack_from("<H", self.rx, 2)[0] == self.session:
                    self.on_fragment(n)
            elif ptype == PKT_HELLO_ACK:
                self.on_hello_ack(n)
            elif ptype == PKT_PONG:
                # Round trip measured on the device itself. With WiFi power
                # save on this sits in the hundreds of milliseconds and spikes;
                # with it off it should be single-digit on a quiet LAN. It is
                # the number that says whether a stall is the radio.
                if n >= 8:
                    sent = struct.unpack_from("<I", self.rx, 4)[0]
                    rtt = time.ticks_diff(time.ticks_ms(), sent)
                    if 0 <= rtt < 5000:
                        self.rtt_n += 1
                        self.rtt_sum += rtt
                        if rtt > self.rtt_max:
                            self.rtt_max = rtt

            elif ptype == PKT_KICK:
                self.connected = False
                status("DISCONNECTED", "server kicked")

    def mark_stale(self, stale):
        """Two pixels of warning instead of a blanked screen."""
        try:
            M5.Display.fillRect(0, 0, 3, 3, 0xFF0000 if stale else 0x000000)
        except Exception:
            pass

    def report(self, now):
        """One line that says where the time went.

        Read it like this:
          draw   time inside the display calls. Near 11ms means the device is
                 working as measured and is not the problem.
          gap    time between complete updates. avg is the real frame period;
                 max and stalls are the judder you can actually feel.
          lost   updates abandoned half-assembled = fragments that never
                 arrived. Non-zero here means the link, not the device.
        """
        el = time.ticks_diff(now, self.stats_at) / 1000.0
        if el <= 0:
            return

        # gc.mem_free() is MicroPython-only; the PC harness runs this same file.
        try:
            heap_k = gc.mem_free() // 1024
        except AttributeError:
            heap_k = -1

        if self.frames:
            print("rx %.1f fps | draw %.1f ms | gap avg %d max %d ms | "
                  "stalls>%dms %d | lost %d | rtt avg %d max %d ms | "
                  "%.1f kB/f | heap %dk" % (
                      self.frames / el,
                      self.draw_us / 1000.0 / self.frames,
                      self.gap_sum // max(1, self.frames),
                      self.gap_max,
                      STALL_REPORT_MS, self.stalls,
                      self.abandoned,
                      self.rtt_sum // max(1, self.rtt_n),
                      self.rtt_max,
                      self.bytes / 1024.0 / self.frames,
                      heap_k))
        else:
            print("no complete updates in %.0fs (connected=%s, frags %d, lost %d, "
                  "rtt max %d ms)"
                  % (el, self.connected, self.frags, self.abandoned, self.rtt_max))

        self.stats_at = now
        self.frames = self.bytes = self.rects = 0
        self.draw_us = self.frags = self.abandoned = 0
        self.gap_sum = self.gap_max = self.stalls = 0
        self.rtt_n = self.rtt_sum = self.rtt_max = 0
        try:
            gc.collect()
        except Exception:
            pass

    def run(self):
        M5.begin()
        try:
            M5.Display.setRotation(config.ROTATION)
        except Exception:
            pass
        status("MICRO DOOM", "booting")

        while not self.wifi():
            time.sleep_ms(2000)

        self.open_socket()
        status("SERVER", "%s:%d" % (config.SERVER_HOST, config.SERVER_PORT))
        self.send_hello()
        self.stats_at = time.ticks_ms()

        while True:
            now = time.ticks_ms()

            self.pump_rx()
            self.piezo.update()

            if not self.connected:
                if time.ticks_diff(now, self.last_hello) >= HELLO_RETRY_MS:
                    self.send_hello()
            else:
                # A stalled stream and a dead link are different problems.
                # Losing fragments for a moment must not cost the player the
                # picture: blanking the screen to announce it is far worse
                # than showing a slightly old frame.
                stale = time.ticks_diff(now, self.last_frame_ms) > STALE_MS
                if stale != self.stale:
                    self.stale = stale
                    self.mark_stale(stale)

                if time.ticks_diff(now, self.last_rx_ms) > LINK_TIMEOUT_MS:
                    # Nothing at all from the server, not even a keepalive.
                    self.connected = False
                    self.stale = False
                    status("SIGNAL LOST", "reconnecting")
                    if not network.WLAN(network.STA_IF).isconnected():
                        self.wifi()
                        self.open_socket()
                    self.send_hello()

                if time.ticks_diff(now, self.last_input) >= config.INPUT_PERIOD_MS:
                    self.last_input = now
                    self.send_input()

                if time.ticks_diff(now, self.last_ping) >= PING_PERIOD_MS:
                    self.last_ping = now
                    try:
                        self.sock.send(struct.pack("<BBHI", MAGIC, PKT_PING,
                                                   self.session, now & 0xFFFFFFFF))
                    except OSError:
                        pass

            if config.DEBUG and time.ticks_diff(now, self.stats_at) >= 5000:
                self.report(now)

            # Yield briefly: without this the socket polling starves the
            # scheduler and WiFi housekeeping suffers.
            time.sleep_ms(2)


def main():
    term = Terminal()
    while True:
        try:
            term.run()
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            # A cabinet at a show must never need a power cycle.
            print("terminal error:", exc)
            status("ERROR", str(exc)[:18])
            time.sleep_ms(2000)


if __name__ == "__main__":
    main()
