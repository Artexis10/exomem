#!/usr/bin/env bash
# Restart the exomem background service after a .env edit.
#   macOS -> launchd agent (com.exomem);  Linux -> systemd --user service (exomem).
# Truncates logs/exomem.log so the post-restart tail shows only this session,
# then tails it. Cross-platform counterpart to scripts/restart.ps1 (Windows).
#
# Usage: bash scripts/restart.sh

set -euo pipefail

LABEL="com.exomem"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG="$REPO_ROOT/logs/exomem.log"

# Truncate the app log (keep the file, just empty it).
: > "$LOG" 2>/dev/null || true

case "$(uname -s)" in
    Darwin)
        launchctl kickstart -k "gui/$(id -u)/$LABEL"
        echo "Restarted launchd agent '$LABEL'."
        ;;
    Linux)
        systemctl --user restart exomem
        echo "Restarted systemd user service 'exomem'."
        ;;
    *)
        echo "Unsupported platform $(uname -s). On Windows use scripts/restart.ps1." >&2
        exit 1
        ;;
esac

# Give the app a beat to write its startup banner, then tail.
sleep 2
if [[ -s "$LOG" ]]; then
    echo
    echo "Log tail:"
    tail -n 8 "$LOG"
else
    echo "No log output at $LOG yet — service may still be initializing." >&2
fi
