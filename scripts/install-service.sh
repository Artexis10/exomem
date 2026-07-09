#!/usr/bin/env bash
# Install or update exomem as a per-user service on macOS (launchd) or Linux
# (systemd --user). Release mode is the product path; repo-dev keeps the checkout
# .venv path available for contributors.
#
# Product install:
#   bash scripts/install-service.sh --release
#
# Developer install:
#   bash scripts/install-service.sh --repo-dev --profile standard
#
# Re-run the same command after package or .env changes. No sudo is required.

set -euo pipefail

LABEL="com.exomem"
SERVICE_NAME="exomem"
MODE="repo-dev"
PROFILE="standard"
BIND_HOST="${EXOMEM_BIND_HOST:-127.0.0.1}"
PORT="${EXOMEM_PORT:-8765}"
SERVICE_ROOT=""
PACKAGE_VERSION=""
ENV_FILE=""
LEGACY_MCP_COMPAT=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
    cat <<'EOF'
Usage: bash scripts/install-service.sh [options]

Modes:
  --release                 Create/update a PyPI-backed service venv (product path)
  --repo-dev                Use the checkout .venv (default for compatibility)

Options:
  --profile lean|hybrid|standard|media
  --service-root PATH       Override release state/venv location
  --package-version VERSION Pin the PyPI release version
  --env-file PATH           Dotenv file (default: <repo>/.env)
  --bind-host HOST          Service bind host (default: 127.0.0.1)
  --port PORT               Service port (default: 8765)
  --legacy-mcp-compat       Set EXOMEM_MCP_LEGACY_COMPAT=1 in the service
  -h, --help                Show this help
EOF
}

die() {
    echo "error: $*" >&2
    exit 1
}

require_value() {
    [[ $# -ge 2 && -n "$2" ]] || die "$1 requires a value"
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --release)
            MODE="release"
            shift
            ;;
        --repo-dev)
            MODE="repo-dev"
            shift
            ;;
        --profile)
            require_value "$@"
            PROFILE="$2"
            shift 2
            ;;
        --service-root)
            require_value "$@"
            SERVICE_ROOT="$2"
            shift 2
            ;;
        --package-version)
            require_value "$@"
            PACKAGE_VERSION="$2"
            shift 2
            ;;
        --env-file)
            require_value "$@"
            ENV_FILE="$2"
            shift 2
            ;;
        --bind-host)
            require_value "$@"
            BIND_HOST="$2"
            shift 2
            ;;
        --port)
            require_value "$@"
            PORT="$2"
            shift 2
            ;;
        --legacy-mcp-compat)
            LEGACY_MCP_COMPAT=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1 (run with --help)"
            ;;
    esac
done

case "$PROFILE" in
    lean|hybrid|standard|media) ;;
    *) die "--profile must be lean, hybrid, standard, or media" ;;
esac
if [[ ! "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
    die "--port must be an integer from 1 to 65535"
fi

OS="$(uname -s)"
ARCH="$(uname -m)"
CURRENT_USER="${USER:-$(id -un)}"
case "$OS" in
    Darwin)
        CONFIG_ROOT="$HOME/Library/Application Support/Exomem"
        DEFAULT_SERVICE_ROOT="$CONFIG_ROOT/service"
        PLIST_SRC="$SCRIPT_DIR/com.exomem.plist"
        PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
        require_command launchctl
        ;;
    Linux)
        CONFIG_ROOT="${XDG_CONFIG_HOME:-$HOME/.config}/exomem"
        DEFAULT_SERVICE_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/exomem/service"
        UNIT_SRC="$SCRIPT_DIR/exomem.service"
        UNIT_DEST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$SERVICE_NAME.service"
        require_command systemctl
        ;;
    *)
        die "unsupported platform $OS; on Windows use scripts/install-service.ps1"
        ;;
esac

SERVICE_ROOT="${SERVICE_ROOT:-$DEFAULT_SERVICE_ROOT}"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"
SERVICE_ENV_FILE="$CONFIG_ROOT/service.env"
LAUNCHD_ENV_FILE="$CONFIG_ROOT/launchd-environment.xml"

[[ -f "$ENV_FILE" ]] || die "dotenv file not found: $ENV_FILE"
require_command curl

if [[ "$MODE" == "release" ]]; then
    require_command uv
    VENV_DIR="$SERVICE_ROOT/.venv"
    VENV_PYTHON="$VENV_DIR/bin/python"
    LOG_DIR="$SERVICE_ROOT/logs"
    WORKING_DIRECTORY="$SERVICE_ROOT"
    mkdir -p "$SERVICE_ROOT" "$LOG_DIR" "$CONFIG_ROOT"

    if [[ ! -x "$VENV_PYTHON" ]]; then
        echo "Creating release service venv at $VENV_DIR..."
        uv venv "$VENV_DIR" --python 3.13
    fi

    case "$PROFILE" in
        lean)
            EXTRAS=""
            ;;
        hybrid)
            EXTRAS="embeddings"
            ;;
        standard)
            EXTRAS="embeddings,media"
            if [[ "$OS" == "Darwin" && "$ARCH" == "arm64" ]]; then
                EXTRAS="$EXTRAS,media-mlx"
            fi
            ;;
        media)
            EXTRAS="embeddings,media,vision,diarization"
            if [[ "$OS" == "Darwin" && "$ARCH" == "arm64" ]]; then
                EXTRAS="$EXTRAS,media-mlx"
            fi
            ;;
    esac
    PACKAGE_REQUIREMENT="exomem"
    [[ -n "$EXTRAS" ]] && PACKAGE_REQUIREMENT="exomem[$EXTRAS]"
    [[ -n "$PACKAGE_VERSION" ]] && PACKAGE_REQUIREMENT="$PACKAGE_REQUIREMENT==$PACKAGE_VERSION"

    echo "Installing $PACKAGE_REQUIREMENT into the release service venv..."
    uv pip install --upgrade --python "$VENV_PYTHON" "$PACKAGE_REQUIREMENT"
else
    VENV_PYTHON="$REPO_ROOT/.venv/bin/python"
    LOG_DIR="$REPO_ROOT/logs"
    WORKING_DIRECTORY="$REPO_ROOT"
    PACKAGE_REQUIREMENT="repository .venv"
    [[ -x "$VENV_PYTHON" ]] || die \
        "venv python not found at $VENV_PYTHON; run uv sync or use --release"
    mkdir -p "$LOG_DIR" "$CONFIG_ROOT"
fi

[[ -x "$VENV_PYTHON" ]] || die "service python is not executable: $VENV_PYTHON"

PROCESS_ENV_FILE="$(mktemp "$CONFIG_ROOT/installer-env.XXXXXX")"
cleanup() {
    rm -f "$PROCESS_ENV_FILE"
}
trap cleanup EXIT

# Parse dotenv with the package's own dependency, render systemd/launchd-safe
# forms, and create a shell-quoted temporary export file for doctor.
"$VENV_PYTHON" - \
    "$ENV_FILE" \
    "$SERVICE_ENV_FILE" \
    "$PROCESS_ENV_FILE" \
    "$LAUNCHD_ENV_FILE" \
    "$LOG_DIR" \
    "$LEGACY_MCP_COMPAT" <<'PY'
from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from dotenv import dotenv_values

env_path, systemd_path, process_path, xml_path, log_dir, legacy = sys.argv[1:]
values = {
    key: str(value)
    for key, value in dotenv_values(env_path).items()
    if value is not None
}
for key, value in values.items():
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        raise SystemExit(f"invalid environment variable name in {env_path}: {key}")
    if "\n" in value or "\r" in value:
        raise SystemExit(f"multiline service environment values are not supported: {key}")

if not values.get("EXOMEM_VAULT_PATH", "").strip():
    raise SystemExit(f"EXOMEM_VAULT_PATH is required in {env_path}")
values.setdefault("EXOMEM_LOG_DIR", log_dir)
if os.environ.get("PATH"):
    values.setdefault("PATH", os.environ["PATH"])
if legacy == "1":
    values["EXOMEM_MCP_LEGACY_COMPAT"] = "1"

def systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

Path(systemd_path).write_text(
    "".join(f"{key}={systemd_quote(value)}\n" for key, value in values.items()),
    encoding="utf-8",
)
Path(process_path).write_text(
    "".join(f"export {key}={shlex.quote(value)}\n" for key, value in values.items()),
    encoding="utf-8",
)
xml_lines = ["    <key>EnvironmentVariables</key>", "    <dict>"]
for key, value in values.items():
    xml_lines.extend(
        [f"        <key>{escape(key)}</key>", f"        <string>{escape(value)}</string>"]
    )
xml_lines.append("    </dict>")
Path(xml_path).write_text("\n".join(xml_lines) + "\n", encoding="utf-8")
for path in (systemd_path, process_path, xml_path):
    os.chmod(path, 0o600)
PY

# This file is generated from parsed values with shlex quoting; it contains no
# caller-provided shell syntax.
# shellcheck disable=SC1090
source "$PROCESS_ENV_FILE"
rm -f "$PROCESS_ENV_FILE"

echo "Preflight: exomem doctor --profile $PROFILE..."
"$VENV_PYTHON" -m exomem doctor \
    --profile "$PROFILE" \
    --vault "$EXOMEM_VAULT_PATH"
echo "Preflight: exomem doctor --profile remote..."
"$VENV_PYTHON" -m exomem doctor \
    --profile remote \
    --vault "$EXOMEM_VAULT_PATH"

render_launchd_plist() {
    [[ -f "$PLIST_SRC" ]] || die "launchd template not found: $PLIST_SRC"
    mkdir -p "$(dirname "$PLIST_DEST")"
    "$VENV_PYTHON" - \
        "$PLIST_SRC" \
        "$PLIST_DEST" \
        "$LAUNCHD_ENV_FILE" \
        "$VENV_PYTHON" \
        "$WORKING_DIRECTORY" \
        "$BIND_HOST" \
        "$PORT" \
        "$LOG_DIR" <<'PY'
from pathlib import Path
from sys import argv
from xml.sax.saxutils import escape

src, dest, env_xml, python, working_dir, host, port, log_dir = argv[1:]
text = Path(src).read_text(encoding="utf-8")
replacements = {
    "__VENV_PYTHON__": python,
    "__WORKING_DIRECTORY__": working_dir,
    "__BIND_HOST__": host,
    "__PORT__": port,
    "__LOG_DIR__": log_dir,
}
for marker, value in replacements.items():
    text = text.replace(marker, escape(value))
text = text.replace("    __ENVIRONMENT_VARIABLES__\n", Path(env_xml).read_text(encoding="utf-8"))
Path(dest).write_text(text, encoding="utf-8")
PY
    chmod 600 "$PLIST_DEST"
}

render_systemd_unit() {
    [[ -f "$UNIT_SRC" ]] || die "systemd template not found: $UNIT_SRC"
    mkdir -p "$(dirname "$UNIT_DEST")"
    "$VENV_PYTHON" - \
        "$UNIT_SRC" \
        "$UNIT_DEST" \
        "$VENV_PYTHON" \
        "$WORKING_DIRECTORY" \
        "$SERVICE_ENV_FILE" \
        "$BIND_HOST" \
        "$PORT" <<'PY'
from pathlib import Path
from sys import argv

src, dest, python, working_dir, env_file, host, port = argv[1:]

def scalar_path(value: str) -> str:
    escaped = {" ": "\\x20", "\t": "\\x09", "\n": "\\x0a", "\r": "\\x0d", "\\": "\\x5c"}
    return "".join(escaped.get(char, char) for char in value)

def exec_path(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')

text = Path(src).read_text(encoding="utf-8")
replacements = {
    "__VENV_PYTHON__": exec_path(python),
    "__WORKING_DIRECTORY__": scalar_path(working_dir),
    "__SERVICE_ENV_FILE__": scalar_path(env_file),
    "__BIND_HOST__": host,
    "__PORT__": port,
}
for marker, value in replacements.items():
    text = text.replace(marker, value)
Path(dest).write_text(text, encoding="utf-8")
PY
}

stop_service() {
    case "$OS" in
        Darwin)
            launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
            ;;
        Linux)
            systemctl --user stop "$SERVICE_NAME" >/dev/null 2>&1 || true
            ;;
    esac
}

case "$OS" in
    Darwin)
        render_launchd_plist
        launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
        launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"
        launchctl kickstart -k "gui/$(id -u)/$LABEL"
        SERVICE_DEFINITION="$PLIST_DEST"
        STATUS_COMMAND="launchctl print gui/$(id -u)/$LABEL"
        LOG_COMMAND="tail -f '$LOG_DIR/service.out.log' '$LOG_DIR/service.err.log' '$LOG_DIR/exomem.log'"
        ;;
    Linux)
        render_systemd_unit
        systemctl --user daemon-reload
        systemctl --user enable --now "$SERVICE_NAME"
        if command -v loginctl >/dev/null 2>&1; then
            LINGER="$(loginctl show-user "$CURRENT_USER" -p Linger --value 2>/dev/null || true)"
            if [[ "$LINGER" != "yes" ]]; then
                if ! loginctl enable-linger "$CURRENT_USER" >/dev/null 2>&1; then
                    echo "warning: could not enable user linger; run: loginctl enable-linger '$CURRENT_USER'" >&2
                fi
            fi
        fi
        SERVICE_DEFINITION="$UNIT_DEST"
        STATUS_COMMAND="systemctl --user status $SERVICE_NAME"
        LOG_COMMAND="journalctl --user -u $SERVICE_NAME -f"
        ;;
esac

VERIFY_HOST="$BIND_HOST"
case "$VERIFY_HOST" in
    0.0.0.0|::|'[::]') VERIFY_HOST="127.0.0.1" ;;
esac
MCP_URL="http://${VERIFY_HOST}:${PORT}/mcp"
LAST_STATUS="000"
for _attempt in $(seq 1 60); do
    LAST_STATUS="$(curl --silent --show-error --output /dev/null \
        --write-out '%{http_code}' --max-time 2 "$MCP_URL" 2>/dev/null || true)"
    case "$LAST_STATUS" in
        401)
            break
            ;;
        200)
            stop_service
            die "$MCP_URL returned 200; OAuth is not enforced (service stopped)"
            ;;
        *)
            sleep 1
            ;;
    esac
done
if [[ "$LAST_STATUS" != "401" ]]; then
    stop_service
    die "$MCP_URL did not return the expected OAuth 401 (last status: ${LAST_STATUS:-000}; service stopped)"
fi

echo "Installed and verified '$SERVICE_NAME' on $OS at ${BIND_HOST}:${PORT}."
echo "  mode:       $MODE"
echo "  package:    $PACKAGE_REQUIREMENT"
echo "  python:     $VENV_PYTHON"
echo "  service:    $SERVICE_DEFINITION"
echo "  environment: $SERVICE_ENV_FILE"
echo "  endpoint:   $MCP_URL -> 401 (healthy, OAuth enforced)"
echo "  status:     $STATUS_COMMAND"
echo "  logs:       $LOG_COMMAND"
if [[ "$MODE" == "release" ]]; then
    echo "  update:     re-run this --release command after package or .env changes"
else
    echo "  update:     re-run this --repo-dev command after .env changes"
fi
