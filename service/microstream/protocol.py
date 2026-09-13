"""MDOOM/2 wire protocol: rectangle updates, VNC-style, sized for a 128x128 screen.

The shape is lifted from RFB's FramebufferUpdate: a frame is a list of
rectangles, each independently encoded, and unchanged parts of the screen are
simply absent. What is *not* lifted is RFB itself -- no negotiation, no palette
filters, no persistent zlib streams, because a MicroPython client cannot afford
to decode those and a 128x128 screen does not need them.

Clients advertise what they can decode (`caps`) and the server only uses those
encodings, so a device that turns out to lack zlib is a one-bit change, not a
protocol change.
"""

import struct
import zlib

MAGIC = 0x4D
PROTO_VER = 2

# client -> server
PKT_HELLO = 0x01
PKT_INPUT = 0x02
PKT_PING = 0x03
PKT_BYE = 0x04

# server -> client
PKT_HELLO_ACK = 0x81
PKT_FRAME = 0x82
PKT_PONG = 0x83
PKT_KICK = 0x85

HDR_LEN = 4
FRAME_HDR_LEN = 11
UPDATE_HDR_LEN = 4
RECT_HDR_LEN = 11

STATUS_OK = 0
STATUS_BAD_TOKEN = 1
STATUS_BUSY = 2
STATUS_BAD_VERSION = 3
STATUS_TEXT = {
    STATUS_OK: "ok",
    STATUS_BAD_TOKEN: "bad token",
    STATUS_BUSY: "server busy",
    STATUS_BAD_VERSION: "protocol version mismatch",
}

FLAG_LAST_FRAG = 0x01

# Rect encodings. SOLID and RAW565 are mandatory: every client can fill a
# rectangle and blit raw pixels.
ENC_SOLID = 0
ENC_RAW565 = 1
ENC_DEFLATE565 = 2
ENC_JPEG = 3

ENC_NAMES = {
    ENC_SOLID: "solid",
    ENC_RAW565: "raw",
    ENC_DEFLATE565: "deflate",
    ENC_JPEG: "jpeg",
}

# Optional capabilities a client advertises in HELLO.
CAP_JPEG = 1 << 0
CAP_DEFLATE = 1 << 1
CAP_ALL = CAP_JPEG | CAP_DEFLATE

# Game state, passed through from the source so input profiles can adapt.
STATE_LEVEL = 0
STATE_MENU = 1
STATE_DEAD = 2
STATE_NONLEVEL = 3

# Buttons. A terminal with only one button sets bit 0; the rest exist for the
# GPIO joystick and for desktop clients.
BTN_FIRE = 1 << 0
BTN_USE = 1 << 1
BTN_UP = 1 << 2
BTN_DOWN = 1 << 3
BTN_LEFT = 1 << 4
BTN_RIGHT = 1 << 5
BTN_STRAFE_L = 1 << 6
BTN_STRAFE_R = 1 << 7
BTN_ENTER = 1 << 8
BTN_ESCAPE = 1 << 9
BTN_RUN = 1 << 10
BTN_MAP = 1 << 11
BTN_PAUSE = 1 << 12
BTN_YES = 1 << 13
BTN_WEAPON_PREV = 1 << 14
BTN_WEAPON_NEXT = 1 << 15


def header(pkt_type, session=0):
    return struct.pack("<BBH", MAGIC, pkt_type, session)


def packet_type(data):
    if len(data) < HDR_LEN or data[0] != MAGIC:
        return None
    return data[1]


def packet_session(data):
    return struct.unpack_from("<H", data, 2)[0]


# --- client -> server ----------------------------------------------------

def pack_hello(token="", width=128, height=128, caps=CAP_ALL):
    tok = token.encode("utf-8")[:32]
    return (header(PKT_HELLO, 0)
            + struct.pack("<BBHHB", PROTO_VER, caps, width, height, len(tok))
            + tok)


def parse_hello(data):
    if len(data) < 11:
        return None
    ver, caps, width, height, token_len = struct.unpack_from("<BBHHB", data, 4)
    if len(data) < 11 + token_len:
        return None
    return {
        "ver": ver,
        "caps": caps,
        "width": width,
        "height": height,
        "token": data[11:11 + token_len].decode("utf-8", "replace"),
    }


def pack_input(session, seq, buttons, ax, ay, az, last_frame_id=0):
    return header(PKT_INPUT, session) + struct.pack(
        "<IHhhhH", seq & 0xFFFFFFFF, buttons, ax, ay, az, last_frame_id)


def parse_input(data):
    if len(data) < 18:
        return None
    seq, buttons, ax, ay, az, last_frame = struct.unpack_from("<IHhhhH", data, 4)
    return {"seq": seq, "buttons": buttons, "accel": (ax, ay, az),
            "last_frame_id": last_frame}


def pack_ping(session, t_client_ms):
    return header(PKT_PING, session) + struct.pack("<I", t_client_ms & 0xFFFFFFFF)


def pack_bye(session):
    return header(PKT_BYE, session)


# --- server -> client ----------------------------------------------------

def pack_hello_ack(session, status, width, height, max_payload, tile):
    return header(PKT_HELLO_ACK, session) + struct.pack(
        "<BBHHHB", PROTO_VER, status, width, height, max_payload, tile)


HELLO_ACK_LEN = HDR_LEN + 9


def parse_hello_ack(data):
    if len(data) < HELLO_ACK_LEN:
        return None
    session = packet_session(data)
    ver, status, width, height, max_payload, tile = struct.unpack_from("<BBHHHB", data, 4)
    return {"session": session, "ver": ver, "status": status, "width": width,
            "height": height, "max_payload": max_payload, "tile": tile}


def pack_pong(session, t_client_ms, t_server_ms):
    return header(PKT_PONG, session) + struct.pack(
        "<II", t_client_ms & 0xFFFFFFFF, t_server_ms & 0xFFFFFFFF)


def pack_kick(session, reason=0):
    return header(PKT_KICK, session) + struct.pack("<B", reason)


# --- frame updates -------------------------------------------------------

def pack_update(rects, state=STATE_LEVEL):
    """Serialise one screen update: a list of (x, y, w, h, enc, data)."""
    out = bytearray(struct.pack("<BBH", len(rects), state, 0))
    for x, y, w, h, enc, data in rects:
        out += struct.pack("<HHHHBH", x, y, w, h, enc, len(data))
        out += data
    return bytes(out)


def parse_update(data):
    """Inverse of pack_update. Returns (rects, state)."""
    if len(data) < UPDATE_HDR_LEN:
        return [], STATE_LEVEL
    count, state, _ = struct.unpack_from("<BBH", data, 0)
    off = UPDATE_HDR_LEN
    rects = []
    for _ in range(count):
        if off + RECT_HDR_LEN > len(data):
            break
        x, y, w, h, enc, length = struct.unpack_from("<HHHHBH", data, off)
        off += RECT_HDR_LEN
        if off + length > len(data):
            break
        rects.append((x, y, w, h, enc, data[off:off + length]))
        off += length
    return rects, state


def pack_frame_fragments(session, frame_id, payload, frag_payload=1024):
    frags = max(1, (len(payload) + frag_payload - 1) // frag_payload)
    if frags > 255:
        raise ValueError("update needs %d fragments, protocol allows 255" % frags)

    out = []
    for i in range(frags):
        chunk = payload[i * frag_payload:(i + 1) * frag_payload]
        flags = FLAG_LAST_FRAG if i == frags - 1 else 0
        out.append(header(PKT_FRAME, session)
                   + struct.pack("<HBBBH", frame_id & 0xFFFF, i, frags, flags,
                                 len(chunk))
                   + chunk)
    return out


def parse_frame(data):
    if len(data) < FRAME_HDR_LEN:
        return None
    frame_id, frag_idx, frag_count, flags, length = struct.unpack_from("<HBBBH", data, 4)
    payload = data[FRAME_HDR_LEN:FRAME_HDR_LEN + length]
    if len(payload) != length:
        return None
    return {"frame_id": frame_id, "frag_idx": frag_idx, "frag_count": frag_count,
            "flags": flags, "payload": payload}


# --- pixel helpers shared by both ends -----------------------------------

def rgb565_bytes_to_rgb888(data, width, height):
    """Big-endian RGB565 -> raw RGB bytes, for desktop clients."""
    out = bytearray(width * height * 3)
    for i in range(width * height):
        v = (data[i * 2] << 8) | data[i * 2 + 1]
        out[i * 3] = ((v >> 11) & 0x1F) << 3
        out[i * 3 + 1] = ((v >> 5) & 0x3F) << 2
        out[i * 3 + 2] = (v & 0x1F) << 3
    return bytes(out)


def inflate(data):
    return zlib.decompress(data)
