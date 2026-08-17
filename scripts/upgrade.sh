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
#   bash scripts/upgrade.sh --cli-sync always          # install CLI if absent
#   bash scripts/upgrade.sh --skip-restart             # stage it, restart later
#   bash scripts/upgrade.sh --unit-file ~/Library/LaunchAgents/com.exomem.http.plist

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
CLI_SYNC="auto"
UNIT_FILE=""

die() { echo "upgrade: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile)         PROFILE="${2:?}"; shift 2 ;;
        --package-version) PACKAGE_VERSION="${2:?}"; shift 2 ;;
        --vault)           VAULT="${2:?}"; shift 2 ;;
        --cli-sync)        CLI_SYNC="${2:?}"; shift 2 ;;
        --unit-file)       UNIT_FILE="${2:?}"; shift 2 ;;
        --skip-restart)    SKIP_RESTART=1; shift ;;
        -h|--help)         sed -n '2,15p' "$0"; exit 0 ;;
        *)                 die "unknown option: $1" ;;
    esac
done

case "$PROFILE" in
    lean|hybrid|standard|media) ;;
    *) die "profile must be lean, hybrid, standard, or media (got: $PROFILE)" ;;
esac
case "$CLI_SYNC" in
    auto|always|never) ;;
    *) die "cli sync must be auto, always, or never (got: $CLI_SYNC)" ;;
esac

OS="$(uname -s)"
case "$OS" in
    Darwin)
        UNIT_DIR="$HOME/Library/LaunchAgents"
        UNIT_GLOB="$LABEL*.plist"
        DEFAULT_UNIT="$UNIT_DIR/$LABEL.plist"
        ;;
    Linux)
        UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
        UNIT_GLOB="$SERVICE_NAME*.service"
        DEFAULT_UNIT="$UNIT_DIR/$SERVICE_NAME.service"
        ;;
    *)  die "unsupported platform $OS; on Windows use scripts/upgrade.ps1" ;;
esac

if [[ -n "$UNIT_FILE" ]]; then
    [[ -f "$UNIT_FILE" ]] || die "--unit-file not found: $UNIT_FILE"
elif [[ -f "$DEFAULT_UNIT" ]]; then
    UNIT_FILE="$DEFAULT_UNIT"
else
    # A hand-rolled install may register a suffixed label (com.exomem.http), so
    # say what was searched and what turned up rather than implying nothing is
    # installed. Suffixed units are reported, never auto-selected: the upgrade
    # reads the service venv out of the rendered unit, so guessing wrong fails
    # later and far less legibly than refusing here.
    shopt -s nullglob
    CANDIDATES=("$UNIT_DIR"/$UNIT_GLOB)
    shopt -u nullglob
    MSG="no installed service found at $DEFAULT_UNIT (searched $UNIT_DIR for $UNIT_GLOB)."
    if (( ${#CANDIDATES[@]} )); then
        MSG="$MSG Found ${#CANDIDATES[@]} similarly-named unit(s): ${CANDIDATES[*]}."
        MSG="$MSG That is a label mismatch, not a missing install."
        MSG="$MSG Re-run with --unit-file <path> if the unit launches the exomem entry point;"
        MSG="$MSG a unit that invokes a custom wrapper cannot be upgraded by this script."
    else
        MSG="$MSG Install one first: bash scripts/install-service.sh --release"
    fi
    die "$MSG"
fi

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

# Resolve and SHOW the target before installing, so "installed:/repo:/target:"
# makes a no-op visible at a glance instead of only in hindsight.
TARGET="$(exomem_resolve_target_version "$VENV_PYTHON" "$REQUIREMENT" "$BEFORE" || echo "")"
# A pin stays assertable even when the resolve could not run at all.
[[ -z "$TARGET" && -n "$PACKAGE_VERSION" ]] && TARGET="$PACKAGE_VERSION"
echo "  target:    ${TARGET:-unresolved}"

echo "Installing $REQUIREMENT into the service venv..."
uv pip install --upgrade --refresh-package exomem --python "$VENV_PYTHON" "$REQUIREMENT"

# No CUDA repair here, unlike the Windows path: PyPI's Linux torch wheels are
# already CUDA-enabled, and macOS uses Metal/MPS. The Windows-only repair exists
# because `uv pip` ignores [tool.uv.sources] and pulls a CPU-only wheel there.

AFTER="$(exomem_installed_version "$VENV_PYTHON" || echo "")"
echo "Installed version: ${BEFORE:-unknown} -> ${AFTER:-unknown}"
# That line is the receipt, not the check. Reading it as the check is what let a
# silent no-op deploy through on the Windows path (#578).
exomem_assert_install_applied "$REQUIREMENT" "$BEFORE" "$AFTER" "$TARGET" \
    || die "the service was NOT restarted."

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
    echo "--skip-restart given: the new version is staged, but the running service and user-facing CLI are unchanged. CLI sync is deferred until the live release is verified."
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

# The verified live process is the release authority.  Keep the daily CLI lean:
# align its Exomem release, not the service's optional ML/media dependency set.
CLI_REQUIRED=0
if [[ "$CLI_SYNC" == "always" ]] || { [[ "$CLI_SYNC" == "auto" ]] && exomem_uv_tool_has_exomem; }; then
    CLI_REQUIRED=1
fi
exomem_write_managed_manifest "$VENV_PYTHON" "$SERVED" "$PROFILE" "http://127.0.0.1:$PORT"
exomem_sync_uv_cli "$CLI_SYNC" "$SERVED"
if [[ "$CLI_SYNC" != "never" ]]; then
    exomem_verify_visible_clis "$SERVED" "$VENV_PYTHON" "$CLI_REQUIRED"
fi

READY="$(curl -fsS --max-time 10 "http://127.0.0.1:$PORT/health/ready" 2>/dev/null || true)"
[[ -n "$READY" ]] && echo "Readiness: $READY"
