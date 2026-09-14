"""Scale, diff, and encode frames into rectangle updates.

The pipeline per frame: crop -> resize to the device's screen -> compare
against the previous frame at tile granularity -> merge dirty tiles into
rectangles -> encode each rectangle with whichever encoding is cheapest for
the *client* to decode (see DeviceCost).

Why tile diffing matters here is worth stating plainly, because the usual
reason does not apply: the link is barely 5% utilised, so this is not about
bandwidth. It is about the ESP32's decode budget. A measured drawJpg of a full
128x128 frame costs 10.9ms -- a third of a 30fps budget -- and decode time
scales with area. Sending a quarter of the screen makes the device four times
faster at the part it is slowest at.
"""

import io
import zlib

import numpy as np
from PIL import Image

from . import protocol as P


class Scaler:
    """Source frames -> device-sized RGB.

    DOOM's 320x200 is meant to be displayed as 4:3, i.e. with pixels 1.2x
    taller than wide; `pixel_aspect` is what undoes that when letterboxing.
    """

    def __init__(self, width=128, height=128, crop=None, letterbox=False,
                 pixel_aspect=1.0, brighten=1.0):
        self.width = width
        self.height = height
        self.crop = crop                  # (x, y, w, h) in source pixels
        self.letterbox = letterbox
        self.pixel_aspect = pixel_aspect
        self.brighten = brighten
        self._lut = None
        if brighten != 1.0:
            self._lut = np.clip(np.arange(256) * brighten, 0, 255).astype(np.uint8)

    def scale(self, rgb):
        img = Image.fromarray(rgb)

        if self.crop:
            x, y, w, h = self.crop
            img = img.crop((x, y, x + w, y + h))

        if self.letterbox:
            src_aspect = img.width / (img.height * self.pixel_aspect)
            dst_aspect = self.width / self.height
            if src_aspect > dst_aspect:
                dw, dh = self.width, max(1, round(self.width / src_aspect))
            else:
                dw, dh = max(1, round(self.height * src_aspect)), self.height
            # BOX averages every source pixel that lands in a destination one.
            # Nearest-neighbour at this scale factor makes wall textures strobe
            # as the player walks, which reads as noise on a 0.85" screen.
            small = img.resize((dw, dh), Image.BOX)
            out = Image.new("RGB", (self.width, self.height), (0, 0, 0))
            out.paste(small, ((self.width - dw) // 2, (self.height - dh) // 2))
            img = out
        else:
            img = img.resize((self.width, self.height), Image.BOX)

        arr = np.asarray(img, dtype=np.uint8)
        if self._lut is not None:
            arr = self._lut[arr]
        return arr


def to_rgb565_bytes(rgb):
    """RGB888 array -> big-endian RGB565 bytes (what M5GFX blits directly)."""
    r = (rgb[:, :, 0] >> 3).astype(np.uint16)
    g = (rgb[:, :, 1] >> 2).astype(np.uint16)
    b = (rgb[:, :, 2] >> 3).astype(np.uint16)
    return ((r << 11) | (g << 5) | b).astype(">u2").tobytes()


class DeviceCost:
    """What each encoding costs the *client*, not the wire.

    Measured on an M5Stack AtomS3R (ESP32-S3, UIFlow2 MicroPython v1.27) with
    client/caps_test.py:

        drawJpg, 128x128, decode + blit   10.94 ms  -> 0.67 us/px
        drawRawBuf, 128x32, blit           3.68 ms  -> 0.90 us/px
        deflate, 1066 -> 8192 bytes       15.25 ms  -> 3.72 us/px + blit

    That last line is the surprise and it changed the encoder: zlib inflate in
    this firmware is roughly seven times more expensive per pixel than a JPEG
    decode. Choosing the smallest payload would pick deflate for flat content
    and spend 61 ms per screen to save bytes on a link that is 5% utilised.

    So rectangles are chosen by predicted client cost -- decode plus
    transmission -- rather than by size. Re-measure on a new device and pass
    new numbers; nothing else has to change.
    """

    def __init__(self, jpeg_us_px=0.67, raw_us_px=0.90, deflate_us_px=4.62,
                 solid_ms=0.02, fixed_ms=0.15, link_kbps=4000,
                 frag_ms=0.35, frag_payload=1024):
        self.jpeg_us_px = jpeg_us_px
        self.raw_us_px = raw_us_px
        self.deflate_us_px = deflate_us_px
        self.solid_ms = solid_ms
        self.fixed_ms = fixed_ms
        # Transmission is part of the client's cost too: bytes have to arrive
        # and be reassembled before the decode can start.
        self.ms_per_byte = 8.0 / max(1, link_kbps)
        # Each datagram costs the MicroPython loop a recv, a parse and a copy.
        # A raw 128x128 frame is 32 of them; a JPEG is 2. This constant is an
        # estimate until phase 2 measures it on the device.
        self.frag_ms = frag_ms
        self.frag_payload = frag_payload

    def cost_ms(self, enc, pixels, nbytes):
        if enc == P.ENC_SOLID:
            decode = self.solid_ms
        elif enc == P.ENC_JPEG:
            decode = self.fixed_ms + pixels * self.jpeg_us_px / 1000.0
        elif enc == P.ENC_DEFLATE565:
            decode = self.fixed_ms + pixels * self.deflate_us_px / 1000.0
        else:
            decode = self.fixed_ms + pixels * self.raw_us_px / 1000.0
        frags = max(1, (nbytes + self.frag_payload - 1) // self.frag_payload)
        return decode + nbytes * self.ms_per_byte + frags * self.frag_ms


class Encoder:
    def __init__(self, width=128, height=128, tile=32, quality=55,
                 caps=P.CAP_ALL, chroma444=False, full_frame_ratio=0.6,
                 max_rects=8, deflate_level=6, cost=None, select="cost"):
        self.width = width
        self.height = height
        # tile 0 means "never diff, always send the whole frame"; a tile that
        # does not divide the screen would leave an unchecked edge strip, so
        # it falls back to the same thing.
        self.tile = tile if (tile and width % tile == 0 and height % tile == 0) else 0
        self.quality = quality
        self.caps = caps
        self.chroma444 = chroma444
        self.full_frame_ratio = full_frame_ratio
        self.max_rects = max_rects
        self.deflate_level = deflate_level
        self.cost = cost or DeviceCost()
        # "cost" minimises predicted client time; "bytes" minimises payload,
        # which is the right choice only if the link is the bottleneck.
        self.select = select

        self.stats = {"frames": 0, "skipped": 0, "bytes": 0, "rects": 0,
                      "enc": {}, "client_ms": 0.0}

    # --- dirty rectangles -------------------------------------------------

    def _dirty_rects(self, cur, ref):
        if ref is None or self.tile == 0:
            return [(0, 0, self.width, self.height)]

        diff = np.any(cur != ref, axis=2)
        if not diff.any():
            return []

        t = self.tile
        th, tw = self.height // t, self.width // t
        tiles = diff.reshape(th, t, tw, t).any(axis=(1, 3))

        if tiles.mean() >= self.full_frame_ratio:
            return [(0, 0, self.width, self.height)]

        # Horizontal runs per tile row, then merge runs that line up vertically.
        rects = []
        for ty in range(th):
            tx = 0
            while tx < tw:
                if not tiles[ty, tx]:
                    tx += 1
                    continue
                start = tx
                while tx < tw and tiles[ty, tx]:
                    tx += 1
                run = (start * t, ty * t, (tx - start) * t, t)

                if rects:
                    px, py, pw, ph = rects[-1]
                    if px == run[0] and pw == run[2] and py + ph == run[1]:
                        rects[-1] = (px, py, pw, ph + t)
                        continue
                rects.append(run)

        # Too many small rectangles cost more in headers and per-rect JPEG
        # tables than one bigger one; collapse to the bounding box.
        if len(rects) > self.max_rects:
            xs0 = min(r[0] for r in rects)
            ys0 = min(r[1] for r in rects)
            xs1 = max(r[0] + r[2] for r in rects)
            ys1 = max(r[1] + r[3] for r in rects)
            rects = [(xs0, ys0, xs1 - xs0, ys1 - ys0)]

        return rects

    # --- per-rect encoding ------------------------------------------------

    def _encode_rect(self, sub, quality=None):
        raw = to_rgb565_bytes(sub)
        pixels = sub.shape[0] * sub.shape[1]
        if quality is None:
            quality = self.quality

        # A single colour is the cheapest thing on the wire and the cheapest
        # thing on the device: two bytes and a fillRect.
        if sub.size and np.array_equal(sub.min(axis=(0, 1)), sub.max(axis=(0, 1))):
            return P.ENC_SOLID, raw[:2]

        candidates = [(P.ENC_RAW565, raw)]

        if self.caps & P.CAP_DEFLATE:
            candidates.append((P.ENC_DEFLATE565, zlib.compress(raw, self.deflate_level)))

        if self.caps & P.CAP_JPEG:
            buf = io.BytesIO()
            Image.fromarray(sub).save(
                buf, format="JPEG", quality=quality,
                subsampling=0 if self.chroma444 else 2, optimize=True)
            candidates.append((P.ENC_JPEG, buf.getvalue()))

        if self.select == "bytes":
            return min(candidates, key=lambda c: len(c[1]))

        return min(candidates,
                   key=lambda c: self.cost.cost_ms(c[0], pixels, len(c[1])))

    def encode(self, frame, ref=None, max_bytes=0):
        """Encode `frame` as rectangles that repair `ref` into `frame`.

        `ref` is the screen the client is known to be showing -- not the last
        frame encoded. Those are different things over UDP, and conflating
        them is a silent bug: an update that never arrives leaves those pixels
        wrong forever, because the server already believes it sent them. Pass
        None for a full frame.

        Returns [] when nothing changed, in which case nothing should be sent.
        """
        rects = self._dirty_rects(frame, ref)

        if not rects:
            self.stats["skipped"] += 1
            return []

        def encode_all(quality):
            return [(x, y, w, h) + self._encode_rect(frame[y:y + h, x:x + w], quality)
                    for (x, y, w, h) in rects]

        def payload_size(encoded):
            return (P.UPDATE_HDR_LEN +
                    sum(P.RECT_HDR_LEN + len(r[5]) for r in encoded))

        out = encode_all(self.quality)

        # Try to keep the whole update inside one datagram. On the AtomS3R a
        # second fragment sent immediately after the first is simply dropped --
        # its socket queues one datagram -- so an update that needs two is an
        # update that usually never arrives. Fragmentation still works (the
        # server paces the fragments), but not needing it is far better, and
        # DOOM at a slightly lower JPEG quality beats DOOM that never repaints.
        if max_bytes and (self.caps & P.CAP_JPEG) and payload_size(out) > max_bytes:
            # Full-motion photographic content needs to go much lower than a
            # game does: measured on a Skyrim recording, 15% of frames fit one
            # datagram at q19 and 97% at q10. A softer picture that arrives
            # beats a sharper one that stalls waiting for its second half.
            for q in (int(self.quality * 0.7), int(self.quality * 0.5),
                      int(self.quality * 0.35), int(self.quality * 0.25),
                      int(self.quality * 0.18)):
                candidate = encode_all(max(10, q))
                if payload_size(candidate) <= max_bytes:
                    out = candidate
                    self.stats["squeezed"] = self.stats.get("squeezed", 0) + 1
                    break
                out = candidate      # keep the smallest attempt regardless

        for (x, y, w, h, enc, data) in out:
            self.stats["enc"][enc] = self.stats["enc"].get(enc, 0) + 1
            self.stats["bytes"] += len(data)
            self.stats["client_ms"] += self.cost.cost_ms(enc, w * h, len(data))

        self.stats["frames"] += 1
        self.stats["rects"] += len(out)
        return out

    def stats_line(self, elapsed):
        s = self.stats
        total = s["frames"] + s["skipped"]
        if not total:
            return ""
        mix = " ".join("%s:%d" % (P.ENC_NAMES.get(k, k), v)
                       for k, v in sorted(s["enc"].items()))
        # The client-cost estimate is the number that matters: what frame rate
        # the device could sustain if it did nothing but decode and draw.
        per_frame = s["client_ms"] / max(1, s["frames"])
        line = ("%.1f fps sent (%.1f produced), %.1f kB/s, %.1f rects/frame, "
                "est client %.1f ms/frame (~%.0f fps ceiling), %s" % (
                    s["frames"] / elapsed, total / elapsed,
                    s["bytes"] / elapsed / 1024.0,
                    s["rects"] / max(1, s["frames"]), per_frame,
                    1000.0 / per_frame if per_frame else 0.0, mix or "-"))
        self.stats = {"frames": 0, "skipped": 0, "bytes": 0, "rects": 0,
                      "enc": {}, "client_ms": 0.0}
        return line
