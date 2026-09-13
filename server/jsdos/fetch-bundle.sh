#!/usr/bin/env bash
# Fetch a .jsdos bundle for the DOS library.
#
#   ./fetch-bundle.sh                              # digger, the test bundle
#   ./fetch-bundle.sh https://.../something.jsdos
#   ./fetch-bundle.sh https://.../x.jsdos mygame.jsdos
#
# Bundles are NOT committed to this repository, deliberately: the same posture
# applies as to DOOM's WAD. Fetch freeware at runtime, build everything else
# locally from a collection you own (see from-exodos.py), commit nothing.
#
# Digger is a reasonable default: Windmill Software released it as freeware in
# 1998, and js-dos hosts it as their own demo bundle.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
URL="${1:-https://v8.js-dos.com/bundles/digger.jsdos}"
DEST="${2:-$HERE/$(basename "$URL")}"

if [ -f "$DEST" ]; then
  echo "already have $DEST ($(wc -c < "$DEST") bytes)"
  exit 0
fi

echo ">> $URL"
curl -fsSL --retry 2 -o "$DEST.part" "$URL"
mv "$DEST.part" "$DEST"

size=$(wc -c < "$DEST")
echo ">> $DEST ($size bytes)"

# A .jsdos bundle is a zip; anything else means we saved an error page.
if ! head -c 2 "$DEST" | grep -q "PK"; then
  echo "!! that is not a zip archive -- the URL probably returned an error page" >&2
  rm -f "$DEST"
  exit 1
fi

echo
echo "   play it:  python service/run.py --source jsdos --bundle $DEST"
