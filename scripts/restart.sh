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
PROFILE="${EXOMEM_PROFILE:-hybrid}"

. "$SCRIPT_DIR/_service-common.sh"

# --- Preflight against the venv the service ACTUALLY runs ------------------------
# This script used to restart blind. Its Windows twin gates on `doctor` first, and
# the same reasoning applies here: a restart that brings the service back broken is
# worse than refusing to restart, because the failure surfaces later and elsewhere.
# Gate the interpreter recorded in the installed unit, never "$REPO_ROOT/.venv" --
# a release install does not run the repo venv.
SERVICE_PY="$(exomem_service_python || true)"
if [[ -n "$SERVICE_PY" && -x "$SERVICE_PY" ]]; then
    DOCTOR_ARGS=(-m exomem doctor --profile "$PROFILE")
    VAULT="$(exomem_dotenv_value "$REPO_ROOT" EXOMEM_VAULT_PATH)"
    [[ -z "$VAULT" ]] && VAULT="${EXOMEM_VAULT_PATH:-}"
    [[ -n "$VAULT" ]] && DOCTOR_ARGS+=(--vault "$VAULT")
    echo "Preflight: exomem doctor --profile $PROFILE..."
    if ! "$SERVICE_PY" "${DOCTOR_ARGS[@]}"; then
        echo "Doctor preflight failed for profile '$PROFILE'; service NOT restarted." >&2
        echo "Fix the findings, or set EXOMEM_PROFILE to the profile this box runs." >&2
        exit 1
    fi
else
    echo "Could not resolve the service interpreter from the installed unit; skipping preflight." >&2
fi

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
