#!/bin/bash
# Build an installable Decky plugin zip: out/deck-as-controller.zip
set -euo pipefail

NAME="deck-as-controller"
cd "$(dirname "$0")/.."
pnpm build >/dev/null

STAGE="$(mktemp -d)"
mkdir -p "$STAGE/$NAME/dist"
cp dist/index.js "$STAGE/$NAME/dist/"
cp main.py plugin.json package.json README.md "$STAGE/$NAME/"
rsync -a --exclude __pycache__ py_modules "$STAGE/$NAME/"

mkdir -p out
rm -f "out/$NAME.zip"
(cd "$STAGE" && zip -qr - "$NAME") > "out/$NAME.zip"
rm -rf "$STAGE"
echo "out/$NAME.zip"
