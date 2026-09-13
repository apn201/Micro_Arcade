# The source protocol — how a game talks to the service

This is the *inside* interface: between a game and the streaming service, over
loopback TCP. It is intentionally trivial, because everything that is hard
(scaling, diffing, encoding, transport, input mapping) belongs on the service
side where it is shared by every game.

Python sources implement the `Source` class in
`service/microstream/sources/base.py` and never touch this wire format. It
exists for out-of-process games — currently DOOM
(`server/src/doomgeneric_pipe.c`).

## Lifecycle

The service binds an **ephemeral** loopback port, spawns the game with
`--connect 127.0.0.1:PORT`, and waits for it to dial in. Nothing has to agree
on a port number up front, and two sessions never collide.

```
service                                game
  bind 127.0.0.1:0, listen
  spawn game --connect 127.0.0.1:N
                          <---------   connect
                          <---------   frame, frame, frame...
  key events              --------->
```

Closing the socket ends the game; the game exiting ends the session.

## Game → service: frames

```
"DGF1" | u16 width | u16 height | u8 bpp | u8 state | u16 reserved | u32 length | pixels
```

16-byte header, little-endian. `length` is `width*height*bpp`, and `bpp` says
how the pixels are laid out:

| bpp | layout | used by |
|-----|--------|---------|
| 4 | `0x00RRGGBB` — B,G,R,X in memory on a little-endian host | DOOM |
| 3 | RGB24 | js-dos |

Both are accepted because converting inside the engine would copy every frame
for nothing; numpy reinterprets either layout for free.

Frames are sent as fast as the game renders (DOOM: 35fps, ~256 kB each, ~9 MB/s
over loopback — irrelevant locally, and the service keeps only the newest
one). A source must never queue frames: on a slow link the freshest frame is
the only one worth having.

`state` lets the input profile adapt without the service knowing anything about
the game:

| value | meaning                         |
|-------|---------------------------------|
| 0     | playing                         |
| 1     | a menu is up                    |
| 2     | player is dead, awaiting respawn |
| 3     | intermission, finale, title/demo |

This one byte is what lets a single button mean ENTER in a menu, USE when dead,
and FIRE in play.

## Service → game: input

```
u8 type | u8 pressed | u8  key            (type 1)
u8 type | u8 pressed | u16 key            (type 3)
u8 type | u8 0       | u8  0              (type 2)
```

`type` 1 is a key transition carrying an 8-bit code — DOOM's own, from
`doomkeys.h`. `type` 3 is the same thing with a 16-bit code, which js-dos
needs: its codes are GLFW-style and run past 255 (`KBD_left` is 263). `type` 2
asks the game to quit.

A source declares which width it wants via `PipeSource.key_width`. The table of
js-dos key names to codes lives in `service/microstream/keys.py`, transcribed
from the package's own type definitions rather than guessed — the DOSBox
`KBD_KEYS` enum ordinals would have looked plausible and typed garbage.

The service sends key codes, not buttons: the mapping from tilt and buttons to
keys is the input profile's job (`service/microstream/profiles.py`), which is
where it can be retuned without touching the game or the device.

## Adding another game

Two options, and the in-process one is usually right:

1. **In-process Python source** — implement `Source.poll()` and
   `Source.send_key()`. That is all a headless-browser or VNC source needs.
2. **Out-of-process** — subclass `PipeSource` and supply `build_command()`.
   Framing, resynchronisation, frame-dropping and shutdown are all inherited,
   so DOOM (C) and js-dos (Node) are about 60 lines each on top of it.
