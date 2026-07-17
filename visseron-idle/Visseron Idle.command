#!/bin/bash
# Double-click this file to launch Visseron Idle in your browser.
# A terminal window opens to show status — close it to stop the server.

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SERVER="$SCRIPT_DIR/server.py"

if [ ! -f "$SERVER" ]; then
  osascript -e 'display notification "server.py not found beside launcher" with title "Visseron Idle"'
  exit 1
fi

exec python3 "$SERVER"
