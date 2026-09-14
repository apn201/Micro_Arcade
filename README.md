# Micro Arcade

An arcade cabinet built around an M5Stack AtomS3R that plays 27 DOS games and DOOM, and runs a demo reel of 41 titles, none of which run on the AtomS3R. The screen is a terminal. The games run somewhere else.

The device has a 128x128 screen, a motion sensor, WiFi, and one button. That is enough to be a terminal and nothing more. It draws whatever the server sends and reports back a tilt and a button. It does not know what DOOM is, what a deadzone is, or which game is running. All of that lives on the server, so the same firmware drives every game, and swapping a game is a server-side change the device never notices.

The arcade cabinet is the demo. The point is the pipe: once the device is a draw-only terminal that renders rectangles from a server, the device stops mattering and the server can drive anything.

## How it works

```
AtomS3R (MicroPython)  <--- UDP framebuffer rects ---  Python server
   draws rectangles         --- tilt + button input -->    encodes frames,
   sends input                                             runs the games
```

- The device runs `main.py` and a `config.py` on stock UIFlow2 MicroPython. No custom firmware build, no GPIO, no extra hardware.
- The server encodes each frame as a set of dirty rectangles, prices each encoding in *client* milliseconds (decode + transfer + per-datagram cost), and picks whatever the device can finish fastest. JPEG usually wins; the client advertises what it can decode in its HELLO packet and the server routes around anything missing.
- Input is 18 bytes, 30 times a second: raw button bits and raw accelerometer values in milli-g. The device sends readings, not decisions.
- DOOM runs as a compiled binary (doomgeneric) pushing frames over loopback. DOS games run in js-dos (DOSBox compiled to WebAssembly) headless in Node, one process per game, loaded straight from the eXoDOS zips. Videos are decoded in the service itself. All of them feed the same Python service, which streams to the device.

## Stack

| where | what |
|---|---|
| device | AtomS3R, UIFlow2 MicroPython v1.27, `main.py` + `config.py`, no custom firmware build |
| server | Python 3 with numpy and Pillow. OpenCV only if you want video titles |
| DOOM | doomgeneric, C, compiled with `zig cc` on Windows because it installs without admin (gcc on Linux) |
| DOS | js-dos `emulators` 8.4.2: DOSBox compiled to WebAssembly, running headless in Node |
| games | eXoDOS, run in place from your own collection. Not included |

The demo server runs on a PC because the eXoDOS library lives there and a headless DOSBox on a Raspberry Pi is a fight not worth picking to prove the idea. Nothing about the device changes based on which machine is sending. A Pi can serve DOOM today ([deploy/DEPLOY.md](deploy/DEPLOY.md)).

## The wire protocol

UDP on port 20002, little-endian. Every packet starts with the same 4 bytes.

```
common header   magic 0x4D ('M') u8 | type u8 | session u16

HELLO     0x01  ver u8 | caps u8 | width u16 | height u16 | token_len u8 | token
INPUT     0x02  seq u32 | buttons u16 | ax i16 | ay i16 | az i16 | last_frame_id u16
FRAME     0x82  frame_id u16 | frag_idx u8 | frag_count u8 | flags u8 | length u16
  update        rect_count u8 | game_state u8 | pad u16
  rect          x u16 | y u16 | w u16 | h u16 | encoding u8 | length u16 | payload
```

Encodings: `SOLID` (one RGB565 colour), `RAW565`, `DEFLATE565`, `JPEG`. Full reference in [docs/PROTOCOL.md](docs/PROTOCOL.md); the game-side interface is [docs/SOURCE_PROTOCOL.md](docs/SOURCE_PROTOCOL.md).

Key design choices, and the bugs that forced them, are written up in full in [NOTES.md](NOTES.md). Short version: the server diffs against the last frame the device *acknowledged* (not the last one it encoded), fragments are paced ~3 ms apart because the device's UDP socket holds one datagram, and WiFi modem sleep is disabled on the device or the radio naps through the frame budget.

## Controls

- Tilt is the joystick. Server smooths the readings (EMA), captures "level" from the first half-second instead of assuming a flat table, and puts hysteresis on the deadzone.
- The button is the whole screen. Click is the main action, double-click is a second action (DOOM: open door), long-press always exits to the menu and no game may bind it.
- Long press also takes the pose you're holding the device in as the new "level", for the menu and the next game.
- Double click can be set to skip to the next title instead, in the menu's games and in the demo reel (`double_click: "next"` in the library).
- In the menu, tilt forward and back to move the selection and click to start.
- Driving and flying games fit this scheme best. Anything wanting a keyboard, a mouse, or a held button does not.

## Games

Each game gets a scripted boot sequence that walks it all the way into gameplay - past the sound-card prompt, the title screens, the pilot-name entry - because the cabinet has no keyboard and the menu text is unreadable at 128x128. Boot sequences are data in the library file. A helper tool, `audition.py`, drives the keys and screenshots the result, scoring each game PLAY / DEMO / STATIC / TEXT by whether the picture answered the controls, not just whether it moved.

Current library: driving (Stunts, Test Drive III, Stunt Driver, Indianapolis 500, SkyRoads), flight (F-15 Strike Eagle II), shooters (Wolfenstein 3D, Blake Stone, Catacomb 3-D, Heretic, Rise of the Triad, Major Stryker, Tyrian), platformers (Duke Nukem, Commander Keen 4, Cosmo's Cosmic Adventure, Jill of the Jungle, Crystal Caves, Bio Menace, Hocus Pocus, Prehistorik 2, Prince of Persia), laserdisc games (Dragon's Lair, Dragon's Lair: Singe's Castle, Dragon's Lair II, Space Ace), plus DOOM (native binary) and Digger. That's 28 titles in the menu: 27 DOS games and DOOM.

## Demo reel

The menu only lists what can be played. The demo reel is the cabinet's attract mode, with its own catalogue: it boots real games live, loading screens and all, with the controls working, and moves to the next one after a while or on a double click. It starts from the last menu row, by itself after the menu has sat untouched for two minutes, or straight away with `--reel`.

Nobody is holding the cabinet in attract mode, so the reel plays the games too, once their boot keys are done: forward and fire for the shooters, throttle and a little steering for the racing games, run and jump for the platformers. Press the screen or tilt and your input takes over at once; after six idle seconds the autopilot picks up again. The attract sequences of the laserdisc and full-motion video games are left alone.

Its catalogue adds titles that can't be played with a tilt and a tap: Space Ace II and Guy Spy run their intros, and the full-motion video CD games run their attract sequences straight from the eXoDOS collection: Mad Dog McCree and Mad Dog II, Crime Patrol, Drug Wars, Who Shot Johnny Rock, Space Pirates, Los Justicieros, Brain Dead 13, Rebel Assault and Fort Boyard. Up to 456 MB of disc each, on a board with 8 MB of memory. And Skyrim, which is not emulated and not interactive. It's a video, streamed through the same pipe as everything else, because the device can't tell a recording from a game. Second Reality has a slot for a video too.

That makes 41 titles in the reel: all 28 from the menu, 12 DOS titles that are only worth watching, and Skyrim. Second Reality's slot makes it 42 once its video is there.

Which catalogue an entry belongs to, how long the reel stays on a title and what double click does are all set in the library file.

Most boot into gameplay in about 18 seconds. Some games were auditioned and cut for wanting a mouse, a serial number, a copy-protection code, or a briefing video that never ends. Those stories are in [NOTES.md](NOTES.md).

## What is not in this repository

No game data, on purpose.

- **DOOM:** `server/fetch-wad.sh` downloads the shareware `doom1.wad`, which id Software allowed to be shared. Retail WADs stay out.
- **Digger:** `server/jsdos/fetch-bundle.sh` downloads the freeware bundle js-dos hosts.
- **DOS titles:** loaded in place from your own eXoDOS collection, or from bundles built from it. Bundles are gitignored. `kiosk/games.example.json` only has titles, launch keys and control mappings.
- **Videos:** put your own file in `kiosk/media/`, which is gitignored.

Your WiFi password lives in `client/config.py`, which is gitignored too.

## Repository layout

```
client/            AtomS3R firmware: main.py, config.example.py, caps_test.py
service/           the Python streaming service
  run.py           entry point, every option documented in --help
  microstream/     protocol, encoder, server loop, input profiles, key tables
    sources/       doom, jsdos, kiosk (menu and demo reel), video, test pattern
  clients/         pc_viewer.py, the device simulated on a PC
  tests/           end-to-end stream test, kiosk, controls, video, firmware harness
server/            the parts that are not Python
  src/             doomgeneric platform layer (C)
  build-source.sh  builds DOOM, fetch-wad.sh gets the shareware WAD
  jsdos/           Node backend, from-exodos.py, make-bundle.js,
                   build-library.py, audition.py, probe.js
kiosk/             games.example.json (the library), media/ for your videos
docs/              PROTOCOL.md, SOURCE_PROTOCOL.md, TROUBLESHOOTING.md
deploy/            systemd unit and installer for a Linux host
```

## Running it

Shell scripts need bash. On Windows that means Git Bash; for the C compiler, `winget install zig.zig` works without admin.

### Server

```bash
pip install -r requirements.txt
```

```bash
pip install opencv-python-headless
```

The second one is only for video titles. Without it they are left out of the menu.

DOOM:

```bash
cd server && ./build-source.sh && ./fetch-wad.sh && cd ..
```

DOS games need Node. Install the emulator and fetch Digger:

```bash
cd server/jsdos && npm install && ./fetch-bundle.sh && cd ../..
```

Then tell the kiosk where your eXoDOS collection is: `"exodos": {"root": "D:\eXoDOS"}` in `kiosk/games.json`, the `EXODOS_ROOT` environment variable, or `--exodos-root`. Titles run in place from the collection's own zips, CD images included, and nothing is copied. A title that needs more memory than the `max_mb` limit (700 by default) is left out.

Building bundles is optional, for a machine without the collection:

```bash
python server/jsdos/build-library.py --root /path/to/eXoDOS
```

Start the kiosk:

```bash
python service/run.py --source kiosk
```

It reads `kiosk/games.json` if you made one, otherwise `kiosk/games.example.json`. Entries whose bundle or video isn't there are skipped, so the menu only shows what actually runs. Add `--reel` to start in the demo reel.

Other sources: `--source doom`, `--source jsdos --bundle game.jsdos`, `--source video --video clip.mp4`, and `--source test`, which draws the live accelerometer values on screen for tuning the tilt. Every control knob is a server flag, see `python service/run.py --help` and [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

### Try it without the device

```bash
python service/clients/pc_viewer.py --mode tilt
```

Arrow keys tilt, Ctrl is a tap on the screen, holding Ctrl for a second goes back to the menu.

### The device

1. Run `client/caps_test.py` on the AtomS3R once. It measures what the firmware can decode and how fast.
2. Copy `client/config.example.py` to `client/config.py` and fill in WiFi and the server's IP address.
3. Upload `client/main.py` and `client/config.py` to the device (UIFlow2, Thonny or `mpremote cp client/main.py :main.py`) and reset.
4. Allow UDP 20002 through the server's firewall. The screen goes `WIFI`, `SERVER`, `CONNECTED`, then the menu.

### Tests

```bash
python service/tests/test_stream.py
```

The whole pipeline on loopback, including recovery from 25% packet loss. Also `test_kiosk.py`, `test_reel.py`, `test_exodos.py`, `test_profile.py`, `test_video.py`, and `firmware_harness.py`, which runs the real `client/main.py` on the PC.

## What this is actually for

The arcade cabinet is a technical demo. The reusable part is the pipe: a sub-€20, draw-only device that renders a slice of a remote framebuffer over UDP with no OS and no attack surface. A few directions that follow from that, none of them gaming:

- **Legacy HMI glances.** Old control PCs get virtualized and contained because a Win95 box does not belong on a plant floor. But an operator sometimes needs to *see* a corner of that screen at a panel. A full thin client is cost and overkill for a glance at a readout. A cheap board that draws one rectangle of a virtualized machine's framebuffer is a reasonable answer for the non-critical, non-Ex, "I just need to see this one gauge over there" case. Not the primary HMI, not safety-critical, not hazardous areas - but there is a real, cost-sensitive middle ground where this fits.
- **Many screens as one surface.** The server already slices a frame into rectangles for a client that draws whatever arrives. That client can be one screen or fifty. Fifty of these tiles arranged as a single surface - flat, or wrapped over a sphere - is the same protocol pointed at many draw-only terminals at once. No use case required. It would look sick.
- **Not** a keyboard. The loose keys and the GPIO pins are right there, and adding them would make the thing work better and mean less. The premise is native peripherals only. A keyboard would make it a small computer with buttons instead of a terminal made from a board that was never meant to be one.

## Credits

- [doomgeneric](https://github.com/ozkl/doomgeneric) (GPL-2.0), fetched and built by `server/build-source.sh`. DOOM by id Software.
- [js-dos](https://js-dos.com) `emulators` (GPL-2.0), installed from npm. DOSBox by the DOSBox team.
- eXoDOS, the DOS collection the library is built from. Not included.
- OpenCV, for video titles.
- Case based on Retromaker's arcade cabinet on Printables, modified to fit.

## License

GPL-2.0, see [LICENSE](LICENSE). The same license as doomgeneric and the js-dos emulators this builds against.

Game data is not covered by it and not included. DOOM, the DOS titles and anything you stream belong to their owners.
