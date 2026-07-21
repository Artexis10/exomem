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
