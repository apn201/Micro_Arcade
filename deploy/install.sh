#!/usr/bin/env bash
# Install or upgrade the micro-doom streaming service on a Linux host.
# Tested target: Raspberry Pi OS (Debian bookworm), arm64 and armhf.
#
#   sudo ./deploy/install.sh
#   sudo ./deploy/install.sh --dir /opt/micro-doom --user micro-doom
#   sudo ./deploy/install.sh --no-service     # dependencies and build only
#   sudo ./deploy/install.sh --no-deps        # skip apt, everything else
#
# Idempotent: safe to run again to upgrade an existing install. It never
# overwrites /etc/micro-doom.env once that exists, so configuration survives.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIR=/opt/micro-doom
USER_NAME=micro-doom
DO_SERVICE=1
DO_DEPS=1
DO_WAD=1

while [ $# -gt 0 ]; do
  case "$1" in
    --dir)        DIR="$2"; shift 2 ;;
    --user)       USER_NAME="$2"; shift 2 ;;
    --no-service) DO_SERVICE=0; shift ;;
    --no-deps)    DO_DEPS=0; shift ;;
    --no-wad)     DO_WAD=0; shift ;;
    -h|--help)    sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n>> %s\n' "$*"; }
die() { printf '\n!! %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run with sudo (needs to create a user and a systemd unit)"

# --- 1. dependencies ------------------------------------------------------
# Distro packages rather than pip: on a Pi, pip would rebuild numpy and Pillow
# from source for many minutes, and Debian bookworm refuses a system-wide pip
# install anyway (PEP 668).
if [ "$DO_DEPS" = 1 ] && command -v apt-get >/dev/null; then
  say "installing dependencies"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y --no-install-recommends \
    build-essential git ca-certificates curl \
    python3 python3-numpy python3-pil
fi

PYTHON=python3
command -v "$PYTHON" >/dev/null || die "python3 not found"

# --- 2. service user ------------------------------------------------------
if ! id -u "$USER_NAME" >/dev/null 2>&1; then
  say "creating system user $USER_NAME"
  useradd --system --create-home --home-dir "/var/lib/$USER_NAME" \
          --shell /usr/sbin/nologin "$USER_NAME"
fi

# --- 3. copy the tree -----------------------------------------------------
if [ "$(readlink -f "$SRC")" != "$(readlink -f "$DIR")" ]; then
  say "installing $SRC -> $DIR"
  mkdir -p "$DIR"
  if command -v rsync >/dev/null; then
    rsync -a --delete \
      --exclude '.git' --exclude 'server/work' --exclude '__pycache__' \
      --exclude '.venv' --exclude 'server/doom1.wad' \
      "$SRC"/ "$DIR"/
  else
    cp -a "$SRC"/. "$DIR"/
    rm -rf "$DIR/.git" "$DIR/server/work"
  fi
else
  say "installing in place at $DIR"
fi
chown -R "$USER_NAME:$USER_NAME" "$DIR"

# --- 4. python libraries, if the distro did not supply them ---------------
if ! sudo -u "$USER_NAME" "$PYTHON" -c 'import numpy, PIL' >/dev/null 2>&1; then
  say "numpy/Pillow missing from the system python, building a venv"
  sudo -u "$USER_NAME" "$PYTHON" -m venv "$DIR/.venv"
  sudo -u "$USER_NAME" "$DIR/.venv/bin/pip" install --upgrade pip
  sudo -u "$USER_NAME" "$DIR/.venv/bin/pip" install -r "$DIR/requirements.txt"
  PYTHON="$DIR/.venv/bin/python"
fi
say "python: $PYTHON"
sudo -u "$USER_NAME" "$PYTHON" -c 'import numpy, PIL; print("  numpy", numpy.__version__, "pillow", PIL.__version__)'

# --- 5. build headless DOOM ----------------------------------------------
say "building the DOOM frame source (a few minutes on a Pi)"
sudo -u "$USER_NAME" bash "$DIR/server/build-source.sh"

# --- 6. the WAD -----------------------------------------------------------
if [ "$DO_WAD" = 1 ] && [ ! -f "$DIR/server/doom1.wad" ]; then
  say "fetching the shareware WAD"
  sudo -u "$USER_NAME" bash "$DIR/server/fetch-wad.sh" || \
    die "WAD download failed -- copy doom1.wad to $DIR/server/ and re-run with --no-wad"
fi

# --- 7. configuration -----------------------------------------------------
if [ ! -f /etc/micro-doom.env ]; then
  say "writing /etc/micro-doom.env (edit it to configure)"
  install -m 0640 -o root -g "$USER_NAME" \
    "$DIR/deploy/micro-doom.env.example" /etc/micro-doom.env
else
  say "keeping existing /etc/micro-doom.env"
fi

# --- 8. systemd -----------------------------------------------------------
if [ "$DO_SERVICE" = 1 ]; then
  say "installing the systemd unit"
  sed -e "s|__DIR__|$DIR|g" \
      -e "s|__USER__|$USER_NAME|g" \
      -e "s|__PYTHON__|$PYTHON|g" \
      "$DIR/deploy/micro-doom.service" > /etc/systemd/system/micro-doom.service
  systemctl daemon-reload
  systemctl enable micro-doom >/dev/null
  systemctl restart micro-doom
  sleep 3
  systemctl --no-pager --lines=15 status micro-doom || true
fi

PORT="$(grep -E '^MD_PORT=' /etc/micro-doom.env 2>/dev/null | cut -d= -f2 || true)"
PORT="${PORT:-20002}"

cat <<EOF

>> installed.

   service   systemctl status micro-doom
   logs      journalctl -u micro-doom -f
   config    /etc/micro-doom.env   (then: systemctl restart micro-doom)
   verify    sudo -u $USER_NAME $PYTHON $DIR/service/tests/test_stream.py --doom

   The terminal talks UDP $PORT. On a LAN with a firewall:
       sudo ufw allow $PORT/udp

   Point the AtomS3R at this host by setting SERVER_HOST in client/config.py to
   this machine's LAN address:
EOF
hostname -I 2>/dev/null | tr ' ' '\n' | grep -v '^$' | sed 's/^/       /' || true
echo
