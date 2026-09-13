# Notes

The long version of the decisions and the bugs. The [README](README.md) is the front door; this is the workshop floor. The Hackaday project log tells the same story as a narrative; this is the reference.

Everything here follows one principle: the device is a draw-only terminal. It renders rectangles and reports a tilt and a button. Every hard decision came from measuring what the device could actually do and pushing everything else to the server.

## Measure the device before designing the protocol

The first encoder copied VNC's Tight approach: send changed rectangles, deflate the flat ones, JPEG the busy ones, pick whatever payload is smallest. It was slow. Measuring the actual board explained why:

| operation on the AtomS3R | measured | per pixel |
|---|---|---|
| `drawJpg`, 128x128, decode + blit | 10.94 ms | 0.67 µs |
| `drawRawBuf`, 128x32, blit | 3.68 ms | 0.90 µs |
| `deflate`, 1066 -> 8192 bytes | 15.25 ms | 3.72 µs |
| free heap | 8.29 MB | |

zlib inflate is ~7x more expensive per pixel than a JPEG decode on this firmware. Picking the smallest payload was spending 61 ms per screen to save bytes on a link that was 5% utilised. So the encoder stopped optimising for payload size and started optimising for *client finish time*: decode cost + transfer time + a per-datagram cost, priced in milliseconds the device actually spends.

JPEG wins almost everywhere now. Deflate stays available for any client that lacks a JPEG decoder; the client declares its capabilities in HELLO and the server routes around whatever is missing.

Dirty rectangles are computed on 32x32 tiles; if more than 60% of tiles changed it is cheaper to send the whole frame than many small ones. Streaming DOOM E1M1 at 128x128 / 20 fps runs ~2,146 bytes per frame while moving (42 kB/s). The link was never the bottleneck; the device was.

## Four bugs that all looked like bad WiFi

Every one of these presented as flaky WiFi. None of them were.

**1. Two animated blocks on a frozen screen.** The encoder diffed each frame against the last frame it had *encoded*. Over UDP that is not what the device has on screen. One lost update and those pixels are stale forever, because the server believes it already sent them. Only continuously-changing tiles self-repair, so DOOM looked like two blinking lights on a frozen level. Fix: every input packet carries `last_frame_id`, so the server diffs against the last frame the device *acknowledged*. The regression test drops 25% of packets and checks convergence - and its first version passed against broken code because the periodic keyframe hid the bug every 3 seconds. Keyframes are disabled for that test now.

**2. Every fragmented update lost, every single-datagram update fine.** The AtomS3R's UDP socket holds one datagram. Send two back to back and the second is gone. Fragments are paced ~3 ms apart, with flow control on top: at most 2 unacknowledged updates in flight, ack timeout at 3x smoothed RTT with exponential backoff. Frame rate settles at what the device can absorb, not what `--fps` requests.

**3. Smooth, then frozen for half a second.** ESP32 WiFi modem sleep: the radio naps between DTIM beacons and a downstream packet waits 100-300 ms for it to wake. That is several frame budgets, worst when the picture is nearly static. Fix: disable power save on the device (`PM_NONE` / `WIFI_PS_NONE`).

**4. `'socket' object has no attribute 'recv_into'`.** UIFlow2's socket lacks it; other MicroPython builds have `recv_into`, or only the stream `readinto()`, or `recv` / `recvfrom`. The firmware probes for each in order and keeps the first that exists, printing what it picked. `readinto()` also reports "no data" as `None` instead of 0, which made the first version spin forever.

The device-side draw path is deliberately tiny: `drawJpg` takes a memoryview slice so a JPEG goes from receive buffer to decoder with no copy. The entire firmware also runs on the PC with M5GFX and the IMU stubbed out, drawing into a PNG - which is how the protocol got debugged without flashing anything. Every line of packet parsing is the firmware's own.

## DOOM, and a compiler that broke it

DOOM is doomgeneric with a small platform layer that pushes frames over loopback TCP with a 16-byte header, plus a game-state byte (menu / in-level / dead) so one button can mean ENTER, USE, or FIRE depending on context.

Building with `zig cc` surfaced two things. zig enables UBSan traps by default, and DOOM left-shifts a negative during sprite init - undefined behaviour that has worked for 30 years, but trapped it dies before the first frame. At `-O1`+ clang also miscompiles something in texture init (corrupt hash chain, hang); the pass was never identified. gcc is fine. The build script pins `-O0` for zig/clang, disables the UB sanitizer, and forces `-fno-strict-aliasing`. At `-O0` DOOM still renders faster than its 35 fps. On Windows there was also a `boolean` typedef clash with `windows.h` and a winsock `s_host` macro collision.

## Controls

Tilt is the joystick: full tilt is ~700 mg of travel, hands shake faster than 30 Hz, so the server smooths with an EMA (0.4), captures "level" from the first half-second of packets rather than assuming a flat table, and applies 60 mg of deadzone hysteresis so a direction does not flicker at the edge.

The button is the whole screen, giving three gestures: click (main action), double-click within 400 ms (second action; DOOM: door), long-press 900 ms (always exit to menu, unbindable by games). The double-click does not delay the first press - waiting to disambiguate would put 400 ms of latency on every shot, and one wasted bullet at a door is the better trade.

A note on walking into walls: `--run` was once 420 mg on a sensor topping out near 700, so every committed lean was also a sprint. Default is 0 now.

## From one game to a library

The client never changed. First it drew DOOM; then it drew a menu; then picking a menu item spun up a real DOSBox on the server. A 6DOF flight sim and a DOS platformer are the same amount of "running on the ESP32", which is zero. The menu is just another 128x128 frame source. Picking a game swaps the source underneath, the picture changes shape (128x128 -> 320x200), the server rebuilds its scaler and loads that game's control profile, and the device keeps drawing rectangles throughout.

The DOS side is js-dos in Node, no browser, one process per game, dropping any frame that arrives while the socket is backed up because only the newest matters. Two traps worth recording:

- `ci.sendKeyEvent()` wants GLFW key codes, not DOSBox ordinals. Left arrow 263, Enter 257, Esc 256, left Ctrl 341. The first table used DOSBox numbers and nothing responded.
- The hand-rolled `.jsdos` zip writer (under 90 lines, to avoid a zip dependency) didn't emit directory entries. The emulator creates a file's parent but not its grandparent, so Wolfenstein 3D (kept two levels down at `WOLF3D/WOLF3D/`) died with `WOLF3D/HELP: No such file or directory`. Fix: emit every parent directory before the first file.

## Boot keys and the audition tool

Nearly every DOS title opens on a sound-card prompt, a "press any key", and menus - unreadable at 128x128 and unreachable without a keyboard. So each game carries a scripted `boot_keys` sequence that drives it all the way into gameplay (car on the grid, plane in the air). Example, Indianapolis 500 abridged: `1` answers the eXoDOS sound-card launcher, a run of Enters walks the title screens (DOS menus usually open on the item you want), then Esc/Enter out of the PRESS ESCAPE TO DRIVE attract loop into the pit lane.

Working these out by hand was slow, so `audition.py` scripts the keys and screenshots the result: a few frames with no input, then several with a movement key held. The question is not "did the picture move" - an attract demo moves beautifully and a car on the start line does not - but "did the picture answer the controls":

```python
if w >= 640:                                   verdict = "TEXT"    # still at a DOS prompt
elif react >= MOTION_THRESHOLD and react >= quiet * 2.0:  verdict = "PLAY"  # answered controls
elif quiet >= MOTION_THRESHOLD:                verdict = "DEMO"    # busy, but not from us
else:                                          verdict = "STATIC"  # menu/title
```

It writes a filmstrip of the samples, which is where the real work happened: see the screen it stuck on, add the keys that answer it, repeat. Lessons:

- Holding one direction gives false negatives (Commander Keen against scenery; Jill of the Jungle behind a `YOU NEED A GEM TO PASS` door). The probe now cycles right/left/up across the hold window.
- Tyrian's Enters opened its Data reader; the sequence has to Esc out and press Down four times to reach Play Next Level.
- Tyrian still scores DEMO (self-scrolling background); it and Crystal Caves were verified by eye.

A faster Enter cadence (1.5 s vs 2.5 s) still landed 16 of 18 games in gameplay, cutting boots to ~18.5 s. Tyrian takes 50.

## Games cut, and why

A small catalogue of failure:

- **Descent** - endless briefing video under the emulator (watched to 165 s). Timeline: Interplay logo 8 s, title 16 s, pilot name 22 s (pre-filled "BLECH"), input device 26 s, menu 31 s, difficulty 36 s, then the video that never ends.
- **Frontier: Elite 2** - reaches the world, needs a mouse to launch.
- **LHX Attack Chopper** - manual-lookup copy protection ("Service ceiling in meters of the Mil Mi-8 Hip-C?").
- **Comanche CD** - serial number. **Raptor** - mouse. **Mortal Kombat** - coin slot. **Mario Andretti** - help screen refuses Escape.

## Skyrim, the one that isn't a game

The kiosk has one title that is not emulated at all. `engine: video` plays a recording through OpenCV inside the service, at the video's own frame rate, and loops it. Frames it is late for get grabbed but never decoded to pixels, so only the newest one pays for colour conversion. The clip is 16:9 and the screen is square, so it is centre-cropped rather than squeezed.

It is there because the device can't tell a recording from a game, which was the whole premise. The file lives in `kiosk/media/` and is gitignored; the library entry only points at it.

## The case

Retromaker's arcade cabinet from Printables, modified to fit. Printed on a Prusa M4S, sides in wood-toned PLA, the rest black. Small enough to hold between two fingers.

## Next steps

The reusable artifact is the pipe, not the arcade. Once the device is a draw-only terminal rendering rectangles from a server, the device stops mattering and the server can drive anything.

- **Legacy HMI glances (the real one).** Virtualized/contained legacy control PCs still sometimes need a corner of their screen visible at an operator panel. A full thin client is overkill for a glance. A sub-€20 draw-only board rendering one rectangle of a remote framebuffer, no OS, no attack surface, fits the non-critical, non-Ex, "just show me that gauge over there" case. Boundaries stated plainly: not the primary HMI, not safety-critical, not hazardous areas.
- **Many screens, one surface (the fun one).** The server already slices frames into rectangles for a client that draws whatever arrives. That client can be fifty clients. Fifty tiles as one surface, flat or wrapped over a sphere, is the same protocol pointed at many terminals. No use case required.
- **Not a keyboard (the deliberate refusal).** The loose keys and GPIO pins are right there. Using them would make the thing work better and mean less. The premise is native peripherals only - a terminal made from a board that was never meant to be one. A keyboard would just make it a small computer with buttons.
