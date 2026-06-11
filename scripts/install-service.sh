#!/usr/bin/env bash
# Install kb-mcp as an always-on background service on macOS (launchd user agent).
#
# This is the cross-platform counterpart to scripts/install-service.ps1
# (Windows/NSSM). On Linux, use the systemd unit instead: scripts/kb-mcp.service
# (its header has the install steps). No sudo needed here — it's a per-user agent.
#
# Prereqs:
#   - .venv exists with kb-mcp installed, e.g.
#       python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[embeddings]"
#     (or `uv sync --extra embeddings`).
#   - .env in the repo root with the GitHub OAuth vars (KB_MCP_BASE_URL,
#     KB_MCP_GITHUB_USERNAME, GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET) and
#     KB_MCP_VAULT_PATH. See the Install section of README.md.
#
# Usage:     bash scripts/install-service.sh
# Override:  KB_MCP_BIND_HOST=127.0.0.1 KB_MCP_PORT=8765 bash scripts/install-service.sh
# Restart:   bash scripts/restart.sh            # after .env edits
# Uninstall: launchctl bootout gui/$(id -u)/com.kb-mcp && rm ~/Library/LaunchAgents/com.kb-mcp.plist

set -euo pipefail

LABEL="com.kb-mcp"
BIND_HOST="${KB_MCP_BIND_HOST:-127.0.0.1}"
PORT="${KB_MCP_PORT:-8765}"

# Resolve repo root from this script's own location (scripts/..), so it works
# regardless of the caller's working directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "This installer targets macOS (launchd)." >&2
    echo "On Linux, use the systemd unit: scripts/kb-mcp.service" >&2
    echo "  (see the 'Install as a service' section of README.md)." >&2
    exit 1
fi

VENV_PYTHON="$REPO_ROOT/.venv/bin/python"
LOG_DIR="$REPO_ROOT/logs"
PLIST_SRC="$SCRIPT_DIR/com.kb-mcp.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

[[ -x "$VENV_PYTHON" ]] || { echo "venv python not found at $VENV_PYTHON — create the venv and install kb-mcp first (see README)." >&2; exit 1; }
[[ -f "$REPO_ROOT/.env" ]] || { echo ".env missing in $REPO_ROOT — add the GitHub OAuth vars (see README Install)." >&2; exit 1; }
[[ -f "$PLIST_SRC" ]] || { echo "plist template missing at $PLIST_SRC" >&2; exit 1; }
mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"

# Render the template: substitute absolute paths + bind host/port.
sed -e "s|__VENV_PYTHON__|$VENV_PYTHON|g" \
    -e "s|__REPO_ROOT__|$REPO_ROOT|g" \
    -e "s|__BIND_HOST__|$BIND_HOST|g" \
    -e "s|__PORT__|$PORT|g" \
    "$PLIST_SRC" > "$PLIST_DEST"

# Reload cleanly: bootout any prior instance (ignore if absent), then bootstrap.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "Installed and started '$LABEL' bound to ${BIND_HOST}:${PORT}."
echo "  plist:   $PLIST_DEST"
echo "  logs:    $LOG_DIR/service.out.log (stdout), service.err.log (stderr), kb-mcp.log (app)"
echo "  status:  launchctl print gui/$(id -u)/$LABEL | grep -i state"
echo "  restart: bash scripts/restart.sh   (after .env edits)"
echo "  remove:  launchctl bootout gui/$(id -u)/$LABEL && rm \"$PLIST_DEST\""
