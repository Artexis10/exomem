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

exomem_unit_file() {
    case "$(uname -s)" in
        Darwin) echo "$HOME/Library/LaunchAgents/$(exomem_label).plist" ;;
        Linux)  echo "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$(exomem_service_name).service" ;;
        *)      return 1 ;;
    esac
}

# Print the interpreter the installed service launches, or nothing.
exomem_service_python() {
    local unit
    unit="$(exomem_unit_file)" || return 1
    [[ -f "$unit" ]] || return 1
    if [[ "$(uname -s)" == "Darwin" ]]; then
        # First ProgramArguments entry is the interpreter.
        grep -o '<string>[^<]*/bin/python</string>' "$unit" \
            | head -n1 | sed 's|<string>||; s|</string>||'
    else
        sed -n 's|^ExecStart="\([^"]*\)".*|\1|p' "$unit" | head -n1
    fi
}

# Print the port the service was installed with; defaults to 8765.
exomem_service_port() {
    local unit port
    unit="$(exomem_unit_file)" || { echo 8765; return; }
    if [[ ! -f "$unit" ]]; then echo 8765; return; fi
    if [[ "$(uname -s)" == "Darwin" ]]; then
        port="$(grep -A1 '<string>--port</string>' "$unit" \
            | grep -o '<string>[0-9]*</string>' | head -n1 | tr -cd '0-9')"
    else
        port="$(sed -n 's|.*--port \([0-9]*\).*|\1|p' "$unit" | head -n1)"
    fi
    echo "${port:-8765}"
}

exomem_installed_version() {
    local python="$1"
    [[ -x "$python" ]] || return 1
    "$python" -c "import importlib.metadata as m; print(m.version('exomem'))" 2>/dev/null
}

# Version declared in the repo checkout. Deliberately offline: comparing against
# the repo rather than PyPI keeps every gate usable on a disconnected box.
exomem_repo_version() {
    local repo_root="$1"
    sed -n 's|^version *= *"\([^"]*\)".*|\1|p' "$repo_root/pyproject.toml" | head -n1
}

# Read one key out of <repo>/.env, or nothing.
exomem_dotenv_value() {
    local repo_root="$1" name="$2"
    [[ -f "$repo_root/.env" ]] || return 0
    sed -n "s|^[[:space:]]*${name}[[:space:]]*=[[:space:]]*||p" "$repo_root/.env" \
        | head -n1 | sed 's|^"\(.*\)"$|\1|; s|^'"'"'\(.*\)'"'"'$|\1|'
}

# True when the per-user uv tool registry already owns Exomem.  Looking at the
# registry, rather than merely `command -v exomem`, avoids taking over an
# independently managed pip/pipx command in auto mode.
exomem_uv_tool_has_exomem() {
    command -v uv >/dev/null 2>&1 || return 1
    uv tool list 2>/dev/null | grep -Eq '^exomem([[:space:]]|$)'
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
            if ! exomem_uv_tool_has_exomem; then
                echo "No existing uv-managed Exomem CLI; auto mode will not install one."
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
    echo "Aligning lean uv-tool CLI to exomem==$service_version..."
    uv tool install --force "exomem==$service_version"
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

# Verify each PATH-visible console script, not just the first shim.  A stale
# shadowed command is a split install waiting to reappear after PATH changes.
exomem_verify_visible_clis() {
    local expected="$1" python="$2" require_one="${3:-0}"
    local found=0 command_name executable output actual
    for command_name in exomem kb; do
        while IFS= read -r executable; do
            [[ -n "$executable" ]] || continue
            found=1
            if ! output="$("$executable" --version --json 2>&1)"; then
                echo "CLI verification failed: $executable does not support --version --json. Repair with: uv tool install --force exomem==$expected" >&2
                return 1
            fi
            if ! actual="$(printf '%s' "$output" | "$python" -c 'import json,sys; print(json.load(sys.stdin).get("version", ""))')"; then
                echo "CLI verification failed: $executable returned invalid version JSON. Repair with: uv tool install --force exomem==$expected" >&2
                return 1
            fi
            if [[ "$actual" != "$expected" ]]; then
                echo "CLI/service split: $executable reports '$actual' while the live service reports '$expected'. Repair with: uv tool install --force exomem==$expected" >&2
                return 1
            fi
            echo "Verified $executable -> exomem $actual"
        done < <(type -a -p "$command_name" 2>/dev/null | awk '!seen[$0]++')
    done
    if [[ "$require_one" == 1 && "$found" == 0 ]]; then
        echo "CLI sync completed but neither exomem nor kb is visible on PATH. Run 'uv tool update-shell', open a new shell, and retry." >&2
        return 1
    fi
}
