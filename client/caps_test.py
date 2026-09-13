"""AtomS3R capability probe -- run this once, before porting the terminal.

It answers the questions the streaming protocol depends on, on the actual
device rather than from documentation:

  * does this firmware have deflate/zlib decompression?
  * does drawJpg() accept a bytes buffer, and how long does a 128x128 frame
    take to decode? That decode is the largest single cost in the frame budget,
    so the number it prints is the one that decides the achievable frame rate.
  * does drawRawBuf() accept a buffer, and in which byte order?
  * do the IMU and the button read?

Results print to the serial console and appear on screen. Send the console
output to the server side: the caps the firmware advertises are derived from
exactly these answers, and the server adapts its encoding to match.

The embedded image is a real DOOM frame from the streaming service, encoded
exactly as the service would encode it.
"""

import gc
import sys
import time
from binascii import a2b_base64

import M5
from M5 import *

JPEG_FRAME_B64 = (
    "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"
)

DEFLATE_STRIP_B64 = (
    "eJytWDuyIjEMTAg2fOGkVJEQEpJuFUfhKhzkBe8iBByE4F1jy5Ylt362p9hSMh9L7pZa8sDl"
    "51Lt+rr+Xn///ql3j/P9fDueip3v5/vlQVaurfE7sBbx8igxSqTzray7vorZ1Tbe9SV46hUg"
    "qVEgOt3ZeDd6WrH96BiMRKJTDIudUN3ER2Wgow6YGyTH0/HQo/ToS8Z1iPkzgjReR9giHHQG"
    "uMrCl+pUsodeLgOKQ8OiUGv/xr/oYJn5QyM0/B9j/lRX0Jb4M3utZ15vFK17wPjFjCEj0EO0"
    "v2BfN9OHNkaGR/gX7bf8NYsy5hQZ66arIO5/QNPidUVh/T1mH09XiNkzf40i8S8r7exwlW9X"
    "kJ3qLddaO5wBlVFBZPDAiprBYoTBm52o+By8ZX9EwXj1fry2em9v8E8M3t7DeCMGDrN+JxhO"
    "scV4PH8Vf8RfcJKn5e/2jxnr9ViDVWtzRDis+naGlSXjN/xljyCfRfvl9CHUMPfVOajrHvCH"
    "HBy2d7Goinnm5Fn15qd9tkilc1WSpzCYqxi033wlq83m+rPZVEgkAxMEKf9ghvwn/noFdx3o"
    "apG/Oy97F066IOOP+l/vIeRv2eGqzDfuuhX+ZiLqDKCPn/VxTwCW0a4Rf80i47PKfkfuFX/g"
    "gfut8hfPGfIZ/xjpWHEf2n3IZA//UI0L/CdZ8l4fsbcnuHmPczBAHPJPzsDJJOz8ZfUUPeW6"
    "z336/bo6cSP+sHs7j/VMnvMHLjUTHNl8f37Mn5Um5x79/vBfltoL5pqZiuF5gZMw/Gb19+H5"
    "mc1f4ku7wPltVeO+M/nbxp4YfpWuFdUzxpaaZeNziHft62n0FWxzos/+DIU6lWu+/PeCy7Oe"
    "5oev76/v7dn56IlVNJtgezYre/Yrtn7P654JG2SBp6RkbWi4c712e1hlM3/k0PnL3HBVzPir"
    "GFP+h1X+1fO931zsoN9ELZQvb1P92+qEUcRSfIMdiP84rsexvU31JvkGPPwE1TDJAc+oHRl7"
    "7mCfdYyqpqis1UF3t/dQ9YpQ6n4I9Ok1sIv/TGtc1SX2mj/1b8De6EW9NTjcaZDyx/nTnvN7"
    "XKuVGvdbpOka1f9eyepAz1T1aVqZPon80fgfG+w+OFPd94CpU8TH4uzfg0H92+xLZ0q4W3Jv"
    "Z0EYNdjfZktpIqhVomTe2fRLUo+Op82gJGrYgza61++O+RvgWzh3sgxY/vZcdHFg9uVZ1R3o"
    "Tl3TS3vOH8jcSg7MqiX+7To733Gm5KpKEDl9BjkZ6V99PZJ/pKrT8jkQ6R90mVR14ctXscKI"
    "fpaOzugwI32Kzv1H2dd9aJgl/WLWRNFgTxPF+S3MJz9fXf5EBeH8m2Qg1H9uGCVei3vpFVnn"
    "j3fbhnONOpJ/JeU91/Nk3in+ozg2ZhJP9Zudf4pTMF/tr5Z/RbROFA=="
)


def show(line, y, color=0xFFFFFF):
    try:
        M5.Display.setCursor(2, y)
        M5.Display.print(line, color)
    except Exception:
        pass
    print(line)


def timed(fn, repeats=5):
    """Median-ish timing: the first call pays for lazy allocation."""
    fn()
    best = None
    for _ in range(repeats):
        t0 = time.ticks_us()
        fn()
        dt = time.ticks_diff(time.ticks_us(), t0)
        if best is None or dt < best:
            best = dt
    return best / 1000.0


def main():
    M5.begin()
    try:
        M5.Display.fillScreen(0x000000)
        M5.Display.setTextSize(1)
    except Exception:
        pass

    print("=" * 46)
    print("micro-doom capability probe")
    print("=" * 46)

    caps = []
    y = 2

    # --- memory ---
    gc.collect()
    print("free heap: %d bytes" % gc.mem_free())

    # --- deflate / zlib ---
    inflate = None
    which = None
    try:
        import deflate
        import io as _io

        def inflate(data):
            return deflate.DeflateIO(_io.BytesIO(data), deflate.ZLIB).read()

        which = "deflate"
    except ImportError:
        try:
            import zlib

            def inflate(data):
                return zlib.decompress(data)

            which = "zlib"
        except ImportError:
            inflate = None

    packed = a2b_base64(DEFLATE_STRIP_B64)
    if inflate is None:
        show("deflate: MISSING", y, 0xFF4444)
        y += 10
        print("  -> firmware has neither `deflate` nor `zlib`")
        print("  -> build the client with CAP_DEFLATE off; JPEG and raw still work")
    else:
        try:
            raw = inflate(packed)
            ok = len(raw) == 128 * 32 * 2
            ms = timed(lambda: inflate(packed))
            show("deflate: %s %.1fms" % (which, ms), y, 0x44FF44 if ok else 0xFF4444)
            y += 10
            print("  module=%s  %d -> %d bytes in %.2f ms" % (which, len(packed), len(raw), ms))
            print("  a full 128x128 screen would cost about %.1f ms" % (ms * 4))
            if ok:
                caps.append("deflate")
            else:
                print("  -> WRONG SIZE: expected %d, got %d" % (128 * 32 * 2, len(raw)))
        except Exception as exc:
            show("deflate: FAILED", y, 0xFF4444)
            y += 10
            print("  -> %s" % exc)

    # --- drawRawBuf ---
    raw_ok = False
    try:
        raw = inflate(packed) if inflate else bytes(128 * 32 * 2)
        buf = bytearray(raw)
        ms = timed(lambda: M5.Display.drawRawBuf(buf, 0, 96, 128, 32, len(buf), False))
        show("drawRawBuf: ok %.1fms" % ms, y, 0x44FF44)
        y += 10
        print("  128x32 blit in %.2f ms (swap=False)" % ms)
        print("  if that strip looked wrong, retry with swap=True")
        raw_ok = True
    except Exception as exc:
        show("drawRawBuf: FAILED", y, 0xFF4444)
        y += 10
        print("  -> %s" % exc)

    # --- drawJpg: the number that sets the frame rate ---
    jpeg = a2b_base64(JPEG_FRAME_B64)
    try:
        ms = timed(lambda: M5.Display.drawJpg(jpeg, 0, 0))
        show("drawJpg: ok %.1fms" % ms, y, 0x44FF44)
        y += 10
        print("  128x128 JPEG (%d bytes) decoded+blitted in %.2f ms" % (len(jpeg), ms))
        print("  -> that alone caps the frame rate at about %.0f fps" % (1000.0 / ms))
        caps.append("jpeg")
    except Exception as exc:
        show("drawJpg: FAILED", y, 0xFF4444)
        y += 10
        print("  -> %s" % exc)

    # memoryview is what the real client will pass; some builds refuse it
    try:
        M5.Display.drawJpg(memoryview(bytearray(jpeg))[0:len(jpeg)], 0, 0)
        print("  drawJpg accepts a memoryview slice (no copy needed)")
    except Exception as exc:
        print("  drawJpg rejects memoryview (%s) -- the client must pass bytes()" % exc)

    # --- IMU ---
    try:
        ax, ay, az = Imu.getAccel()
        show("imu: %.2f %.2f %.2f" % (ax, ay, az), y, 0x44FF44)
        y += 10
        print("  accel (g): x=%.3f y=%.3f z=%.3f" % (ax, ay, az))
        print("  tilt the device and re-run to see which axis is which")
    except Exception as exc:
        show("imu: FAILED", y, 0xFF4444)
        y += 10
        print("  -> %s" % exc)

    # --- button ---
    try:
        M5.update()
        print("  button pressed right now: %s" % BtnA.isPressed())
        show("button: ok", y, 0x44FF44)
        y += 10
    except Exception as exc:
        show("button: FAILED", y, 0xFF4444)
        y += 10
        print("  -> %s" % exc)

    print("-" * 46)
    print("caps to advertise: %s" % (", ".join(caps) if caps else "raw only"))
    print("micropython: %s" % sys.version)
    gc.collect()
    print("free heap after: %d bytes" % gc.mem_free())
    print("=" * 46)

    # Leave the DOOM frame on screen: if it looks like DOOM, the whole
    # decode path works.
    time.sleep(2)
    try:
        M5.Display.drawJpg(jpeg, 0, 0)
    except Exception:
        pass


main()
