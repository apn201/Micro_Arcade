# Troubleshooting: is it the link, the device, or the controls?

Those three have opposite fixes and they feel the same from the player's seat,
so every symptom below starts by naming the number that tells them apart.

## Turn the instruments on

**Device** — set `DEBUG = True` in `client/config.py`. Every five seconds the
UIFlow console prints:

```
rx 14.8 fps | draw 11.2 ms | gap avg 67 max 156 ms | stalls>200ms 0 | lost 0 | frags 123 | 1.5 kB/f | heap 8102k
```

| field | meaning | what it looks like when it is the problem |
|---|---|---|
| `rx` | complete updates drawn per second | below ~10 and everything feels like a slideshow |
| `draw` | time inside `drawJpg`/`drawRawBuf`/`fillRect` | near 11 ms is normal for a full frame; much more means the device is the bottleneck |
| `gap avg` | time between complete updates | this is the real frame period; `1000/gap` should match `rx` |
| `gap max` / `stalls` | the worst pause, and how many exceeded 200 ms | **this is the judder you feel.** avg 60 with max 600 is a stuttering link, not a slow one |
| `lost` | updates abandoned half-assembled | non-zero means fragments are being dropped — the link, not the device |
| `frags` | datagrams received in the interval | compare with `rx × 2`: a moving DOOM frame is two fragments |

**Server** — every five seconds:

```
14.2 fps sent (17.6 produced), 19.4 kB/s, 1.2 rects/frame, est client 10.2 ms/frame (~98 fps ceiling), jpeg:80
link: blocked 3, acks 71, ack latency avg 38 ms max 210 ms, in flight 1
```

`produced` is what the game rendered; `sent` is what survived the dirty-rect
skip and flow control. `est client` is what the cost model predicts the device
will spend decoding — compare it against the device's real `draw`. `blocked` is
how often flow control held a frame back, and `ack latency` is how long the
device took to confirm one.

**Controls** — add `--debug-keys` (or `MD_DEBUG_KEYS=1`). A few lines a second,
safe to leave on while playing:

```
keys +FWD +USE       turn=  +13 move= +282 raw=(-50, -700, 690) state=0
keys +LEFT           turn= -208 move=   -6 raw=(300, 20, 930) state=0
```

`turn`/`move` are the smoothed, calibrated values the decision was made on;
`raw` is what the device actually sent. If a key appears that you did not
intend, this line shows exactly how close to the threshold you were.

## "It jumps forward and hits a wall"

Two different causes, and the device's `gap max` separates them.

**If `gap max` is large (say >400 ms) and `stalls` is non-zero:** it is latency,
not controls. You tilt, the screen shows nothing for half a second, and DOOM
has been walking the whole time — so the wall arrives before the picture does.
Look at `lost` and the server's `blocked`/`ack latency`. Lower `--fps`, which
sounds backwards but reduces the queue the device has to keep up with.

**If gaps are steady:** it was sprinting. Full tilt measures about 700 milli-g,
so the old `--run 420` default meant any committed lean also held RUN, at
double speed, on a screen too small to see the wall coming. `--run` now
defaults to `0` (never run). Set it to ~600 if you want sprinting back on a
hard lean only.

## "Turning is twitchy / it turns when I did not ask"

Run with `--debug-keys` and watch the `turn=` value when it misfires. A hand
holding a device drifts and shakes by tens of milli-g, and anything that
crosses `--deadzone` (default 180) engages.

- Raise `--deadzone` to 220-260 for a more deliberate lean.
- Lower `--smooth` (default 0.4) toward 0.2 for heavier filtering. It costs
  response time: 0.4 settles in about 3 samples (~100 ms), 0.2 in about 7
  (~230 ms).
- Re-seat the neutral pose by reconnecting the device while holding it the way
  you actually play. The server captures neutral from the first half-second of
  packets, so calibrating flat on a desk and then playing at an angle spends a
  chunk of the deadzone before you start.

## "frags N, lost N" — nothing ever completes

If the device's `lost` count matches its `frags` count, every update is
arriving incomplete, and the give-away is in the same line: updates around
`1.3 kB/f` work while `1.7 kB/f` ones never do. That is the boundary between
one fragment and two.

The AtomS3R's socket holds **one datagram**. Two fragments sent back to back
means the second arrives while the first is still sitting unread, and it is
simply dropped — so any update needing two fragments fails forever, while
anything that fits in one works perfectly. It looks like a link problem and is
not: `rtt` stays in single-digit milliseconds throughout.

Two settings address it, both on by default:

- `--frag-gap 3` spaces the fragments of one update by 3 ms. `-1` sends them
  all at once, which is right for a PC client and fatal for this device.
- `--no-fit-datagram` disables the other half: normally the encoder will trade
  JPEG quality to keep an update inside a single datagram, so the second
  fragment is usually not needed at all.

The cost is throughput for clients that *cannot* use JPEG: a raw RGB565 frame
is ~24 fragments, and pacing those is 72 ms of transmission per update. That
path is a diagnostic fallback, not a normal mode.

`service/tests/test_stream.py` models this device: a client that refuses any
datagram arriving within 2 ms of the last one gets 0 updates unpaced and ~67
paced.

## "Smooth for a second, then frozen, even standing still"

**Check `rtt` in the device line first.** On a quiet LAN it should be single
digit. If `rtt max` is in the hundreds of milliseconds while nothing is moving,
the radio is asleep, not busy.

The ESP32 powers its radio down between DTIM beacons by default, so a
downstream packet can wait 100-300 ms for the next wake-up. With only one or
two updates allowed in flight, one such nap costs the whole frame budget
several times over — and it is *most* visible when the screen is nearly static,
because then a single delayed update is the entire animation.

The firmware disables it at connect:

```python
wlan.config(pm=network.WLAN.PM_NONE)
```

and prints `wifi: power save disabled` when it works. If you see `could not
disable power save` instead, that build names the setting differently and the
stalls will persist; say so, because the fallback path needs extending.

Anything blinking at a fixed period — DOOM's flickering lights are perfect for
this — makes the problem obvious: the blink either keeps time or it does not.

## "Smooth for a second, then janky"

Look at `lost` and `stalls` together:

| `lost` | `stalls` | reading |
|---|---|---|
| 0 | 0 | the link is fine; low `rx` with high `draw` means the device is the limit |
| 0 | some | frames are arriving in bursts — WiFi contention or the server loop stalling. Check the server is not running with `--debug-input` |
| some | some | fragments are being dropped. Lower `--fps`, or `--quality` to shrink frames toward a single datagram |

Wi-Fi power saving on the ESP32 is a common cause of periodic 100-300 ms
stalls. If `stalls` clusters at a regular interval, that is worth suspecting
before anything in this repo.

## "The whole screen freezes but a couple of blocks keep animating"

That specific shape means the server's reference frame has drifted from what
the device is showing: it believes it already sent pixels the device never
received. It should be fixed — the server only diffs against acknowledged
frames now, and sends a full keyframe every `--keyframe-ms` (default 3000)
regardless. If it comes back, `--tile 0` disables dirty rectangles entirely and
sends whole frames, which is slower but cannot drift. If that cures it, the
reference tracking has a bug worth reporting.

## Reproducing without the hardware

```bash
python service/tests/firmware_harness.py    # the real firmware, on the PC
python service/tests/test_stream.py --doom  # the whole pipeline, including 25% packet loss
```

The harness runs `client/main.py` itself with only M5GFX, the IMU and the
button stubbed, so a protocol or decode bug reproduces there in seconds instead
of a flash cycle. What it cannot show you is real WiFi — if the harness is
clean and the device is not, the difference is the radio.
