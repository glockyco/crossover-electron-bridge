#!/bin/bash
# Launch Visseron Idle through the shared CrossOver Electron Bridge CLI.
# A terminal window opens to show status — close it to stop the server.

set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
PROJECT_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)"

if [ -n "${PYTHONPATH:-}" ]; then
  export PYTHONPATH="$PROJECT_ROOT/src:$PYTHONPATH"
else
  export PYTHONPATH="$PROJECT_ROOT/src"
fi

exec python3 -m crossover_electron_bridge launch visseron-idle
