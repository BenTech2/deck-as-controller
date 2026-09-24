#!/bin/bash
# Build the plugin and install it on a Steam Deck over SSH (dev loop).
# Usage: scripts/deploy.sh [deck@host]   (needs passwordless sudo on the Deck)
set -euo pipefail

DECK="${1:-${DECK:-deck@192.168.1.136}}"
KEY="${DECK_SSH_KEY:-$HOME/.ssh/id_ed25519_steamdeck}"
NAME="deck-as-controller"
SSH=(ssh -i "$KEY" "$DECK")

cd "$(dirname "$0")/.."
pnpm build >/dev/null

STAGE="$(mktemp -d)/$NAME"
mkdir -p "$STAGE"
cp -R dist main.py plugin.json package.json "$STAGE/"
rsync -a --exclude __pycache__ py_modules "$STAGE/"
rm -f "$STAGE/dist/"*.map

rsync -a --delete -e "ssh -i $KEY" "$STAGE/" "$DECK:/tmp/$NAME/"
"${SSH[@]}" "sudo rm -rf ~/homebrew/plugins/$NAME && sudo cp -R /tmp/$NAME ~/homebrew/plugins/$NAME \
  && sudo systemctl restart plugin_loader"
echo "Installed $NAME on $DECK and restarted Decky."
