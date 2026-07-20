#!/usr/bin/env bash
# Upgrade the installed exomem service to the current release, in one command.
# macOS -> launchd agent (com.exomem);  Linux -> systemd --user service (exomem).
# Cross-platform counterpart to scripts/upgrade.ps1 (Windows).
#
# This exists because the service runs a PyPI-backed venv that is NOT the repo
# checkout, so `git pull` does nothing to it and nothing compared the two. The
# Windows box was found five releases behind for exactly that reason.
#
# Usage:
#   bash scripts/upgrade.sh
#   bash scripts/upgrade.sh --profile media
#   bash scripts/upgrade.sh --package-version 0.25.4   # pin instead of latest
#   bash scripts/upgrade.sh --skip-restart             # stage it, restart later

set -euo pipefail

LABEL="com.exomem"
SERVICE_NAME="exomem"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

. "$SCRIPT_DIR/_service-common.sh"

PROFILE="standard"
PACKAGE_VERSION=""
VAULT=""
SKIP_RESTART=0

die() { echo "upgrade: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile)         PROFILE="${2:?}"; shift 2 ;;
        --package-version) PACKAGE_VERSION="${2:?}"; shift 2 ;;
        --vault)           VAULT="${2:?}"; shift 2 ;;
        --skip-restart)    SKIP_RESTART=1; shift ;;
        -h|--help)         sed -n '2,14p' "$0"; exit 0 ;;
        *)                 die "unknown option: $1" ;;
    esac
done

case "$PROFILE" in
    lean|hybrid|standard|media) ;;
    *) die "profile must be lean, hybrid, standard, or media (got: $PROFILE)" ;;
esac

OS="$(uname -s)"
case "$OS" in
    Darwin) UNIT_FILE="$HOME/Library/LaunchAgents/$LABEL.plist" ;;
    Linux)  UNIT_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$SERVICE_NAME.service" ;;
    *)      die "unsupported platform $OS; on Windows use scripts/upgrade.ps1" ;;
esac

[[ -f "$UNIT_FILE" ]] || die "no installed service found at $UNIT_FILE. Install one first: bash scripts/install-service.sh --release"

# --- Locate the venv the service ACTUALLY runs ----------------------------------
# The rendered unit is the source of truth here, the same role the NSSM registry
# plays on Windows: the service root is wherever --service-root said at install
# time, which is not derivable from the repo layout.
VENV_PYTHON="$(exomem_service_python || true)"
[[ -n "$VENV_PYTHON" && -x "$VENV_PYTHON" ]] || die "could not resolve the service interpreter from $UNIT_FILE (got: '${VENV_PYTHON:-}')"
PORT="$(exomem_service_port)"

BEFORE="$(exomem_installed_version "$VENV_PYTHON" || echo "")"
echo "Service '$SERVICE_NAME'"
echo "  venv:      $VENV_PYTHON"
echo "  installed: ${BEFORE:-unknown}"
echo "  repo:      $(exomem_repo_version "$REPO_ROOT")"

# --- Upgrade ---------------------------------------------------------------------
# Extras mirror install-service.sh, including the Apple-Silicon Metal path.
ARCH="$(uname -m)"
case "$PROFILE" in
    lean)     EXTRAS="" ;;
    hybrid)   EXTRAS="embeddings" ;;
    standard) EXTRAS="embeddings,media" ;;
    media)    EXTRAS="embeddings,media,vision,diarization" ;;
esac
if [[ "$OS" == "Darwin" && "$ARCH" == "arm64" && -n "$EXTRAS" && "$PROFILE" != "hybrid" ]]; then
    EXTRAS="$EXTRAS,media-mlx"
fi
REQUIREMENT="exomem"
[[ -n "$EXTRAS" ]] && REQUIREMENT="exomem[$EXTRAS]"
[[ -n "$PACKAGE_VERSION" ]] && REQUIREMENT="$REQUIREMENT==$PACKAGE_VERSION"

command -v uv >/dev/null 2>&1 || die "uv not found on PATH"
echo "Installing $REQUIREMENT into the service venv..."
uv pip install --upgrade --python "$VENV_PYTHON" "$REQUIREMENT"

# No CUDA repair here, unlike the Windows path: PyPI's Linux torch wheels are
# already CUDA-enabled, and macOS uses Metal/MPS. The Windows-only repair exists
# because `uv pip` ignores [tool.uv.sources] and pulls a CPU-only wheel there.

AFTER="$(exomem_installed_version "$VENV_PYTHON" || echo "")"
echo "Installed version: ${BEFORE:-unknown} -> ${AFTER:-unknown}"

# --- Preflight against the venv the service actually runs -------------------------
DOCTOR_ARGS=(-m exomem doctor --profile "$PROFILE")
if [[ -z "$VAULT" ]]; then
    VAULT="$(exomem_dotenv_value "$REPO_ROOT" EXOMEM_VAULT_PATH)"
fi
[[ -z "$VAULT" ]] && VAULT="${EXOMEM_VAULT_PATH:-}"
[[ -n "$VAULT" ]] && DOCTOR_ARGS+=(--vault "$VAULT")

echo "Preflight: exomem doctor --profile $PROFILE..."
if ! "$VENV_PYTHON" "${DOCTOR_ARGS[@]}"; then
    die "doctor preflight failed for profile '$PROFILE'. The upgrade is staged in the venv but the service was NOT restarted; fix the findings and re-run."
fi

if [[ "$SKIP_RESTART" -eq 1 ]]; then
    echo "--skip-restart given: the new version is installed but the service is still running the old one."
    exit 0
fi

# --- Restart ----------------------------------------------------------------------
echo "Restarting $SERVICE_NAME..."
if [[ "$OS" == "Darwin" ]]; then
    launchctl kickstart -k "gui/$(id -u)/$LABEL"
else
    systemctl --user restart "$SERVICE_NAME"
fi

# --- Verify what is actually serving ------------------------------------------------
# The point of the whole script: assert the LIVE process reports the version we
# just installed. A restart that silently came back on the old code is the failure
# mode this exists to catch.
HEALTH="http://127.0.0.1:$PORT/health"
SERVED=""
for _ in $(seq 1 45); do
    BODY="$(curl -fsS --max-time 5 "$HEALTH" 2>/dev/null || true)"
    if [[ -n "$BODY" ]]; then
        SERVED="$(printf '%s' "$BODY" | sed -n 's|.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*|\1|p')"
        [[ -n "$SERVED" ]] && break
    fi
    sleep 2      # startup loads embedding/media models before binding
done

[[ -n "$SERVED" ]] || die "service restarted but $HEALTH never answered. Check the service logs."

echo "Serving version: $SERVED (from $HEALTH)"
if [[ -n "$AFTER" && "$SERVED" != "$AFTER" ]]; then
    die "version mismatch: installed '$AFTER' but the live service reports '$SERVED'. Something else is bound to $PORT, or the restart did not take."
fi

READY="$(curl -fsS --max-time 10 "http://127.0.0.1:$PORT/health/ready" 2>/dev/null || true)"
[[ -n "$READY" ]] && echo "Readiness: $READY"
