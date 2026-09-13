# MDOOM/2 — the terminal protocol

Transport: **UDP**, client-initiated. All integers **little-endian**.
Implemented by `service/microstream/protocol.py`; the reference decoder is
`service/clients/pc_viewer.py`.

The device is a dumb terminal. It sends raw button bits and raw accelerometer
counts up, and draws the rectangles that come down. It knows nothing about
DOOM, tilt thresholds, or key mappings — all of that lives in the service, so
retuning the controls is a server restart rather than a reflash.

## Why UDP

Frame updates are self-contained, so a lost packet costs one update and never
desynchronises a decoder. TCP would add head-of-line blocking: one retransmit
on a flaky WiFi link stalls every frame queued behind it. Input packets are
absolute state snapshots rather than deltas, so a dropped one self-corrects
33ms later.

NAT works out of the box: the client speaks first and the server replies to the
source address of the HELLO.

## Common header (4 bytes)

| off | type | field   | notes                                      |
|-----|------|---------|--------------------------------------------|
| 0   | u8   | magic   | `0x4D` (`'M'`)                              |
| 1   | u8   | type    | see below                                   |
| 2   | u16  | session | 0 in the client's HELLO; assigned by server |

Bad magic, unknown type or stale session: dropped silently.

## Client → server

### 0x01 HELLO
```
[hdr][u8 proto_ver=2][u8 caps][u16 width][u16 height][u8 token_len][token]
```
`caps` says what the client can *decode*: bit 0 JPEG, bit 1 DEFLATE565. SOLID
and RAW565 are mandatory — every client can fill a rectangle and blit pixels.
The server only ever sends encodings the client advertised, which is what makes
"does this firmware have zlib?" a one-bit question instead of a protocol
change.

Repeated every 500ms until a HELLO_ACK arrives.

### 0x02 INPUT
```
[hdr][u32 seq][u16 buttons][i16 ax][i16 ay][i16 az][u16 last_frame_id]
```
Sent at a fixed rate (default 30Hz), *not* only on change: the server uses
arrival as a liveness signal and releases every key after `input_timeout_ms` of
silence, so a device that walks out of WiFi range stops running into a wall.

`ax/ay/az` are raw, uncalibrated accelerometer readings in milli-g. `seq`
increases monotonically; the server ignores any packet that is not newer than
the last accepted one, because UDP reorders and an older snapshot would undo a
newer one.

Button bits:

| bit | name      | bit | name        |
|-----|-----------|-----|-------------|
| 0   | FIRE      | 8   | ENTER       |
| 1   | USE       | 9   | ESCAPE      |
| 2   | UP        | 10  | RUN         |
| 3   | DOWN      | 11  | MAP         |
| 4   | LEFT      | 12  | PAUSE       |
| 5   | RIGHT     | 13  | YES (`y`)   |
| 6   | STRAFE_L  | 14  | WEAPON_PREV |
| 7   | STRAFE_R  | 15  | WEAPON_NEXT |

An AtomS3R with only the onboard button sets bit 0 and leaves the rest clear.
The movement bits exist for the GPIO joystick, which needs no server change.

### 0x03 PING / 0x04 BYE
```
[hdr][u32 t_client_ms]
[hdr]
```

## Server → client

### 0x81 HELLO_ACK (13 bytes)
```
[hdr][u8 proto_ver][u8 status][u16 width][u16 height][u16 max_payload][u8 tile]
```
`status`: 0 ok, 1 bad token, 2 busy, 3 version mismatch. Non-zero means no
frames will follow.

### 0x82 FRAME
```
[hdr][u16 frame_id][u8 frag_idx][u8 frag_count][u8 flags][u16 payload_len][payload]
```
Header is 11 bytes; `payload_len` is at most `max_payload` (default 1024, sized
to stay under a 1500-byte MTU so the ESP32's lwIP never reassembles IP
fragments). `flags` bit 0 marks the last fragment.

Reassembly rule: keep one in-progress update; when a fragment arrives for a
newer `frame_id`, abandon the old one immediately rather than waiting. A stale
frame is worth less than the latency of holding a newer one back.

`frame_id` wraps at 65536; compare with `(int16_t)(a - b) > 0`.

### The update payload

Once reassembled, a FRAME payload is a list of rectangles — the same shape as
RFB's FramebufferUpdate, minus everything a MicroPython client cannot afford:

```
[u8 rect_count][u8 state][u16 reserved]
then rect_count times:
[u16 x][u16 y][u16 w][u16 h][u8 enc][u16 len][data]
```

`rect_count` of 0 is a **keepalive**: nothing on screen changed, but the link is
alive. Without it a motionless game would look identical to a dead server.

`state` is passed through from the game (0 playing, 1 menu, 2 dead, 3
intermission/title). The client does not have to care; it is there so clients
can show something sensible and so the value is visible when debugging.

Rectangles are absolute screen coordinates and the client keeps the screen
between updates — that persistence is the entire point of sending only what
changed.

### Encodings

| id | name       | payload                        | client cost            |
|----|------------|--------------------------------|------------------------|
| 0  | SOLID      | 2 bytes, one RGB565 colour     | `fillRect`             |
| 1  | RAW565     | w·h·2 bytes, big-endian RGB565 | `drawRawBuf`           |
| 2  | DEFLATE565 | zlib stream of the above       | `deflate` + `drawRawBuf` |
| 3  | JPEG       | baseline JPEG of the rect      | `drawJpg(buf, x, y)`   |

The server picks per rectangle by encoding both ways and keeping the smaller,
with ties going to the lossless one. In practice DOOM's dithered 3D view comes
out as JPEG (~2.1 kB/frame) and flat UI content as DEFLATE565 — which is the
same conclusion TigerVNC's Tight encoder reaches, arrived at by measurement
rather than heuristics.

RGB565 is **big-endian on the wire**: that is what M5GFX's `drawRawBuf(...,
swap=False)` expects, so the device does no byte shuffling.

JPEG is baseline only — no progressive, no arithmetic coding — because TJpgDec
on the ESP32 decodes nothing else.

### 0x83 PONG / 0x85 KICK
```
[hdr][u32 t_client_ms][u32 t_server_ms]
[hdr][u8 reason]
```

## Session lifecycle

```
client                                  server
  |-- HELLO (session=0) ---------------->|
  |<------------- HELLO_ACK (session=N) --|
  |-- INPUT (30Hz) --------------------->|
  |<-------------- FRAME fragments -------|
  |-- BYE ------------------------------>|
```

One active client at a time; a new valid HELLO takes over and the previous
client gets a KICK. This is a single-cabinet arcade machine, not a multiplayer
server. A reconnecting client always gets a full-screen update first, because
the server cannot know what is still on its screen.
