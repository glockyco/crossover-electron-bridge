#!/bin/bash
# Launch Fractured Realms through Steam, with its UI hosted in the native browser.
# The Steam-tracked Electron process stays alive for playtime/presence.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
LAUNCHER="$SCRIPT_DIR/patch-and-launch.py"

if [ ! -f "$LAUNCHER" ]; then
  osascript -e 'display notification "patch-and-launch.py not found beside launcher" with title "Fractured Realms"'
  exit 1
fi

exec python3 "$LAUNCHER"
