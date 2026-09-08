#!/usr/bin/env bash
# Execute one accepted native-owner migration or activation with a proven stop window.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/_service-common.sh"

UNIT_FILE=""
REQUEST_ID=""
RESUME=0

die() { echo "owner-setup: $*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
Run an accepted native-owner maintenance review.

Usage: python -m exomem.native_owner_maintenance_runner \
       --unit-file PATH --request-id ID [--resume]

Copy the exact command from the owner review page. It briefly stops the
selected service, applies the accepted migration or activation, and restarts it.
Use --resume only for that review's retained stopped-transition receipt.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --unit-file) UNIT_FILE="${2:?}"; shift 2 ;;
        --request-id) REQUEST_ID="${2:?}"; shift 2 ;;
        --resume) RESUME=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

[[ -n "$REQUEST_ID" ]] || die "--request-id is required"
case "$(uname -s)" in
    Darwin|Linux) ;;
    *) die "Windows owner setup is not supported by this command" ;;
esac
if [[ -z "$UNIT_FILE" ]]; then
    UNIT_FILE="$(exomem_unit_file)" || die "could not resolve the managed service unit"
fi
[[ -f "$UNIT_FILE" ]] || die "--unit-file not found: $UNIT_FILE"

SERVICE_ID="$(exomem_service_id "$UNIT_FILE")" \
    || die "could not resolve the exact service identity"
VENV_PYTHON="$(exomem_service_python "$UNIT_FILE" || true)"
[[ -n "$VENV_PYTHON" && -x "$VENV_PYTHON" ]] \
    || die "could not resolve the service interpreter"
PORT="$(exomem_service_port "$UNIT_FILE")"
RUNTIME_VERSION="$(exomem_installed_version "$VENV_PYTHON")" \
    || die "the selected service interpreter cannot import Exomem"

METADATA="$("$VENV_PYTHON" -m exomem.native_owner_maintenance metadata \
    --unit-file "$UNIT_FILE" --request-id "$REQUEST_ID")" \
    || die "managed service metadata is invalid"
metadata_field() {
    "$VENV_PYTHON" -c 'import json,sys; value=json.loads(sys.argv[1])[sys.argv[2]]; print(value)' \
        "$METADATA" "$1"
}
[[ "$(metadata_field service_id)" == "$SERVICE_ID" ]] \
    || die "service identity differs from the rendered unit"
[[ "$(metadata_field port)" == "$PORT" ]] \
    || die "service port differs from the rendered unit"
VAULT="$(metadata_field vault_root)"
MANAGED_STATE_ROOT="$(metadata_field state_root)"
BINDING_PATH="$(metadata_field binding_path)"

PREFLIGHT_ARGS=(preflight --unit-file "$UNIT_FILE" --request-id "$REQUEST_ID")
[[ "$RESUME" == 1 ]] && PREFLIGHT_ARGS+=(--resume)
"$VENV_PYTHON" -m exomem.native_owner_maintenance "${PREFLIGHT_ARGS[@]}" >/dev/null \
    || die "accepted owner maintenance review is absent, expired, or changed"

REQUEST_TOKEN="$("$VENV_PYTHON" -c \
    'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())' \
    "$REQUEST_ID")"
RECEIPT_KEY="$SERVICE_ID.owner-$REQUEST_TOKEN"
TRANSITION_RECEIPT="$(exomem_transition_receipt_path "$RECEIPT_KEY")" \
    || die "could not resolve an outside-vault transition receipt path"

WORKER_BEFORE="$(exomem_service_worker_pid "$SERVICE_ID")"
WORKER_AFTER=0
if [[ "$RESUME" == 1 ]]; then
    exomem_assert_stopped_resume_authority \
        "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
        "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" \
        || die "exact stopped maintenance receipt cannot be resumed"
    WORKER_BEFORE="$(exomem_transition_receipt_field \
        "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
        "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" worker_pid)"
else
    [[ "$WORKER_BEFORE" =~ ^[1-9][0-9]*$ ]] \
        || die "service must be running with a capturable worker before first entry"
    LISTENER_PIDS_BEFORE="$(exomem_listener_pids "$PORT")" \
        || die "could not capture the configured listener pid set"
fi

TRANSITION_BEGAN=0
TRANSITION_SUCCEEDED=0
cleanup_transition() {
    local status=$? proof_ok=1
    trap - EXIT
    if [[ "$TRANSITION_BEGAN" == 1 && "$TRANSITION_SUCCEEDED" == 0 ]]; then
        exomem_publish_failed_transition_receipt \
            "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
            "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" "$WORKER_AFTER" \
            >/dev/null 2>&1 || proof_ok=0
        exomem_stop_service "$SERVICE_ID" >/dev/null 2>&1 || proof_ok=0
        if [[ "$proof_ok" == 1 ]]; then
            echo "owner-setup: maintenance failed; service remains stopped." >&2
        else
            echo "owner-setup: maintenance failed; stopped state is uncertain." >&2
        fi
        echo "owner-setup: receipt: $TRANSITION_RECEIPT" >&2
        echo "owner-setup: review:  $REQUEST_ID" >&2
    fi
    exit "$status"
}
trap cleanup_transition EXIT

# Ordered maintenance transition: consent, receipt, stop proof, apply, restart proof.
if [[ "$RESUME" == 0 ]]; then
    exomem_create_transition_receipt \
        "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
        "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" "$PORT" "$WORKER_BEFORE" \
        "$LISTENER_PIDS_BEFORE"
fi
TRANSITION_BEGAN=1
if [[ "$RESUME" == 1 ]]; then
    exomem_assert_stopped_resume_authority \
        "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
        "$MANAGED_STATE_ROOT" "$VAULT" "$PORT"
else
    exomem_stop_service "$SERVICE_ID"
    CAPTURED_PIDS="$(exomem_transition_receipt_field \
        "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
        "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" proof_pids)"
    exomem_assert_service_stopped "$CAPTURED_PIDS" "$PORT" "$SERVICE_ID"
fi
exomem_update_transition_receipt \
    "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
    "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" stopped

APPLY_ARGS=(apply --unit-file "$UNIT_FILE" --request-id "$REQUEST_ID" \
    --receipt "$TRANSITION_RECEIPT")
[[ "$RESUME" == 1 ]] && APPLY_ARGS+=(--resume)
"$VENV_PYTHON" -m exomem.native_owner_maintenance "${APPLY_ARGS[@]}" >/dev/null
exomem_update_transition_receipt \
    "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
    "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" migrated

exomem_update_transition_receipt \
    "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
    "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" starting
exomem_start_service "$UNIT_FILE" "$SERVICE_ID"
WORKER_AFTER="$(exomem_wait_worker_pid 60 "$SERVICE_ID")" \
    || die "service started without an observable worker"
exomem_update_transition_receipt \
    "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
    "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" starting "$WORKER_AFTER"
exomem_assert_service_restarted "$WORKER_BEFORE" "$WORKER_AFTER"
exomem_assert_listener_owned_by_worker "$PORT" "$WORKER_AFTER"

HEALTH="http://127.0.0.1:$PORT/health"
SERVED=""
for _ in $(seq 1 45); do
    BODY="$(curl -fsS --max-time 5 "$HEALTH" 2>/dev/null || true)"
    SERVED="$(printf '%s' "$BODY" \
        | sed -n 's|.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*|\1|p')"
    [[ -n "$SERVED" ]] && break
    sleep 2
done
[[ "$SERVED" == "$RUNTIME_VERSION" ]] \
    || die "restarted service version does not match the accepted runtime"
exomem_update_transition_receipt \
    "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
    "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" started
exomem_update_transition_receipt \
    "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
    "$MANAGED_STATE_ROOT" "$VAULT" "$PORT" accepted
exomem_clear_transition_receipt \
    "$VENV_PYTHON" "$TRANSITION_RECEIPT" "$SERVICE_ID" "$BINDING_PATH" \
    "$MANAGED_STATE_ROOT" "$VAULT" "$PORT"
TRANSITION_SUCCEEDED=1
echo "owner-setup: completed review $REQUEST_ID for service $SERVICE_ID"
