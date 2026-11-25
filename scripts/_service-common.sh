#!/usr/bin/env bash
# Shared service-location helpers for macOS/Linux. Source from the other scripts:
#   . "$SCRIPT_DIR/_service-common.sh"
#
# Counterpart to _service-common.ps1 (Windows). Same reason for existing: the
# interpreter the service runs is NOT derivable from the repo layout. A release
# install points the unit at a PyPI-backed venv under whatever --service-root said
# at install time, so scripts that assume "$REPO_ROOT/.venv" gate the wrong
# environment entirely.
#
# The rendered launchd plist / systemd unit is the source of truth, so ask it.

exomem_label() { echo "com.exomem"; }
exomem_service_name() { echo "exomem"; }

EXOMEM_SERVICE_COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXOMEM_TRANSITION_RECEIPT_TOOL="$EXOMEM_SERVICE_COMMON_DIR/service-transition-receipt.py"

# Setting names. The package (env_compat) holds this rule; the shell readers keep
# this one copy, and tests/test_setting_names_in_step.py keeps it in step. A setting
# `EXOMEM_X` may also be named `EVERMATTER_X` or the pre-rename `KB_MCP_X`; read
# `EXOMEM_X`, else `EVERMATTER_X`, else `KB_MCP_X`. A placement setting (it picks
# the vault or a state root) that a source names both `EXOMEM_X` and `EVERMATTER_X`
# with different values is refused: a guess runs on the wrong data. The message
# names both variables, never the values. Any other conflict lets `EXOMEM_X` win.
#
# exomem_resolve_setting LOOKUP SOURCE EXOMEM_X [LOOKUP_ARGS...] prints the value.
# LOOKUP LITERAL_NAME [LOOKUP_ARGS...] prints that one name's value and returns 1
# when the source does not set it, so a set but empty name still counts as set.
exomem_resolve_setting() {
    local lookup="$1" source="$2" name="$3" suffix
    local explicit="" evermatter="" legacy=""
    local has_explicit=1 has_evermatter=1 has_legacy=1
    shift 3
    if [[ "$name" != EXOMEM_* ]]; then
        "$lookup" "$name" "$@" || true
        return 0
    fi
    suffix="${name#EXOMEM_}"
    if explicit="$("$lookup" "EXOMEM_$suffix" "$@")"; then has_explicit=0; fi
    if evermatter="$("$lookup" "EVERMATTER_$suffix" "$@")"; then has_evermatter=0; fi
    if legacy="$("$lookup" "KB_MCP_$suffix" "$@")"; then has_legacy=0; fi
    if (( has_explicit == 0 )); then
        if (( has_evermatter == 0 )) && [[ "$evermatter" != "$explicit" ]]; then
            # The closed set of settings that choose where data lives, defined by the package itself.
            case "$suffix" in
                VAULT_PATH|STATE_ROOT|HOSTED_STATE_ROOT)
                    echo "EVERMATTER_$suffix and EXOMEM_$suffix are set to different values in $source, so it is unclear which to use. Delete one name of each pair, then run it again." >&2
                    return 2
                    ;;
            esac
        fi
        printf '%s' "$explicit"
    elif (( has_evermatter == 0 )); then
        printf '%s' "$evermatter"
    elif (( has_legacy == 0 )); then
        printf '%s' "$legacy"
    fi
    return 0
}

# One literal name out of the process environment, for exomem_resolve_setting.
exomem_process_env_lookup() {
    local literal="$1"
    [[ -n "${!literal+x}" ]] || return 1
    printf '%s' "${!literal}"
}

# Read EXOMEM_X out of the process environment under any of its names.
exomem_process_setting() {
    exomem_resolve_setting exomem_process_env_lookup "the process environment" "$1"
}

# Force EXOMEM_X to a value for every process this shell starts: set it and remove the
# EVERMATTER_X and KB_MCP_X spellings from the process environment. A child that
# inherited both names with different values would refuse to start (the package rule
# above), so a forced value that left an inherited alias in place would stop it.
exomem_export_setting() {
    local name="$1" value="$2"
    [[ "$name" == EXOMEM_?* ]] || { echo "exomem_export_setting takes an EXOMEM_ name, not $name" >&2; return 2; }
    unset "EVERMATTER_${name#EXOMEM_}" "KB_MCP_${name#EXOMEM_}"
    export "$name=$value"
}

exomem_transition_receipt_path() {
    local service_id="$1" root
    [[ "$service_id" =~ ^[A-Za-z0-9_.@-]+$ ]] || return 2
    if [[ -n "${EXOMEM_TRANSITION_RECEIPT_ROOT:-}" ]]; then
        root="$EXOMEM_TRANSITION_RECEIPT_ROOT"
    elif [[ "$(uname -s)" == "Darwin" ]]; then
        root="$HOME/Library/Application Support/Exomem/transitions"
    else
        root="${XDG_STATE_HOME:-$HOME/.local/state}/exomem/transitions"
    fi
    [[ "$root" == /* ]] || return 2
    printf '%s/%s.json\n' "$root" "$service_id"
}

exomem_create_transition_receipt() {
    local python="$1" receipt="$2" service_id="$3" binding="$4" state_root="$5"
    local vault="$6" port="$7" target_port="$8" worker_pid="$9" listener_pids="${10:-}"
    local args=(
        create --path "$receipt" --service-id "$service_id"
        --binding-path "$binding" --state-root "$state_root" --vault "$vault"
        --target-port "$target_port" --port "$port" --worker-pid "$worker_pid"
    ) pid
    while IFS= read -r pid; do
        [[ "$pid" =~ ^[1-9][0-9]*$ ]] && args+=(--listener-pid "$pid")
    done <<< "$listener_pids"
    "$python" "$EXOMEM_TRANSITION_RECEIPT_TOOL" "${args[@]}"
}

exomem_verify_transition_receipt() {
    local python="$1" receipt="$2" service_id="$3" binding="$4" state_root="$5" vault="$6" target_port="$7"
    "$python" "$EXOMEM_TRANSITION_RECEIPT_TOOL" verify \
        --path "$receipt" --service-id "$service_id" \
        --binding-path "$binding" --state-root "$state_root" --vault "$vault" \
        --target-port "$target_port" --json
}

exomem_transition_receipt_field() {
    local python="$1" receipt="$2" service_id="$3" binding="$4" state_root="$5" vault="$6" target_port="$7" field="$8"
    "$python" "$EXOMEM_TRANSITION_RECEIPT_TOOL" verify \
        --path "$receipt" --service-id "$service_id" \
        --binding-path "$binding" --state-root "$state_root" --vault "$vault" \
        --target-port "$target_port" --field "$field"
}

exomem_update_transition_receipt() {
    local python="$1" receipt="$2" service_id="$3" binding="$4" state_root="$5"
    local vault="$6" target_port="$7" phase="$8" observed_pids="${9:-}" pid
    local args=(
        phase --path "$receipt" --service-id "$service_id"
        --binding-path "$binding" --state-root "$state_root" --vault "$vault"
        --target-port "$target_port" --phase "$phase"
    )
    while IFS= read -r pid; do
        [[ "$pid" =~ ^[1-9][0-9]*$ ]] && args+=(--observed-pid "$pid")
    done <<< "$observed_pids"
    "$python" "$EXOMEM_TRANSITION_RECEIPT_TOOL" "${args[@]}"
}

exomem_publish_failed_transition_receipt() {
    local python="$1" receipt="$2" service_id="$3" binding="$4" state_root="$5"
    local vault="$6" target_port="$7" observed_worker_pid="${8:-0}"
    local phase receipt_port ports port listeners listener_pid proof_pids="" published_pids

    phase="$(exomem_transition_receipt_field \
        "$python" "$receipt" "$service_id" "$binding" "$state_root" "$vault" "$target_port" phase)" \
        || return 1
    if [[ "$observed_worker_pid" =~ ^[1-9][0-9]*$ ]]; then
        proof_pids="$observed_worker_pid"
    fi
    if [[ "$phase" == "starting" || "$phase" == "started" ]]; then
        receipt_port="$(exomem_transition_receipt_field \
            "$python" "$receipt" "$service_id" "$binding" "$state_root" "$vault" "$target_port" port)" \
            || return 1
        ports="$(printf '%s\n%s\n' "$receipt_port" "$target_port" | awk '!seen[$0]++')"
        while IFS= read -r port; do
            [[ "$port" =~ ^[1-9][0-9]*$ ]] || {
                echo "transition receipt contains an invalid listener port; failed-start proof was not published." >&2
                return 1
            }
            if ! listeners="$(exomem_listener_pids "$port")"; then
                echo "cannot capture a trustworthy failed-start listener pid set for port $port." >&2
                return 1
            fi
            while IFS= read -r listener_pid; do
                [[ -z "$listener_pid" ]] && continue
                [[ "$listener_pid" =~ ^[1-9][0-9]*$ ]] || {
                    echo "listener enumeration for port $port returned an unattributable process." >&2
                    return 1
                }
                proof_pids="${proof_pids:+$proof_pids$'\n'}$listener_pid"
            done <<< "$listeners"
        done <<< "$ports"
    fi
    proof_pids="$(printf '%s\n' "$proof_pids" | awk '/^[1-9][0-9]*$/ && !seen[$0]++')"
    if [[ "$phase" == "starting" || "$phase" == "started" ]]; then
        exomem_update_transition_receipt \
            "$python" "$receipt" "$service_id" "$binding" "$state_root" "$vault" \
            "$target_port" "$phase" "$proof_pids" || return 1
        published_pids="$(exomem_transition_receipt_field \
            "$python" "$receipt" "$service_id" "$binding" "$state_root" "$vault" \
            "$target_port" proof_pids)" || return 1
        while IFS= read -r listener_pid; do
            [[ -z "$listener_pid" ]] && continue
            grep -Fxq -- "$listener_pid" <<< "$published_pids" || {
                echo "durable failed-start process proof is incomplete; receipt remains non-resumable." >&2
                return 1
            }
        done <<< "$proof_pids"
        exomem_update_transition_receipt \
            "$python" "$receipt" "$service_id" "$binding" "$state_root" "$vault" \
            "$target_port" failed ""
        return
    fi
    exomem_update_transition_receipt \
        "$python" "$receipt" "$service_id" "$binding" "$state_root" "$vault" \
        "$target_port" failed "$proof_pids"
}

exomem_clear_transition_receipt() {
    local python="$1" receipt="$2" service_id="$3" binding="$4" state_root="$5" vault="$6" target_port="$7"
    "$python" "$EXOMEM_TRANSITION_RECEIPT_TOOL" clear \
        --path "$receipt" --service-id "$service_id" \
        --binding-path "$binding" --state-root "$state_root" --vault "$vault" \
        --target-port "$target_port"
}

exomem_unit_file() {
    case "$(uname -s)" in
        Darwin) echo "$HOME/Library/LaunchAgents/$(exomem_label).plist" ;;
        Linux)  echo "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$(exomem_service_name).service" ;;
        *)      return 1 ;;
    esac
}

# Print the service-manager identity declared by a rendered unit. A selected
# suffixed unit must never be controlled through the default exomem identity.
exomem_service_id() {
    local unit="${1:-}" value=""
    [[ -n "$unit" && -f "$unit" ]] || return 1
    case "$(uname -s)" in
        Darwin)
            if [[ -x /usr/libexec/PlistBuddy ]]; then
                value="$(/usr/libexec/PlistBuddy -c 'Print :Label' "$unit" 2>/dev/null || true)"
            elif command -v plutil >/dev/null 2>&1; then
                value="$(plutil -extract Label raw -o - "$unit" 2>/dev/null || true)"
            fi
            ;;
        Linux)
            value="$(basename "$unit")"
            value="${value%.service}"
            ;;
        *) return 1 ;;
    esac
    [[ "$value" =~ ^[A-Za-z0-9_.@-]+$ ]] || return 1
    printf '%s\n' "$value"
}

# Print the interpreter the installed service launches, or nothing.
exomem_service_python() {
    local unit="${1:-}"
    [[ -n "$unit" ]] || unit="$(exomem_unit_file)" || return 1
    [[ -f "$unit" ]] || return 1
    if [[ "$(uname -s)" == "Darwin" ]]; then
        # First ProgramArguments entry is the interpreter.
        grep -o '<string>[^<]*/bin/python</string>' "$unit" \
            | head -n1 | sed 's|<string>||; s|</string>||'
    else
        sed -n 's|^ExecStart="\([^"]*\)".*|\1|p' "$unit" | head -n1
    fi
}

exomem_service_is_managed() {
    local unit="${1:-}"
    [[ -f "$unit" ]] && grep -Eq '^ExecStart=.* -m exomem\.service_manager serve([[:space:]]|$)' "$unit"
}

exomem_managed_runtime_dir() {
    local unit="$1" python="$2"
    "$python" - "$unit" <<'PY'
import shlex
import sys
from pathlib import Path

for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if line.startswith("ExecStart="):
        args = shlex.split(line.partition("=")[2])
        if args[1:4] == ["-m", "exomem.service_manager", "serve"]:
            print(args[args.index("--runtime-dir") + 1])
            break
PY
}

# Print the port the service was installed with; defaults to 8765.
exomem_service_port() {
    local unit="${1:-}" port
    [[ -n "$unit" ]] || unit="$(exomem_unit_file)" || { echo 8765; return; }
    if [[ ! -f "$unit" ]]; then echo 8765; return; fi
    if [[ "$(uname -s)" == "Darwin" ]]; then
        port="$(grep -A1 '<string>--port</string>' "$unit" \
            | grep -o '<string>[0-9]*</string>' | head -n1 | tr -cd '0-9')"
    else
        port="$(sed -n 's|.*--port \([0-9]*\).*|\1|p' "$unit" | head -n1)"
    fi
    echo "${port:-8765}"
}

# Print the exact managed worker pid. Zero means no observable worker.
exomem_service_worker_pid() {
    local service_id="${1:-}" value=""
    case "$(uname -s)" in
        Darwin)
            [[ -n "$service_id" ]] || service_id="$(exomem_label)"
            value="$(launchctl print "gui/$(id -u)/$service_id" 2>/dev/null \
                | sed -n 's/^[[:space:]]*pid = \([0-9][0-9]*\)$/\1/p' | head -n1)"
            ;;
        Linux)
            [[ -n "$service_id" ]] || service_id="$(exomem_service_name)"
            value="$(systemctl --user show "$service_id" \
                --property MainPID --value 2>/dev/null || true)"
            ;;
    esac
    [[ "$value" =~ ^[1-9][0-9]*$ ]] && printf '%s\n' "$value" || printf '0\n'
}

exomem_pid_alive() {
    local pid="$1" observed
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
    observed="$(ps -p "$pid" -o pid= 2>/dev/null | tr -d '[:space:]')"
    [[ "$observed" == "$pid" ]]
}

exomem_service_is_stopped() {
    local service_id="${1:-}"
    case "$(uname -s)" in
        Darwin)
            [[ -n "$service_id" ]] || service_id="$(exomem_label)"
            ! launchctl print "gui/$(id -u)/$service_id" >/dev/null 2>&1
            ;;
        Linux)
            local state
            [[ -n "$service_id" ]] || service_id="$(exomem_service_name)"
            state="$(systemctl --user show "$service_id" \
                --property ActiveState --value 2>/dev/null || true)"
            [[ "$state" == "inactive" || "$state" == "failed" ]]
            ;;
        *) return 1 ;;
    esac
}

# Print unique pids listening on a port. Return 2 when no trustworthy probe is
# available; an empty successful result means the port is proven unbound.
exomem_listener_pids() {
    local port="$1" output status pids="" line line_pids
    if command -v lsof >/dev/null 2>&1; then
        set +e
        output="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>&1)"
        status=$?
        set -e
        if [[ "$status" -gt 1 ]]; then return 2; fi
        if [[ "$status" -eq 1 ]]; then
            [[ -z "$output" ]] || return 2
            return 0
        fi
        while IFS= read -r line; do
            [[ -z "$line" ]] && continue
            [[ "$line" =~ ^[1-9][0-9]*$ ]] || return 2
        done <<< "$output"
        printf '%s\n' "$output" | awk '!seen[$0]++'
        return 0
    fi
    if command -v ss >/dev/null 2>&1; then
        output="$(ss -H -ltnp "sport = :$port" 2>/dev/null)" || return 2
        [[ -z "$output" ]] && return 0
        while IFS= read -r line; do
            line_pids="$(printf '%s\n' "$line" \
                | grep -o 'pid=[0-9][0-9]*' \
                | cut -d= -f2 \
                | awk '!seen[$0]++')" || true
            # `ss` can mix attributable sockets with sockets owned by another
            # security context. Every visible row needs a pid; a partial set is
            # ambiguous and cannot authorize cleanup or migration.
            [[ -n "$line_pids" ]] || return 2
            pids="${pids:+$pids$'\n'}$line_pids"
        done <<< "$output"
        [[ -n "$pids" ]] || return 2
        printf '%s\n' "$pids" | awk '!seen[$0]++'
        return 0
    fi
    return 2
}

exomem_assert_listener_unbound() {
    local port="$1" listeners
    if ! listeners="$(exomem_listener_pids "$port")"; then
        echo "cannot prove listener port $port is unbound; neither lsof nor ss produced a trustworthy result." >&2
        return 1
    fi
    if [[ -n "$listeners" ]]; then
        echo "listener port $port is still owned by pid(s): $(printf '%s' "$listeners" | tr '\n' ' ')." >&2
        return 1
    fi
}

exomem_assert_listener_owned_by_worker() {
    local port="$1" worker_pid="$2" deadline=$((SECONDS + ${3:-60})) listeners
    while (( SECONDS < deadline )); do
        listeners="$(exomem_listener_pids "$port")" || {
            echo "cannot prove listener port $port belongs to worker $worker_pid." >&2
            return 1
        }
        if [[ "$listeners" == "$worker_pid" ]]; then return 0; fi
        if [[ -n "$listeners" ]]; then
            echo "listener port $port is not owned only by selected worker $worker_pid (observed: $listeners)." >&2
            return 1
        fi
        sleep 1
    done
    echo "selected worker $worker_pid never bound listener port $port." >&2
    return 1
}

exomem_stop_service() {
    local service_id="${1:-}"
    case "$(uname -s)" in
        Darwin)
            [[ -n "$service_id" ]] || service_id="$(exomem_label)"
            launchctl bootout "gui/$(id -u)/$service_id" >/dev/null 2>&1
            ;;
        Linux)
            [[ -n "$service_id" ]] || service_id="$(exomem_service_name)"
            systemctl --user stop "$service_id"
            ;;
        *) return 1 ;;
    esac
}

exomem_start_service() {
    local unit_file="$1" service_id="${2:-}"
    case "$(uname -s)" in
        Darwin)
            [[ -n "$service_id" ]] || service_id="$(exomem_label)"
            launchctl bootstrap "gui/$(id -u)" "$unit_file"
            launchctl kickstart "gui/$(id -u)/$service_id"
            ;;
        Linux)
            [[ -n "$service_id" ]] || service_id="$(exomem_service_name)"
            systemctl --user daemon-reload
            systemctl --user start "$service_id"
            ;;
        *) return 1 ;;
    esac
}

exomem_assert_service_stopped() {
    local captured_pids="$1" ports="$2" service_id="${3:-}" pid port
    exomem_service_is_stopped "$service_id" || {
        echo "service manager does not prove Evermatter stopped; offline migration refused." >&2
        return 1
    }
    [[ -n "$captured_pids" ]] || {
        echo "no pre-stop worker pid was captured; offline migration refused." >&2
        return 1
    }
    while IFS= read -r pid; do
        [[ "$pid" =~ ^[1-9][0-9]*$ ]] || continue
        if exomem_pid_alive "$pid"; then
            echo "captured Evermatter transition pid $pid is still alive; offline migration refused." >&2
            return 1
        fi
    done <<< "$captured_pids"
    while IFS= read -r port; do
        [[ "$port" =~ ^[1-9][0-9]*$ ]] || continue
        exomem_assert_listener_unbound "$port" || return 1
    done <<< "$ports"
}

exomem_assert_stopped_resume_authority() {
    local python="$1" receipt="$2" service_id="$3" binding="$4" state_root="$5" vault="$6" target_port="$7"
    local phase pids ports pid port
    exomem_verify_transition_receipt \
        "$python" "$receipt" "$service_id" "$binding" "$state_root" "$vault" "$target_port" \
        >/dev/null || {
        echo "exact durable transition receipt is missing or invalid; stopped-transition recovery refused." >&2
        return 1
    }
    phase="$(exomem_transition_receipt_field \
        "$python" "$receipt" "$service_id" "$binding" "$state_root" "$vault" "$target_port" phase)" \
        || return 1
    if [[ "$phase" == "starting" || "$phase" == "started" ]]; then
        echo "transition receipt records an incomplete start with no complete process proof; recovery refused." >&2
        return 1
    fi
    exomem_service_is_stopped "$service_id" || {
        echo "service manager does not prove Evermatter stopped; stopped-transition recovery refused." >&2
        return 1
    }
    pids="$(exomem_transition_receipt_field \
        "$python" "$receipt" "$service_id" "$binding" "$state_root" "$vault" "$target_port" proof_pids)" \
        || return 1
    while IFS= read -r pid; do
        [[ "$pid" =~ ^[1-9][0-9]*$ ]] || continue
        if exomem_pid_alive "$pid"; then
            echo "captured transition pid $pid is still alive; recovery refused." >&2
            return 1
        fi
    done <<< "$pids"
    ports="$(
        exomem_transition_receipt_field \
            "$python" "$receipt" "$service_id" "$binding" "$state_root" "$vault" "$target_port" port
        printf '%s\n' "$target_port"
    )"
    while IFS= read -r port; do
        [[ "$port" =~ ^[1-9][0-9]*$ ]] || continue
        exomem_assert_listener_unbound "$port" || return 1
    done <<< "$(printf '%s\n' "$ports" | awk '!seen[$0]++')"
}

exomem_assert_service_restarted() {
    local before="${1:-0}" after="${2:-0}"
    if ! [[ "$after" =~ ^[1-9][0-9]*$ ]] || ! exomem_pid_alive "$after"; then
        echo "no running Evermatter worker pid was observed after start." >&2
        return 1
    fi
    if [[ "$before" =~ ^[1-9][0-9]*$ && "$before" == "$after" ]]; then
        echo "Evermatter is still running the captured pre-transition pid $before." >&2
        return 1
    fi
}

exomem_wait_worker_pid() {
    local deadline=$((SECONDS + ${1:-60})) service_id="${2:-}" pid
    while (( SECONDS < deadline )); do
        pid="$(exomem_service_worker_pid "$service_id")"
        if [[ "$pid" =~ ^[1-9][0-9]*$ ]]; then printf '%s\n' "$pid"; return 0; fi
        sleep 1
    done
    return 1
}

exomem_platform_state_root() {
    printf '%s/exomem/state\n' "${XDG_STATE_HOME:-$HOME/.local/state}"
}

# Return and, when absent, persist the exact state root consumed by both the
# target interpreter and its managed service. The helper prints only that path;
# it never renders the rest of the service environment.
exomem_bind_service_state_root() {
    local unit_file="$1" python="$2" preferred="${3:-}" mode="${4:-bind}"
    "$python" - "$unit_file" "$(uname -s)" "$preferred" "$mode" <<'PY'
from __future__ import annotations

import os
import plistlib
import re
import sys
from pathlib import Path

unit = Path(sys.argv[1])
platform = sys.argv[2]
preferred = sys.argv[3].strip()
mode = sys.argv[4]

def atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)

# The binding may be named under any of the state root's names. The rule is
# exomem_resolve_setting's (read EXOMEM_, else EVERMATTER_, else KB_MCP_; refuse
# EXOMEM_ against EVERMATTER_ with different values), kept in step by
# tests/test_setting_names_in_step.py. The writer below adds EXOMEM_STATE_ROOT only when none is set.
NAMES = ("EXOMEM_STATE_ROOT", "EVERMATTER_STATE_ROOT", "KB_MCP_STATE_ROOT")

def resolve_existing(found: dict[str, str], source: str) -> str:
    found = {name: value.strip() for name, value in found.items()}
    explicit = found.get(NAMES[0])
    if explicit is not None and NAMES[1] in found and found[NAMES[1]] != explicit:
        raise SystemExit(
            f"{NAMES[1]} and {NAMES[0]} are set to different values in {source}, "
            "so it is unclear which to use. Delete one name of each pair, then run it again."
        )
    return next((found[name] for name in NAMES if name in found), "")

if platform == "Darwin":
    binding_path = unit
    payload = plistlib.loads(unit.read_bytes())
    environment = payload.setdefault("EnvironmentVariables", {})
    existing = resolve_existing(
        {name: str(environment[name]) for name in NAMES if name in environment}, str(unit)
    )
else:
    text = unit.read_text(encoding="utf-8")
    match = re.search(r"(?m)^EnvironmentFile=([^\n]+)$", text)
    if not match:
        raise SystemExit("managed systemd unit has no EnvironmentFile")
    encoded = match.group(1).strip().lstrip("-")
    env_path = Path(re.sub(r"\\x([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), encoded))
    binding_path = env_path
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    found: dict[str, str] = {}
    for line in lines:
        name, sep, value = line.partition("=")
        if sep and name in NAMES:
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1].replace(r'\"', '"').replace(r"\\", "\\")
            found[name] = value
    existing = resolve_existing(found, str(env_path))
if mode == "binding-path":
    print(binding_path.expanduser().resolve(strict=False))
    raise SystemExit(0)

resolved_existing = (
    str(Path(existing).expanduser().resolve(strict=False)) if existing else ""
)
resolved_preferred = (
    str(Path(preferred).expanduser().resolve(strict=False)) if preferred else ""
)
if mode == "bind" and resolved_existing and resolved_preferred and resolved_existing != resolved_preferred:
    raise SystemExit("managed EXOMEM_STATE_ROOT does not match the durable transition receipt")
selected = resolved_existing or resolved_preferred
if not selected or not Path(selected).is_absolute():
    raise SystemExit("managed EXOMEM_STATE_ROOT must be absolute")
if mode == "select":
    print(selected)
    raise SystemExit(0)
if mode != "bind":
    raise SystemExit("invalid state-root binding mode")

# An empty binding is filled under the name the file already uses; EXOMEM_STATE_ROOT is
# appended only when the file names the state root under no name, never beside an
# EVERMATTER_ line that would then differ from it.
if platform == "Darwin":
    if not existing:
        for name in [n for n in NAMES if n in environment] or [NAMES[0]]:
            environment[name] = selected
        atomic_write(unit, plistlib.dumps(payload, sort_keys=False))
else:
    if not existing:
        rendered = selected.replace("\\", "\\\\").replace('"', r'\"')
        named = [i for i, line in enumerate(lines) if line.partition("=")[0] in NAMES]
        for i in named:
            lines[i] = f'{lines[i].partition("=")[0]}="{rendered}"'
        if not named:
            lines.append(f'EXOMEM_STATE_ROOT="{rendered}"')
        env_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(env_path, ("\n".join(lines) + "\n").encode())
print(selected)
PY
}

exomem_select_service_state_root() {
    exomem_bind_service_state_root "$1" "$2" "${3:-}" select
}

exomem_service_binding_path() {
    exomem_bind_service_state_root "$1" "$2" "" binding-path
}

# The release installs under `evermatter` or, for installs made before the
# rename, `exomem`; the probe reads whichever name the interpreter has.
# A copy of install_info.DISTRIBUTION_NAMES, temporary: openspec rename-to-evermatter task 4.2 retires it.
exomem_installed_version() {
    local python="$1"
    [[ -x "$python" ]] || return 1
    "$python" -c "
import importlib.metadata as m
for name in ('evermatter', 'exomem'):
    try:
        print(m.version(name))
        break
    except m.PackageNotFoundError:
        continue
else:
    raise SystemExit(1)
" 2>/dev/null
}

# Pull the exomem version out of a `uv pip install` plan read on stdin, or print
# nothing. uv prints its plan as " + exomem==0.52.3" (extras normalised away), and
# prints NO "+ exomem" line when it would install nothing.
exomem_install_plan_version() {
    sed -n 's|^[[:space:]]*+[[:space:]]*exomem==\([^[:space:]]*\)[[:space:]]*$|\1|p' | head -n1
}

# Print the concrete version an install of $2 would land in the interpreter $1, or
# return nonzero when it cannot be resolved. $3 is the currently-installed version.
#
# `--dry-run` resolves without writing, so the target can be shown to the operator
# BEFORE the install lands and the post-install assertion has something concrete to
# compare against when no --package-version was pinned. `--refresh-package exomem`
# is load-bearing: uv serves the package index out of its HTTP cache, so an unpinned
# `--upgrade` can resolve to a release that is no longer latest and exit 0 having
# done nothing -- and a target read through the same stale cache would agree with
# the stale install and vouch for it. uv writes its whole plan to stderr.
exomem_resolve_target_version() {
    local python="$1" requirement="$2" installed="${3:-}" plan target
    plan="$(uv pip install --dry-run --upgrade --refresh-package exomem \
        --python "$python" "$requirement" 2>&1)" || return 1
    target="$(printf '%s\n' "$plan" | exomem_install_plan_version)"
    if [[ -n "$target" ]]; then printf '%s\n' "$target"; return 0; fi
    # uv planned no change to exomem, so the resolved target is the installed one.
    if [[ -n "$installed" ]]; then printf '%s\n' "$installed"; return 0; fi
    return 1
}

# Fail when an install reported success without changing what is installed.
# #578: uv exited 0 having installed nothing, the script printed
# "Installed version: 0.52.2 -> 0.52.2" as a REPORT, and the deploy continued onto
# a service that restarted cleanly on the old build. before/after were already
# known; this turns the report into a gate. Args: requirement before after target.
exomem_assert_install_applied() {
    local requirement="$1" before="${2:-}" after="${3:-}" target="${4:-}" was
    was="${before:-not installed}"
    if [[ -z "$after" ]]; then
        echo "upgrade: install of '$requirement' reported success but no exomem version is importable from that interpreter (was: $was). Nothing was deployed." >&2
        return 1
    fi
    if [[ -z "$target" ]]; then
        echo "upgrade: target version unresolved, so this run can only assert that SOMETHING is installed ($after). Re-run with --package-version <version> for a checked upgrade." >&2
        return 0
    fi
    if [[ "$after" != "$target" ]]; then
        echo "upgrade: install did not take: '$requirement' resolved to $target, but the interpreter still reports $after (was: $was). uv exited 0 without applying the change; re-run with --package-version $target, and check that uv is not resolving from a stale index cache." >&2
        return 1
    fi
    return 0
}

# Version declared in the repo checkout. Deliberately offline: comparing against
# the repo rather than PyPI keeps every gate usable on a disconnected box.
exomem_repo_version() {
    local repo_root="$1"
    sed -n 's|^version *= *"\([^"]*\)".*|\1|p' "$repo_root/pyproject.toml" | head -n1
}

# One literal name out of an exact dotenv file, for exomem_resolve_setting.
exomem_dotenv_file_lookup() {
    local name="$1" env_file="$2"
    [[ -f "$env_file" ]] || return 1
    grep -Eq "^[[:space:]]*${name}[[:space:]]*=" "$env_file" || return 1
    sed -n "s|^[[:space:]]*${name}[[:space:]]*=[[:space:]]*||p" "$env_file" \
        | head -n1 | sed 's|^"\(.*\)"$|\1|; s|^'"'"'\(.*\)'"'"'$|\1|'
}

# Read one setting out of an exact dotenv file under any of its names, or nothing.
exomem_dotenv_file_value() {
    local env_file="$1" name="$2"
    exomem_resolve_setting exomem_dotenv_file_lookup "$env_file" "$name" "$env_file"
}

# Read one setting out of <repo>/.env, or nothing.
exomem_dotenv_value() {
    local repo_root="$1" name="$2"
    exomem_dotenv_file_value "$repo_root/.env" "$name"
}

# True when $1 is the platform's default unit rather than a second service
# (such as a client cell on another port) installed beside it.
exomem_unit_is_default() {
    local unit="${1:-}" default
    default="$(exomem_unit_file)" || return 1
    [[ -n "$unit" && "$unit" -ef "$default" ]]
}

# One literal name out of the environment a rendered unit gives its service, for
# exomem_resolve_setting. The unit's Environment= assignments come first, split
# the way systemd splits them (a later assignment wins); systemd applies each
# EnvironmentFile= after them, so a file that sets the name wins.
exomem_unit_env_lookup() {
    local name="$1" unit="$2" python="$3" value="" present=1 env_file line
    if [[ "$(uname -s)" == "Darwin" ]]; then
        [[ -x /usr/libexec/PlistBuddy ]] || return 1
        value="$(/usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:$name" "$unit" 2>/dev/null)" \
            || return 1
        printf '%s' "$value"
        return 0
    fi
    if value="$("$python" - "$unit" "$name" <<'PY'
import shlex
import sys
from pathlib import Path

found = False
value = ""
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if line.startswith("Environment="):
        for word in shlex.split(line.partition("=")[2]):
            key, sep, rest = word.partition("=")
            if sep and key == sys.argv[2]:
                found = True
                value = rest
print(value, end="")
raise SystemExit(0 if found else 1)
PY
)"; then
        present=0
    else
        value=""
    fi
    while IFS= read -r env_file; do
        env_file="${env_file#-}"
        if line="$(exomem_dotenv_file_lookup "$name" "$env_file")"; then
            value="$line"
            present=0
        fi
    done < <(sed -n 's|^EnvironmentFile=||p' "$unit")
    (( present == 0 )) || return 1
    printf '%s' "$value"
}

# Read one setting out of the environment a rendered unit gives its service, under
# any of its names, or nothing. A second service's settings live in its own unit,
# never in the checkout's .env, which describes the default service.
exomem_unit_env_value() {
    local unit="$1" name="$2" python="${3:-python3}"
    [[ -f "$unit" ]] || return 0
    exomem_resolve_setting exomem_unit_env_lookup \
        "the service environment of $unit" "$name" "$unit" "$python"
}

# Print the name of the per-user uv tool that owns the CLI, `evermatter` or, for
# an install made before the rename, `exomem`; fail when there is none.  Looking
# at the registry, rather than merely `command -v exomem`, avoids taking over an
# independently managed pip/pipx command in auto mode.  A tool keeps upgrading
# under its own name: installing the other name beside it would collide on the
# shared `exomem` and `kb` executables.
exomem_uv_tool_name() {
    command -v uv >/dev/null 2>&1 || return 1
    local listing name
    listing="$(uv tool list 2>/dev/null)" || return 1
    for name in evermatter exomem; do
        if grep -Eq "^${name}([[:space:]]|\$)" <<< "$listing"; then
            printf '%s\n' "$name"
            return 0
        fi
    done
    return 1
}

# Align the lean user-facing command with the exact release verified from the
# live service.  The service keeps its selected extras; duplicating its media/ML
# stack into the uv-tool environment would waste gigabytes and is not parity.
exomem_sync_uv_cli() {
    local mode="$1" service_version="$2"
    case "$mode" in
        never)
            echo "CLI sync disabled (--cli-sync never)."
            return 0
            ;;
        auto)
            if ! exomem_uv_tool_name >/dev/null; then
                echo "No uv tool named evermatter or exomem is installed; auto mode will not install one."
                return 0
            fi
            ;;
        always) ;;
        *)
            echo "invalid CLI sync mode: $mode" >&2
            return 2
            ;;
    esac
    command -v uv >/dev/null 2>&1 || {
        echo "uv is required for CLI sync; install uv or use --cli-sync never." >&2
        return 1
    }
    [[ -n "$service_version" ]] || {
        echo "cannot sync the CLI without a verified live service version." >&2
        return 1
    }
    local tool
    tool="$(exomem_uv_tool_name || echo evermatter)"
    echo "Aligning lean uv-tool CLI to $tool==$service_version..."
    # `--force` also replaces an operator-owned launcher (a regular file, where
    # uv would put a symlink) in uv's bin directory. Keep those and put them back.
    local bin_dir saved name
    bin_dir="$(uv tool dir --bin 2>/dev/null || true)"
    saved="$(mktemp -d)"
    for name in evermatter exomem kb; do
        if [[ -n "$bin_dir" && -f "$bin_dir/$name" && ! -L "$bin_dir/$name" ]]; then
            cp -p "$bin_dir/$name" "$saved/$name"
        fi
    done
    local status=0
    uv tool install --force "$tool==$service_version" --compile-bytecode || status=$?
    for name in evermatter exomem kb; do
        if [[ -f "$saved/$name" ]]; then
            rm -f "$bin_dir/$name"
            mv "$saved/$name" "$bin_dir/$name"
            echo "Kept the operator launcher $bin_dir/$name"
        fi
    done
    rmdir "$saved" 2>/dev/null || true
    return "$status"
}

exomem_managed_manifest_path() {
    if [[ -n "${EXOMEM_MANAGED_INSTALL_MANIFEST:-}" ]]; then
        printf '%s\n' "$EXOMEM_MANAGED_INSTALL_MANIFEST"
    elif [[ "$(uname -s)" == "Darwin" ]]; then
        printf '%s\n' "$HOME/Library/Application Support/Exomem/managed-install.json"
    else
        printf '%s\n' "${XDG_CONFIG_HOME:-$HOME/.config}/exomem/managed-install.json"
    fi
}

exomem_write_managed_manifest() {
    local python="$1" service_version="$2" service_profile="$3" service_target="$4"
    local path
    path="$(exomem_managed_manifest_path)"
    "$python" - "$path" "$service_version" "$service_profile" "$service_target" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
payload = {
    "schema_version": 1,
    "service_version": sys.argv[2],
    "service_profile": sys.argv[3],
    "service_target": sys.argv[4],
    "cli_profile": "lean",
    "cli_route": "direct",
}
temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
os.chmod(temporary, 0o600)
os.replace(temporary, path)
PY
    echo "Managed install manifest: $path"
}

# The manifest names ONE service: the one the CLI and TUI route to. Only the
# default unit, or the service the manifest already names, may rewrite it; a
# second service on this machine leaves it where it points.
exomem_write_owned_managed_manifest() {
    local unit="$1" python="$2" service_version="$3" service_profile="$4" service_target="$5"
    local path current=""
    path="$(exomem_managed_manifest_path)"
    if ! exomem_unit_is_default "$unit" && [[ -f "$path" ]]; then
        # An unreadable manifest is the default service's to repair, not ours to claim.
        current="$("$python" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("service_target") or "")' "$path" 2>/dev/null || true)"
        if [[ "$current" != "$service_target" ]]; then
            echo "Managed install manifest left on ${current:-its unreadable contents}: $unit is not the service it names."
            return 0
        fi
    fi
    exomem_write_managed_manifest "$python" "$service_version" "$service_profile" "$service_target"
}

# Verify each PATH-visible console script, not just the first shim.  A stale
# shadowed command is a split install waiting to reappear after PATH changes.
exomem_verify_visible_clis() {
    local expected="$1" python="$2" require_one="${3:-0}"
    local found=0 command_name executable output actual tool
    tool="$(exomem_uv_tool_name || echo evermatter)"
    for command_name in evermatter exomem kb; do
        while IFS= read -r executable; do
            [[ -n "$executable" ]] || continue
            found=1
            if ! output="$("$executable" --version --json 2>&1)"; then
                echo "CLI verification failed: $executable does not support --version --json. Repair with: uv tool install --force $tool==$expected" >&2
                return 1
            fi
            if ! actual="$(printf '%s' "$output" | "$python" -c 'import json,sys; print(json.load(sys.stdin).get("version", ""))')"; then
                echo "CLI verification failed: $executable returned invalid version JSON. Repair with: uv tool install --force $tool==$expected" >&2
                return 1
            fi
            if [[ "$actual" != "$expected" ]]; then
                echo "CLI/service split: $executable reports '$actual' while the live service reports '$expected'. Repair with: uv tool install --force $tool==$expected" >&2
                return 1
            fi
            echo "Verified $executable -> exomem $actual"
        done < <(type -a -p "$command_name" 2>/dev/null | awk '!seen[$0]++')
    done
    if [[ "$require_one" == 1 && "$found" == 0 ]]; then
        echo "CLI sync completed but none of evermatter, exomem or kb is visible on PATH. Run 'uv tool update-shell', open a new shell, and retry." >&2
        return 1
    fi
}
