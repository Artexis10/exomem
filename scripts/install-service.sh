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
RESUME_STOPPED_TRANSITION=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

. "$SCRIPT_DIR/_service-common.sh"

usage() {
    cat <<'EOF'
Usage: bash scripts/install-service.sh [options]

Modes:
  --release                 Create/update a PyPI-backed service venv (product path)
  --repo-dev                Use the checkout .venv (default for compatibility)

Options:
  --profile lean|onnx|hybrid|standard|media
                            onnx is the CPU-only vector lane: same model as
                            hybrid, served by ONNX Runtime, no CUDA torch wheel
  --service-root PATH       Override release state/venv location
  --package-version VERSION Pin the PyPI release version
  --env-file PATH           Dotenv file (default: <repo>/.env)
  --bind-host HOST          Service bind host (default: 127.0.0.1)
  --port PORT               Service port (default: 8765)
  --legacy-mcp-compat       Set EXOMEM_MCP_LEGACY_COMPAT=1 in the service
  --resume-stopped-transition
                            Continue a prior failed transition proven stopped
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
        --resume-stopped-transition)
            RESUME_STOPPED_TRANSITION=1
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
    lean|onnx|hybrid|standard|media) ;;
    *) die "--profile must be lean, onnx, hybrid, standard, or media" ;;
esac

# `onnx` is an install lane, not a doctor profile. It expects exactly the vector
# lane `hybrid` expects — same model, same sidecar — and only the serving runtime
# differs, which doctor already resolves from EXOMEM_EMBED_BACKEND.
DOCTOR_PROFILE="$PROFILE"
EMBED_BACKEND=""
if [[ "$PROFILE" == "onnx" ]]; then
    DOCTOR_PROFILE="hybrid"
    EMBED_BACKEND="onnx"
fi
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
        EXPECTED_SERVICE_ID="$LABEL"
        require_command launchctl
        ;;
    Linux)
        CONFIG_ROOT="${XDG_CONFIG_HOME:-$HOME/.config}/exomem"
        DEFAULT_SERVICE_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/exomem/service"
        UNIT_SRC="$SCRIPT_DIR/exomem.service"
        UNIT_DEST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$SERVICE_NAME.service"
        EXPECTED_SERVICE_ID="$SERVICE_NAME"
        require_command systemctl
        ;;
    *)
        die "unsupported platform $OS; on Windows use scripts/install-service.ps1"
        ;;
esac

SERVICE_DEFINITION="${PLIST_DEST:-${UNIT_DEST:-}}"
EXISTING_SERVICE=0
[[ -f "$SERVICE_DEFINITION" ]] && EXISTING_SERVICE=1
if [[ "$EXISTING_SERVICE" == 1 ]]; then
    SERVICE_ID="$(exomem_service_id "$SERVICE_DEFINITION")" \
        || die "could not resolve the service-manager identity from $SERVICE_DEFINITION"
    [[ "$SERVICE_ID" == "$EXPECTED_SERVICE_ID" ]] \
        || die "rendered service identity '$SERVICE_ID' does not match supported installer identity '$EXPECTED_SERVICE_ID'"
else
    SERVICE_ID="$EXPECTED_SERVICE_ID"
fi
if [[ "$MODE" == "release" && -z "$SERVICE_ROOT" && "$EXISTING_SERVICE" == 1 ]]; then
    EXISTING_PYTHON="$(exomem_service_python "$SERVICE_DEFINITION" || true)"
    if [[ "$EXISTING_PYTHON" == */.venv/bin/python ]]; then
        SERVICE_ROOT="$(dirname "$(dirname "$(dirname "$EXISTING_PYTHON")")")"
    fi
fi
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
        onnx)
            # `torch` is pinned to the CUDA index for Linux and Windows, so on a
            # GPU-less host `hybrid` downloads a multi-GB wheel that can never be
            # used. This lane serves the identical model through ONNX Runtime —
            # the right default for a CPU-only host, which is most of them.
            EXTRAS="embeddings-onnx"
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

OLD_PORT="$(exomem_service_port "$SERVICE_DEFINITION")"
VAULT="$(exomem_dotenv_file_value "$ENV_FILE" EXOMEM_VAULT_PATH)"
[[ -n "$VAULT" ]] || die "EXOMEM_VAULT_PATH is required in $ENV_FILE"
DOTENV_STATE_ROOT="$(exomem_dotenv_file_value "$ENV_FILE" EXOMEM_STATE_ROOT)"
PREFERRED_STATE_ROOT="${EXOMEM_STATE_ROOT:-${DOTENV_STATE_ROOT:-$(exomem_platform_state_root)}}"
WORKER_BEFORE=0
WORKER_AFTER=0
RESUMING=0
if [[ "$EXISTING_SERVICE" == 1 ]]; then
    MANAGED_STATE_ROOT="$(exomem_select_service_state_root \
        "$SERVICE_DEFINITION" "$VENV_PYTHON" "$PREFERRED_STATE_ROOT")" \
        || die "could not select the existing service state-root binding"
    BINDING_PATH="$(exomem_service_binding_path "$SERVICE_DEFINITION" "$VENV_PYTHON")" \
        || die "could not resolve the selected service binding path"
    TRANSITION_RECEIPT="$(exomem_transition_receipt_path "$SERVICE_ID")" \
        || die "could not resolve an outside-vault transition receipt path"
    WORKER_BEFORE="$(exomem_service_worker_pid "$SERVICE_ID")"
    if [[ "$WORKER_BEFORE" =~ ^[1-9][0-9]*$ ]]; then
        LISTENER_PIDS_BEFORE="$(exomem_listener_pids "$OLD_PORT")" \
            || die "could not capture the full configured listener pid set"
    elif [[ "$RESUME_STOPPED_TRANSITION" == 1 ]]; then
        exomem_assert_stopped_resume_authority \
            "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
            "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" \
            || die "could not prove the stopped transition safe to resume"
        WORKER_BEFORE="$(exomem_transition_receipt_field \
            "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
            "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" worker_pid)"
        RESUMING=1
    else
        die "existing service must be running with a capturable worker for first entry; use --resume-stopped-transition only after a failed transition"
    fi
else
    MANAGED_STATE_ROOT="$PREFERRED_STATE_ROOT"
    [[ "$MANAGED_STATE_ROOT" == /* ]] \
        || die "managed EXOMEM_STATE_ROOT must be absolute"
    exomem_assert_listener_unbound "$PORT" \
        || die "fresh install cannot prove its listener is unbound"
fi

TRANSITION_BEGAN=0
TRANSITION_SUCCEEDED=0
PROCESS_ENV_FILE=""
cleanup() {
    local status=$?
    trap - EXIT
    [[ -z "$PROCESS_ENV_FILE" ]] || rm -f "$PROCESS_ENV_FILE"
    if [[ "$TRANSITION_BEGAN" == 1 && "$TRANSITION_SUCCEEDED" == 0 ]]; then
        local observed proof_ok=1
        observed="$(exomem_service_worker_pid "$SERVICE_ID")"
        if [[ "$observed" =~ ^[1-9][0-9]*$ && "$observed" != "$WORKER_BEFORE" ]]; then
            WORKER_AFTER="$observed"
        fi
        if [[ "$EXISTING_SERVICE" == 1 ]]; then
            exomem_publish_failed_transition_receipt \
                "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
                "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" "$WORKER_AFTER" \
                >/dev/null 2>&1 || proof_ok=0
        fi
        exomem_stop_service "$SERVICE_ID" >/dev/null 2>&1 || proof_ok=0
        if [[ "$EXISTING_SERVICE" == 1 ]]; then
            exomem_assert_stopped_resume_authority \
                "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
                "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" || proof_ok=0
        else
            if [[ "$WORKER_AFTER" =~ ^[1-9][0-9]*$ ]] && exomem_pid_alive "$WORKER_AFTER"; then
                echo "captured target worker pid $WORKER_AFTER survived the failed stop." >&2
                proof_ok=0
            fi
            exomem_assert_listener_unbound "$PORT" || proof_ok=0
        fi
        [[ "$PORT" == "$OLD_PORT" ]] || exomem_assert_listener_unbound "$PORT" || proof_ok=0
        if [[ "$proof_ok" == 1 ]]; then
            echo "error: state-root transition failed; service remains stopped." >&2
        else
            echo "error: state-root transition failed and the stopped state could not be proven; any retained receipt blocks resume." >&2
        fi
    fi
    exit "$status"
}
trap cleanup EXIT

# --- STATE-ROOT MAIN TRANSITION: stop/prove/install/migrate/doctor/start -------
if [[ "$EXISTING_SERVICE" == 1 && "$RESUMING" == 0 ]]; then
    exomem_create_transition_receipt \
        "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
        "$MANAGED_STATE_ROOT" "$VAULT" "$OLD_PORT" "$PORT" "$WORKER_BEFORE" \
        "$LISTENER_PIDS_BEFORE"
fi
TRANSITION_BEGAN=1
if [[ "$EXISTING_SERVICE" == 1 && "$RESUMING" == 0 ]]; then
    echo "Stopping $SERVICE_ID and proving worker $WORKER_BEFORE is gone..."
    exomem_stop_service "$SERVICE_ID"
    CAPTURED_PIDS="$(exomem_transition_receipt_field \
        "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
        "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" proof_pids)"
    exomem_assert_service_stopped \
        "$CAPTURED_PIDS" "$(printf '%s\n%s\n' "$OLD_PORT" "$PORT" | awk '!seen[$0]++')" "$SERVICE_ID"
elif [[ "$EXISTING_SERVICE" == 1 ]]; then
    exomem_assert_stopped_resume_authority \
        "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
        "$MANAGED_STATE_ROOT" "$VAULT" "$PORT"
fi
[[ "$PORT" == "$OLD_PORT" ]] || exomem_assert_listener_unbound "$PORT"
if [[ "$EXISTING_SERVICE" == 1 ]]; then
    exomem_update_transition_receipt \
        "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
        "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" stopped
fi

# Bind the one state root used by this operator process and the rendered
# service before replacing any package bytes. An existing managed binding is
# sticky; changing roots is a separate offline relocation, not an install
# side effect.
if [[ "$EXISTING_SERVICE" == 1 ]]; then
    MANAGED_STATE_ROOT="$(exomem_bind_service_state_root \
        "$SERVICE_DEFINITION" "$VENV_PYTHON" "$MANAGED_STATE_ROOT")"
    exomem_update_transition_receipt \
        "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
        "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" bound
fi
export EXOMEM_STATE_ROOT="$MANAGED_STATE_ROOT"

if [[ "$MODE" == "release" ]]; then
    echo "Installing $PACKAGE_REQUIREMENT into the release service venv..."
    uv pip install --upgrade --python "$VENV_PYTHON" "$PACKAGE_REQUIREMENT"
fi
if [[ "$EXISTING_SERVICE" == 1 ]]; then
    exomem_update_transition_receipt \
        "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
        "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" installed
fi

PROCESS_ENV_FILE="$(mktemp "$CONFIG_ROOT/installer-env.XXXXXX")"

# Parse dotenv with the package's own dependency, render systemd/launchd-safe
# forms, and create a shell-quoted temporary export file for doctor.
EXOMEM_MANAGED_STATE_ROOT_DEFAULT="${EXOMEM_STATE_ROOT:-$(exomem_platform_state_root)}" \
EXOMEM_PROFILE_EMBED_BACKEND="$EMBED_BACKEND" \
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
# Passed through the environment rather than as an eighth positional: the
# renderer invocations here are told apart by argv length, so a new positional
# would silently alias this call onto a different renderer.
embed_backend = os.environ.get("EXOMEM_PROFILE_EMBED_BACKEND", "")
state_root_default = os.environ.get("EXOMEM_MANAGED_STATE_ROOT_DEFAULT", "").strip()
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
if not state_root_default or not Path(state_root_default).is_absolute():
    raise SystemExit("managed EXOMEM_STATE_ROOT default must be absolute")
values["EXOMEM_STATE_ROOT"] = state_root_default
if not Path(values["EXOMEM_STATE_ROOT"]).is_absolute():
    raise SystemExit("EXOMEM_STATE_ROOT must be absolute")
values.setdefault("EXOMEM_LOG_DIR", log_dir)
# setdefault, not assignment: an explicit choice in the dotenv outranks the
# profile's default, so `--profile onnx` never silently overrides it.
if embed_backend:
    values.setdefault("EXOMEM_EMBED_BACKEND", embed_backend)
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
PROCESS_ENV_FILE=""

echo "Offline state migration..."
"$VENV_PYTHON" -m exomem maintain --vault "$EXOMEM_VAULT_PATH" \
    --migrate-state --offline --json
if [[ "$EXISTING_SERVICE" == 1 ]]; then
    exomem_update_transition_receipt \
        "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
        "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" migrated
fi

echo "Preflight: exomem doctor --profile $DOCTOR_PROFILE..."
"$VENV_PYTHON" -m exomem doctor \
    --profile "$DOCTOR_PROFILE" \
    --vault "$EXOMEM_VAULT_PATH"
echo "Preflight: exomem doctor --profile remote..."
"$VENV_PYTHON" -m exomem doctor \
    --profile remote \
    --vault "$EXOMEM_VAULT_PATH"
if [[ "$EXISTING_SERVICE" == 1 ]]; then
    exomem_update_transition_receipt \
        "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
        "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" doctor-passed
fi

INSTALLED_VERSION="$(exomem_installed_version "$VENV_PYTHON" || true)"
[[ -n "$INSTALLED_VERSION" ]] \
    || die "target interpreter has no importable Exomem version; service remains stopped"

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

case "$OS" in
    Darwin)
        render_launchd_plist
        SERVICE_DEFINITION="$PLIST_DEST"
        RENDERED_SERVICE_ID="$(exomem_service_id "$SERVICE_DEFINITION")" \
            || die "could not resolve identity from rendered service $SERVICE_DEFINITION"
        [[ "$RENDERED_SERVICE_ID" == "$SERVICE_ID" ]] \
            || die "rendered service identity changed from '$SERVICE_ID' to '$RENDERED_SERVICE_ID'"
        STATUS_COMMAND="launchctl print gui/$(id -u)/$SERVICE_ID"
        LOG_COMMAND="tail -f '$LOG_DIR/service.out.log' '$LOG_DIR/service.err.log' '$LOG_DIR/exomem.log'"
        ;;
    Linux)
        render_systemd_unit
        SERVICE_DEFINITION="$UNIT_DEST"
        RENDERED_SERVICE_ID="$(exomem_service_id "$SERVICE_DEFINITION")" \
            || die "could not resolve identity from rendered service $SERVICE_DEFINITION"
        [[ "$RENDERED_SERVICE_ID" == "$SERVICE_ID" ]] \
            || die "rendered service identity changed from '$SERVICE_ID' to '$RENDERED_SERVICE_ID'"
        systemctl --user daemon-reload
        systemctl --user enable "$SERVICE_ID"
        if command -v loginctl >/dev/null 2>&1; then
            LINGER="$(loginctl show-user "$CURRENT_USER" -p Linger --value 2>/dev/null || true)"
            if [[ "$LINGER" != "yes" ]]; then
                if ! loginctl enable-linger "$CURRENT_USER" >/dev/null 2>&1; then
                    echo "warning: could not enable user linger; run: loginctl enable-linger '$CURRENT_USER'" >&2
                fi
            fi
        fi
        STATUS_COMMAND="systemctl --user status $SERVICE_ID"
        LOG_COMMAND="journalctl --user -u $SERVICE_ID -f"
        ;;
esac

if [[ "$EXISTING_SERVICE" == 1 ]]; then
    exomem_update_transition_receipt \
        "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
        "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" starting
fi
exomem_start_service "$SERVICE_DEFINITION" "$SERVICE_ID"
WORKER_AFTER="$(exomem_wait_worker_pid 60 "$SERVICE_ID")" \
    || die "service started without an observable worker pid"
if [[ "$EXISTING_SERVICE" == 1 ]]; then
    exomem_update_transition_receipt \
        "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
        "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" starting "$WORKER_AFTER"
fi
exomem_assert_service_restarted "$WORKER_BEFORE" "$WORKER_AFTER"
exomem_assert_listener_owned_by_worker "$PORT" "$WORKER_AFTER"

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
            die "$MCP_URL returned 200; OAuth is not enforced (service stopped)"
            ;;
        *)
            sleep 1
            ;;
    esac
done
if [[ "$LAST_STATUS" != "401" ]]; then
    die "$MCP_URL did not return the expected OAuth 401 (last status: ${LAST_STATUS:-000}; service stopped)"
fi

HEALTH_URL="http://${VERIFY_HOST}:${PORT}/health"
SERVED_VERSION=""
for _attempt in $(seq 1 45); do
    HEALTH_BODY="$(curl -fsS --max-time 5 "$HEALTH_URL" 2>/dev/null || true)"
    if [[ -n "$HEALTH_BODY" ]]; then
        SERVED_VERSION="$(printf '%s' "$HEALTH_BODY" \
            | sed -n 's|.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*|\1|p')"
        [[ -n "$SERVED_VERSION" ]] && break
    fi
    sleep 2
done
[[ -n "$SERVED_VERSION" ]] \
    || die "$HEALTH_URL never reported a live Exomem version; service stopped"
[[ "$SERVED_VERSION" == "$INSTALLED_VERSION" ]] \
    || die "live service version '$SERVED_VERSION' differs from target interpreter '$INSTALLED_VERSION'; service stopped"

if [[ "$EXISTING_SERVICE" == 1 ]]; then
    exomem_update_transition_receipt \
        "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
        "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" started
    exomem_update_transition_receipt \
        "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
        "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" accepted
    exomem_clear_transition_receipt \
        "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
        "$MANAGED_STATE_ROOT" "$VAULT" "$PORT"
fi

TRANSITION_SUCCEEDED=1

echo "Installed and verified '$SERVICE_ID' on $OS at ${BIND_HOST}:${PORT}."
echo "  mode:       $MODE"
echo "  package:    $PACKAGE_REQUIREMENT"
echo "  python:     $VENV_PYTHON"
echo "  service:    $SERVICE_DEFINITION"
echo "  environment: $SERVICE_ENV_FILE"
echo "  endpoint:   $MCP_URL -> 401 (healthy, OAuth enforced)"
echo "  version:    $SERVED_VERSION (from $HEALTH_URL)"
echo "  status:     $STATUS_COMMAND"
echo "  logs:       $LOG_COMMAND"
if [[ "$MODE" == "release" ]]; then
    echo "  update:     re-run this --release command after package or .env changes"
else
    echo "  update:     re-run this --repo-dev command after .env changes"
fi
