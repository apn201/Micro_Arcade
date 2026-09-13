# Deploying the server side to a Linux host

Written to be followed start to finish, by a person or an agent, on a Raspberry
Pi running Raspberry Pi OS (Debian bookworm). Nothing here needs a display, a
desktop session, or an internet-facing port.

**Scope:** this installs the DOOM and test-pattern sources. The kiosk's DOS
games also need Node, `npm install` in `server/jsdos/` and bundles built from
your own eXoDOS copy, which this script does not do yet.

**What gets installed:** a Python service (`service/`) and a small headless
DOOM binary it builds from source (`server/`). The service listens on **UDP
20002** for the AtomS3R terminal. Nothing else is exposed.

## What the host needs

- Linux, arm64/armhf/x86_64. Raspberry Pi OS, Debian or Ubuntu tested paths.
- ~400 MB of disk (mostly the doomgeneric checkout and build).
- Outbound internet **during install only**, to clone doomgeneric and fetch the
  shareware WAD. Neither is needed afterwards.
- sudo.

A Pi 3 or better is comfortable. The heavy per-frame work is a 128×128 resize
and JPEG encode, which is roughly 1-2 ms; DOOM itself is a 1993 engine.

## 1. Get the code onto the host

Either clone it, if this repository has a remote:

```bash
git clone <repo-url> ~/micro-doom
cd ~/micro-doom
```

or copy it from the machine that has it:

```bash
rsync -a --exclude .git --exclude server/work --exclude '*.wad' \
      ./ pi@raspi.local:~/micro-doom/
```

## 2. Install

```bash
cd ~/micro-doom
sudo ./deploy/install.sh
```

This is idempotent — re-run it to upgrade. It will:

1. `apt-get install` build-essential, git, python3, python3-numpy, python3-pil.
   Distro packages on purpose: pip would rebuild numpy and Pillow from source
   for many minutes on a Pi, and bookworm refuses system-wide pip anyway
   (PEP 668). If those packages turn out to be unavailable, the script falls
   back to a venv in the install directory.
2. Create the system user `micro-doom`.
3. Copy the tree to `/opt/micro-doom`.
4. Build the DOOM frame source — clones doomgeneric at a pinned commit and
   compiles ~80 C files. **Several minutes on a Pi.**
5. Download the shareware `doom1.wad` (4,196,020 bytes, md5
   `f0cefca49926d00903cf57551d901abe`).
6. Write `/etc/micro-doom.env` if it does not exist, and never touch it again.
7. Install, enable and start `micro-doom.service`.

Expected tail of the output: a `systemctl status` block showing
`active (running)`, then a list of the host's IP addresses.

## 3. Verify

```bash
sudo -u micro-doom python3 /opt/micro-doom/service/tests/test_stream.py --doom
```

This runs the whole pipeline against itself on loopback — no device, no
network. It takes about two minutes and must end with `all checks passed`.
It covers the handshake, authentication, fragment reassembly, all four
encodings, flow control, and recovery from 25% packet loss.

If that passes, the server side is correct and anything still wrong is the
device, the WiFi, or a firewall.

Check the live service separately:

```bash
systemctl status micro-doom
journalctl -u micro-doom -n 30
```

A healthy idle log shows the startup banner and then nothing until a terminal
connects. It should **not** be restarting in a loop.

## 4. Open the port

The terminal speaks UDP 20002 from the LAN:

```bash
sudo ufw allow 20002/udp        # only if ufw is active
```

Do **not** forward this port from the internet. Authentication is a single
shared token and the service runs a game process on your behalf; it is built
for a LAN.

## 5. Point the device at it

On the AtomS3R, in `client/config.py`:

```python
SERVER_HOST = "192.168.x.y"     # the Pi's LAN address
SERVER_PORT = 20002
TOKEN = ""                      # must match MD_TOKEN in /etc/micro-doom.env
```

The device's screen goes `WIFI` → `SERVER` → `CONNECTED`, then DOOM.

## Configuring

Everything lives in `/etc/micro-doom.env` as `MD_*` variables — one per option
of `service/run.py`. Edit, then:

```bash
sudo systemctl restart micro-doom
```

The two most likely to need changing:

- `MD_TOKEN` — set a shared secret and put the same string in the device config.
- `MD_SOURCE=test` — swaps DOOM for a test pattern that draws the live
  accelerometer values on the streamed image. That is the tilt calibration
  screen; use it to confirm `MD_TILT_TURN` / `MD_TILT_MOVE` / `MD_DEADZONE`,
  then set `MD_SOURCE=doom` again.

## Troubleshooting

**The build fails.** It needs `build-essential`. On a non-Debian host install a
C compiler and re-run with `--no-deps`. The build pins `-O2` for gcc and adds
`-fsigned-char`, which matters on ARM: plain `char` is unsigned there and DOOM
assumes otherwise, so without it the game misbehaves quietly rather than
crashing.

**The WAD download fails.** Both mirrors are third-party. Copy any shareware
`doom1.wad` to `/opt/micro-doom/server/` and re-run with `--no-wad`. Shareware
only — do not put a retail WAD in this repo.

**The service restarts in a loop.** `journalctl -u micro-doom -n 50`. The usual
causes are a missing WAD, an unbuilt `server/doom-source`, or a port already in
use.

**The device connects but the picture is stuck or blank.** That is almost
always the link rather than the host. The server logs a stats line every five
seconds with the estimated client decode cost; the device logs its own frame
rate when `DEBUG = True` in its config.

**Nothing connects at all.** Check the port is open and that both ends agree on
`MD_TOKEN`. `journalctl -u micro-doom -f` prints a line for every connection
attempt, including rejected ones and why.

## Uninstalling

```bash
sudo systemctl disable --now micro-doom
sudo rm /etc/systemd/system/micro-doom.service /etc/micro-doom.env
sudo systemctl daemon-reload
sudo rm -rf /opt/micro-doom
sudo userdel -r micro-doom
```
