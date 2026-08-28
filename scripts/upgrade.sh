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
#   bash scripts/upgrade.sh --resume-stopped-transition # roll forward after failure
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
RESUME_STOPPED_TRANSITION=0
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
        --resume-stopped-transition) RESUME_STOPPED_TRANSITION=1; shift ;;
        --skip-restart)    die "--skip-restart is unavailable during state-root migration; a target install must complete offline migration and restart or remain stopped on failure" ;;
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

SERVICE_ID="$(exomem_service_id "$UNIT_FILE")" \
    || die "could not resolve the service-manager identity from $UNIT_FILE"

# --- Locate the venv the service ACTUALLY runs ----------------------------------
# The rendered unit is the source of truth here, the same role the NSSM registry
# plays on Windows: the service root is wherever --service-root said at install
# time, which is not derivable from the repo layout.
VENV_PYTHON="$(exomem_service_python "$UNIT_FILE" || true)"
[[ -n "$VENV_PYTHON" && -x "$VENV_PYTHON" ]] || die "could not resolve the service interpreter from $UNIT_FILE (got: '${VENV_PYTHON:-}')"
PORT="$(exomem_service_port "$UNIT_FILE")"

BEFORE="$(exomem_installed_version "$VENV_PYTHON" || echo "")"
echo "Service '$SERVICE_ID'"
echo "  venv:      $VENV_PYTHON"
echo "  installed: ${BEFORE:-unknown}"
echo "  repo:      $(exomem_repo_version "$REPO_ROOT")"

# Resolve every authority before entering the stop window. No state mutation or
# package replacement has happened yet.
if [[ -z "$VAULT" ]]; then
    VAULT="$(exomem_dotenv_value "$REPO_ROOT" EXOMEM_VAULT_PATH)"
fi
[[ -z "$VAULT" ]] && VAULT="${EXOMEM_VAULT_PATH:-}"
[[ -n "$VAULT" ]] || die "no vault resolved; pass --vault or set EXOMEM_VAULT_PATH"

DOTENV_STATE_ROOT="$(exomem_dotenv_value "$REPO_ROOT" EXOMEM_STATE_ROOT)"
PREFERRED_STATE_ROOT="${EXOMEM_STATE_ROOT:-${DOTENV_STATE_ROOT:-$(exomem_platform_state_root)}}"
MANAGED_STATE_ROOT="$(exomem_select_service_state_root \
    "$UNIT_FILE" "$VENV_PYTHON" "$PREFERRED_STATE_ROOT")" \
    || die "could not select the existing service state-root binding"
BINDING_PATH="$(exomem_service_binding_path "$UNIT_FILE" "$VENV_PYTHON")" \
    || die "could not resolve the selected service binding path"
TRANSITION_RECEIPT="$(exomem_transition_receipt_path "$SERVICE_ID")" \
    || die "could not resolve an outside-vault transition receipt path"

WORKER_BEFORE="$(exomem_service_worker_pid "$SERVICE_ID")"
WORKER_AFTER=0
RESUMING=0
if [[ "$WORKER_BEFORE" =~ ^[1-9][0-9]*$ ]]; then
    LISTENER_PIDS_BEFORE="$(exomem_listener_pids "$PORT")" \
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
    die "service must be running with a capturable worker for first entry; use --resume-stopped-transition only after a failed transition left it proven stopped"
fi

TRANSITION_BEGAN=0
TRANSITION_SUCCEEDED=0
cleanup_transition() {
    local status=$?
    trap - EXIT
    if [[ "$TRANSITION_BEGAN" == 1 && "$TRANSITION_SUCCEEDED" == 0 ]]; then
        local observed proof_ok=1
        observed="$(exomem_service_worker_pid "$SERVICE_ID")"
        if [[ "$observed" =~ ^[1-9][0-9]*$ && "$observed" != "$WORKER_BEFORE" ]]; then
            WORKER_AFTER="$observed"
        fi
        exomem_publish_failed_transition_receipt \
            "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
            "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" "$WORKER_AFTER" \
            >/dev/null 2>&1 || proof_ok=0
        exomem_stop_service "$SERVICE_ID" >/dev/null 2>&1 || proof_ok=0
        exomem_assert_stopped_resume_authority \
            "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
            "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" || proof_ok=0
        if [[ "$proof_ok" == 1 ]]; then
            echo "upgrade: state-root transition failed; service remains stopped." >&2
        else
            echo "upgrade: state-root transition failed and the stopped state could not be proven; the retained receipt blocks resume." >&2
        fi
    fi
    exit "$status"
}
trap cleanup_transition EXIT

# --- STATE-ROOT MAIN TRANSITION: stop/prove/install/migrate/doctor/start -------
if [[ "$RESUMING" == 0 ]]; then
    exomem_create_transition_receipt \
        "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
        "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" "$PORT" "$WORKER_BEFORE" \
        "$LISTENER_PIDS_BEFORE"
fi
TRANSITION_BEGAN=1
if [[ "$RESUMING" == 1 ]]; then
    exomem_assert_stopped_resume_authority \
        "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
        "$MANAGED_STATE_ROOT" "$VAULT" "$PORT"
else
    echo "Stopping $SERVICE_ID and proving worker $WORKER_BEFORE is gone..."
    exomem_stop_service "$SERVICE_ID"
    CAPTURED_PIDS="$(exomem_transition_receipt_field \
        "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
        "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" proof_pids)"
    exomem_assert_service_stopped "$CAPTURED_PIDS" "$PORT" "$SERVICE_ID"
fi
exomem_update_transition_receipt \
    "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
    "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" stopped
MANAGED_STATE_ROOT="$(exomem_bind_service_state_root \
    "$UNIT_FILE" "$VENV_PYTHON" "$MANAGED_STATE_ROOT")"
export EXOMEM_STATE_ROOT="$MANAGED_STATE_ROOT"
exomem_update_transition_receipt \
    "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
    "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" bound

# --- Upgrade while the service remains stopped ---------------------------------
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
    || die "the target package was not installed; the service remains stopped."
exomem_update_transition_receipt \
    "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
    "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" installed

# --- Explicit offline migration and target-interpreter doctor -------------------
echo "Offline state migration..."
"$VENV_PYTHON" -m exomem maintain --vault "$VAULT" \
    --migrate-state --offline --json
exomem_update_transition_receipt \
    "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
    "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" migrated

echo "Preflight: exomem doctor --profile $PROFILE..."
if ! "$VENV_PYTHON" -m exomem doctor --profile "$PROFILE" --vault "$VAULT"; then
    die "doctor preflight failed for profile '$PROFILE'; the target is installed and the service remains stopped"
fi
exomem_update_transition_receipt \
    "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
    "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" doctor-passed

# --- Start target and prove a new live worker ------------------------------------
exomem_update_transition_receipt \
    "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
    "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" starting
echo "Starting $SERVICE_ID..."
exomem_start_service "$UNIT_FILE" "$SERVICE_ID"
WORKER_AFTER="$(exomem_wait_worker_pid 60 "$SERVICE_ID")" \
    || die "service started without an observable worker pid"
exomem_update_transition_receipt \
    "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
    "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" starting "$WORKER_AFTER"
exomem_assert_service_restarted "$WORKER_BEFORE" "$WORKER_AFTER"
exomem_assert_listener_owned_by_worker "$PORT" "$WORKER_AFTER"

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
exomem_update_transition_receipt \
    "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
    "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" started

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
exomem_update_transition_receipt \
    "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
    "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" accepted
exomem_clear_transition_receipt \
    "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
    "$MANAGED_STATE_ROOT" "$VAULT" "$PORT"
TRANSITION_SUCCEEDED=1
