#!/usr/bin/env bash
# Build the headless DOOM frame source.
#
#   ./build-source.sh              # native build
#   CC="zig cc" ./build-source.sh  # explicit compiler
#   ./build-source.sh clean
#
# Works on Windows (Git Bash + zig cc) and Linux (gcc/clang/zig cc). The
# binary has no dependencies beyond libc and sockets: no libjpeg, no threads,
# no X. Everything else lives in the Python service.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$HERE/work"
DG_DIR="$WORK/doomgeneric"
DG_SRC="$DG_DIR/doomgeneric"
DG_REPO="https://github.com/ozkl/doomgeneric.git"
DG_COMMIT="dcb7a8dbc7a16ce3dda29382ac9aae9d77d21284"

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) HOST_WIN=1 ;;
  *)                    HOST_WIN=0 ;;
esac

OUT="$HERE/doom-source"
[ "$HOST_WIN" = 1 ] && OUT="$OUT.exe"

if [ "${1:-build}" = "clean" ]; then
  rm -rf "$WORK" "$HERE/doom-source" "$HERE/doom-source.exe"
  echo "cleaned"
  exit 0
fi

# zig cc is the default because it is the one compiler that installs without
# admin on Windows and cross-compiles to Linux from the same box.
if [ -z "${CC:-}" ]; then
  if command -v zig >/dev/null; then CC="zig cc"
  elif command -v cc >/dev/null; then CC="cc"
  elif command -v gcc >/dev/null; then CC="gcc"
  else echo "no C compiler found (install zig, gcc or clang)" >&2; exit 1
  fi
fi
echo ">> compiler: $CC"

mkdir -p "$WORK"
if [ ! -d "$DG_DIR/.git" ]; then
  echo ">> cloning doomgeneric"
  git clone "$DG_REPO" "$DG_DIR"
fi
git -C "$DG_DIR" checkout --quiet "$DG_COMMIT"

cp "$HERE/src/doomgeneric_pipe.c" "$HERE/src/md_state.c" "$DG_SRC/"

SRC_DOOM="dummy.c am_map.c doomdef.c doomstat.c dstrings.c d_event.c d_items.c \
d_iwad.c d_loop.c d_main.c d_mode.c d_net.c f_finale.c f_wipe.c g_game.c \
hu_lib.c hu_stuff.c info.c i_cdmus.c i_endoom.c i_joystick.c i_scale.c \
i_sound.c i_system.c i_timer.c memio.c m_argv.c m_bbox.c m_cheat.c m_config.c \
m_controls.c m_fixed.c m_menu.c m_misc.c m_random.c p_ceilng.c p_doors.c \
p_enemy.c p_floor.c p_inter.c p_lights.c p_map.c p_maputl.c p_mobj.c \
p_plats.c p_pspr.c p_saveg.c p_setup.c p_sight.c p_spec.c p_switch.c \
p_telept.c p_tick.c p_user.c r_bsp.c r_data.c r_draw.c r_main.c r_plane.c \
r_segs.c r_sky.c r_things.c sha1.c sounds.c statdump.c st_lib.c st_stuff.c \
s_sound.c tables.c v_video.c wi_stuff.c w_checksum.c w_file.c w_main.c \
w_wad.c z_zone.c w_file_stdc.c i_input.c i_video.c doomgeneric.c \
doomgeneric_pipe.c md_state.c"

# zig cc (clang) miscompiles DOOM at -O1 and above: the game hangs during
# sprite/texture init with a corrupt texture hash chain, with or without
# -fwrapv and -fno-strict-aliasing. gcc has no such trouble, so optimise there
# and stay at -O0 under zig. DOOM is a 1993 engine -- even unoptimised it
# renders 320x200 far faster than the 35fps it asks for, and the heavy work in
# this system is in the Python service anyway.
# Override with MD_OPT=-O2 if you want to retest.
case "$CC" in
  *zig*) OPT="${MD_OPT:--O0}" ;;
  *)     OPT="${MD_OPT:--O2}" ;;
esac

# DOOM renders straight into the buffer the service scales from, so pin
# doomgeneric's framebuffer to DOOM's own resolution: no upscale to undo.
CFLAGS="$OPT -DDOOMGENERIC_RESX=320 -DDOOMGENERIC_RESY=200"

# 1993 C, compiled in 2026. The warnings are all noise from that gap, but the
# flag that silences them is compiler-specific: -Wno-everything is clang only.
case "$CC" in
  *zig*|*clang*) CFLAGS="$CFLAGS -Wno-everything" ;;
  *)            CFLAGS="$CFLAGS -w" ;;
esac

# DOOM shifts negative ints and puns pointers all over the place. That is
# undefined behaviour by the letter of the standard and has been fine in
# practice for thirty years -- but zig cc turns UBSan traps on by default, and
# the first trap is `SHORT(patch->leftoffset)<<FRACBITS` during sprite init.
# Untrapped it runs correctly; trapped it dies before the first frame.
CFLAGS="$CFLAGS -fno-strict-aliasing"
case "$CC" in
  *zig*|*clang*) CFLAGS="$CFLAGS -fno-sanitize=undefined" ;;
esac

# On ARM -- a Raspberry Pi, say -- plain `char` is unsigned, and DOOM's code
# assumes the x86 default of signed. This is the classic way a 1993 codebase
# breaks on a Pi, and it breaks quietly: bad lookups rather than a crash.
CFLAGS="$CFLAGS -fsigned-char"

LIBS=""

if [ "$HOST_WIN" = 1 ]; then
  CFLAGS="$CFLAGS -DWIN32 -D_CRT_SECURE_NO_WARNINGS"
  LIBS="-lws2_32"
else
  CFLAGS="$CFLAGS -DNORMALUNIX -DLINUX -D_DEFAULT_SOURCE"
fi

echo ">> building $(basename "$OUT")"
cd "$DG_SRC"
# shellcheck disable=SC2086
$CC $CFLAGS $SRC_DOOM -o "$OUT" $LIBS

echo ">> built $OUT"
