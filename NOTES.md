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

## Why Skyrim stopped every now and then

Two separate things, found by measuring on loopback rather than guessing.

The decoder. OpenCV decodes a frame of the clip in 0.3 ms, but about once every 40 seconds one read took 182 ms. Done inline, that froze the whole service loop and the picture stopped. The video source now decodes in its own thread, half a second ahead of the clock, and hands the service whichever frame is due.

The datagram budget. The encoder squeezes JPEG quality to keep an update inside one UDP datagram, because the device's socket holds one datagram and a second fragment is a coin toss. The squeeze stopped at quality 19. That's plenty for DOOM, whose updates are small dirty rectangles, but Skyrim changes every pixel of every frame, snow and trees, and at q19 only 15% of frames fit. Every other update went out in two fragments, and a lost half means no ack, an ack timeout and a stall. The ladder now goes down to q10, where 97% of frames fit. On loopback, 100% of updates became single datagrams and the longest freeze on a clean link dropped from 198 ms to 141 ms. With 3% packet loss there is still one stall of about a quarter second every twelve seconds or so, which is the ack timeout doing its job.

## Demo reel and browsing

The menu shows the "play" catalogue. The demo reel runs the "demo" catalogue, which by default is everything, plus titles that are only worth watching. It boots real games live, loading screens included, with their controls working, so someone can pick the cabinet up mid-reel and play. It moves on after `advance_s`, or a title's own `demo_s`, or on double click. `advance_s: 0` waits for the double click only. A title can boot differently in the reel with `demo_keys`: Dragon's Lair skips the start press so its own attract demo plays.

Double click can be claimed by the kiosk in either mode (`"double_click": "next"`). The game then never sees it, same rule as long press. The first click of the pair still reaches the game; delaying every click to wait for a possible second one would cost more than it saves.

Nobody is holding the cabinet in attract mode, so the reel also plays. An autopilot wraps the title's own profile and, once the boot keys have had time to get into the game, holds and taps the same joystick bits a GPIO joystick would set: forward and fire for the shooters, throttle and a little steering for racing, run right and jump for platformers. Going through the profile means every game's own key mapping still applies. The turns are lopsided, two lefts to every right: with symmetric ones DOOM spent half a minute firing at the first wall it walked into. Any press, or a tilt past the deadzone, hands control to whoever picked the cabinet up; six idle seconds and the autopilot is back. The laserdisc and full-motion video titles have it switched off, because input only interrupts their attract sequences. The first real-server run crashed the moment Wolfenstein started: boot-key waits add up as floats, and a float can't index the list of turns. The test now drives a reel title through its turns instead of only checking it was wrapped.

Long press now also resets the tilt. Holding the device still with a finger on the screen for most of a second is the best calibration sample the cabinet ever gets, so the kiosk averages the accelerometer over the press and uses it as level for the menu and the next game. The server resets a profile whenever it swaps one in, so the preset has to survive `reset()`.

## Laserdisc games

Dragon's Lair and Space Ace are quick-time events all the way through: a direction or the sword, at the right moment. That is exactly tilt plus a tap, which is why they are worth the trouble.

- ReadySoft's PC versions open on a text setup screen that wants letters, not numbers: V for VGA, N for no sound, N for no joystick, N for no install. The "1" the rest of the library sends for the eXoDOS launcher landed in the graphics field and sat there forever.
- Space Ace's "Press Fire Button to Start" ignores Space, Enter, Ctrl, Alt and numpad 5. Fire is numpad 0, or Insert. Dragon's Lair (1989) takes Space. Dragon's Lair II reads the whole numpad.
- Dragon's Lair III and Space Ace II ask for a code from the manual once a game starts, so they are out of the play catalogue. Space Ace II's intro still runs in the reel.
- CD titles needed the bundler to carry `imgmount` lines over from eXoDOS's config. Guy Spy then still failed with "The image must be on a host or local drive": eXoDOS writes `cd\GuySpy.cue`, the archive has `CD/`, and js-dos's file system is case-sensitive. Mount paths are now matched against the real file names.
- Dragon's Lair II's launch script shows a hint and then runs `pause` before the game, and the bundler took `pause` for the game.
- Mad Dog McCree's full-motion video runs fine from its ISO. It wants a light gun, so it is in the reel only.

## Running eXoDOS in place

The first version copied every game out of eXoDOS into a `.jsdos` bundle next to the code. Fine for a 1 MB shareware title, silly for a 500 MB CD game that is already sitting on the disk.

js-dos turned out to accept a list of zips and load them into one file system, in order. So a title now boots from two: a few hundred bytes generated on the spot, then the eXoDOS zip itself, untouched, from wherever the collection lives. The generated one holds the `dosbox.conf` and one entry for every folder of the game. The folders are the catch: eXoDOS zips have no folder entries, and js-dos creates a file's parent folder but not its grandparent, so without them Wolfenstein 3D never started at all.

Everything the config needs comes from the zip's file list and eXoDOS's own launch script, never from extracting anything: which CD or floppy images to mount (with the file names' real case, and quotes where the path has spaces), which drive and folder to start from, and whether `call run` means `RUN.BAT` or `RUN.EXE`. `from-exodos.py` builds its bundles from the same code, so there is one copy of those rules.

Memory is the limit. A running title takes roughly five times its zip size: The Last Bounty Hunter's 194 MB was 1.1 GB, Brain Dead 13's 456 MB was 2.5 GB, Space Ace CD's 541 MB was 3.0 GB. js-dos runs DOSBox as 32-bit WebAssembly, which stops at 4 GB, so the kiosk skips zips over 700 MB and the 1 GB discs stay out.

Bundles that already exist are still used first, because every title in the play catalogue was auditioned from one. Setting `"prefer": "in_place"` ignores them.

Auditioned in place, 20 of the 26 playable eXoDOS titles scored PLAY without a bundle anywhere. Singe's Castle, Dragon's Lair II and Space Ace score DEMO either way, because their scenes move whether you press anything or not. Cosmo and Crystal Caves score STATIC either way and were checked by eye. Tyrian is the one real difference: in place it stops on its Players menu, because its scripted Downs and Enters land at different moments when the zip loads differently. It keeps its bundle until the sequence is retimed.

The same way, straight from the collection, fourteen of fifteen full-motion video discs from 196 to 541 MB booted: nine of them run attract sequences good enough for the reel. Man Enough (661 MB, two CD images and a floppy) hung. Dragon's Lair CD, Space Ace CD and Kingdom stop on setup screens that still need keys. Dracula Unleashed and The Lawnmower Man open with a minute of text on black, which reads as nothing at 128x128.

## The case

Retromaker's arcade cabinet from Printables, modified to fit. Printed on a Prusa M4S, sides in wood-toned PLA, the rest black. Small enough to hold between two fingers.

## Next steps

The reusable artifact is the pipe, not the arcade. Once the device is a draw-only terminal rendering rectangles from a server, the device stops mattering and the server can drive anything.

None of it is a new or complicated idea. Remote framebuffers are as old as VNC. What changed is the price of the receiving end: a board for about 15 dollars gets the memory and compute of whatever server it talks to. This began as a retrocomputing entry and turned into an arcade cabinet, but the same pipe fits a lot of places that have nothing to do with games.

- **Where devices get broken or stolen.** Galleries, museums, shop windows, info points. A 15 dollar screen that holds nothing is cheap to replace and worthless to take.
- **Replacing a PC or a tablet.** Anything that mostly displays and takes a tap back can live on the server, with the cheapest possible board on the wall.
- **Other screens.** The protocol sends rectangles at the size the client asks for. A larger panel works the same way; e-ink fits content that changes rarely.
- **Legacy HMI glances (the real one).** Virtualized/contained legacy control PCs still sometimes need a corner of their screen visible at an operator panel. A full thin client is overkill for a glance. A 15 dollar draw-only board rendering one rectangle of a remote framebuffer, no OS, no attack surface, fits the non-critical, non-Ex, "just show me that gauge over there" case. Boundaries stated plainly: not the primary HMI, not safety-critical, not hazardous areas.
- **Many screens, one surface (the fun one).** The server already slices frames into rectangles for a client that draws whatever arrives. That client can be fifty clients. Fifty tiles as one surface, flat or wrapped over a sphere, is the same protocol pointed at many terminals. No use case required.
- **Not a keyboard (the deliberate refusal).** The loose keys and GPIO pins are right there. Using them would make the thing work better and mean less. The premise is native peripherals only - a terminal made from a board that was never meant to be one. A keyboard would just make it a small computer with buttons.
