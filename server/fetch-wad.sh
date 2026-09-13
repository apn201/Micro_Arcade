#!/usr/bin/env bash
# Download the freely redistributable DOOM shareware IWAD.
#
# Shareware only. doom1.wad (episode 1) is the one id Software allowed to be
# passed around freely; doom.wad, doom2.wad and friends are commercial and must
# not end up in this repo or in anything published from it.
#
# If you own a retail WAD you can of course point the server at it locally --
# just keep it out of the repository and out of the project write-up.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${1:-$HERE/doom1.wad}"

# 4,196,020 bytes, md5 f0cefca49926d00903cf57551d901abe (shareware v1.9)
EXPECTED_SIZE=4196020
EXPECTED_MD5="f0cefca49926d00903cf57551d901abe"

MIRRORS=(
  "https://distro.ibiblio.org/slitaz/sources/packages/d/doom1.wad"
  "https://github.com/Akbar30Bill/DOOM_wads/raw/master/doom1.wad"
)

if [ -f "$DEST" ]; then
  echo "already have $DEST"
else
  for url in "${MIRRORS[@]}"; do
    echo ">> trying $url"
    if curl -fsSL --retry 2 -o "$DEST.part" "$url"; then
      mv "$DEST.part" "$DEST"
      break
    fi
    rm -f "$DEST.part"
  done
fi

if [ ! -f "$DEST" ]; then
  echo "could not download doom1.wad from any mirror." >&2
  echo "Grab it manually and drop it at $DEST" >&2
  exit 1
fi

size=$(wc -c < "$DEST")
echo ">> $DEST ($size bytes)"

if command -v md5sum >/dev/null; then
  md5=$(md5sum "$DEST" | cut -d' ' -f1)
  echo ">> md5 $md5"
  if [ "$md5" != "$EXPECTED_MD5" ]; then
    echo "!! not the expected shareware v1.9 checksum ($EXPECTED_MD5)."
    echo "!! It may still work -- other shareware revisions exist -- but check"
    echo "!! what you downloaded before shipping anything built on it."
  fi
fi

[ "$size" = "$EXPECTED_SIZE" ] || echo "!! unexpected size (wanted $EXPECTED_SIZE)"
