#!/bin/sh
# Smoke test for scripts/install.sh.
#
# Puts mock `curl`/`uv`/`exomem` on PATH and asserts install.sh's control
# flow end to end WITHOUT ever touching the network or doing a real install:
#
#   1. uv missing  -> install.sh must run the (mock) uv installer, then still
#                     run `uv tool install exomem`, then either invoke
#                     `exomem setup` or print it as the next command.
#   2. uv present  -> a re-run (idempotent case) must skip the installer and
#                     go straight to `uv tool install exomem`.
#
# Usage: sh tests/scripts/test_install_sh.sh

set -eu

SCRIPT_DIR=$(CDPATH="" cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH="" cd -- "$SCRIPT_DIR/../.." && pwd)
INSTALL_SH="$REPO_ROOT/scripts/install.sh"

[ -f "$INSTALL_SH" ] || { echo "FAIL: $INSTALL_SH not found" >&2; exit 1; }

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT INT TERM

fail() {
    printf 'FAIL: %s\n' "$1" >&2
    exit 1
}

export MOCK_LOG="$WORKDIR/calls.log"

# A bin dir with only a mock `curl` on it — real uv/exomem must NOT be
# reachable through it, so install.sh's own "is uv missing" detection is
# exercised for real.
MOCK_BIN="$WORKDIR/mock-bin"
mkdir -p "$MOCK_BIN"

# This dev machine may already have real uv/exomem installed (commonly at
# the very same ~/.local/bin the mocks below also use) — strip that
# directory out of PATH so the mocks are what install.sh actually finds.
CLEAN_PATH="$PATH"
real_uv=$(command -v uv 2>/dev/null || true)
if [ -n "$real_uv" ]; then
    real_uv_dir=$(dirname "$real_uv")
    new_path=""
    old_ifs=$IFS
    IFS=:
    for d in $PATH; do
        if [ "$d" != "$real_uv_dir" ]; then
            new_path="${new_path:+$new_path:}$d"
        fi
    done
    IFS=$old_ifs
    CLEAN_PATH="$new_path"
fi

# Mock curl: ignores the URL entirely. Instead of downloading anything, it
# drops a mock `uv` (and the `env` file the real uv installer writes) into
# $HOME/.local/bin — the same place the real installer would — then prints a
# harmless one-liner for install.sh to pipe into `sh`.
cat > "$MOCK_BIN/curl" <<'CURLEOF'
#!/bin/sh
echo "CURL $*" >> "$MOCK_LOG"
mkdir -p "$HOME/.local/bin"

cat > "$HOME/.local/bin/env" <<'ENVEOF'
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) export PATH="$HOME/.local/bin:$PATH" ;;
esac
ENVEOF

cat > "$HOME/.local/bin/uv" <<'UVEOF'
#!/bin/sh
echo "UV $*" >> "$MOCK_LOG"
if [ "$1" = "tool" ] && [ "$2" = "install" ] && [ "$3" = "exomem" ]; then
    mkdir -p "$HOME/.local/bin"
    cat > "$HOME/.local/bin/exomem" <<'EXEOF'
#!/bin/sh
echo "EXOMEM $*" >> "$MOCK_LOG"
exit 0
EXEOF
    chmod +x "$HOME/.local/bin/exomem"
    exit 0
fi
if [ "$1" = "--version" ]; then
    echo "uv 0.0.0 (mock)"
fi
exit 0
UVEOF
chmod +x "$HOME/.local/bin/uv"

echo ": mock uv installer ran"
CURLEOF
chmod +x "$MOCK_BIN/curl"

# ---------------------------------------------------------------------------
# Scenario 1: uv missing.
# ---------------------------------------------------------------------------
: > "$MOCK_LOG"
HOME1="$WORKDIR/home-missing-uv"
mkdir -p "$HOME1"

output1=$(HOME="$HOME1" PATH="$MOCK_BIN:$CLEAN_PATH" sh "$INSTALL_SH" </dev/null 2>&1) \
    || fail "install.sh exited non-zero on the 'uv missing' scenario. Output:
$output1"

grep -q '^CURL ' "$MOCK_LOG" \
    || fail "expected install.sh to invoke curl to fetch the uv installer when uv is missing. Log:
$(cat "$MOCK_LOG")"

grep -q '^UV tool install exomem$' "$MOCK_LOG" \
    || fail "expected install.sh to run 'uv tool install exomem'. Log:
$(cat "$MOCK_LOG")"

if ! grep -q '^EXOMEM setup$' "$MOCK_LOG" && ! printf '%s' "$output1" | grep -q 'exomem setup'; then
    fail "expected install.sh to either invoke 'exomem setup' or print it as the next command. Output:
$output1"
fi

echo "PASS: scenario 1 - uv missing -> installs uv, then installs + sets up exomem"

# ---------------------------------------------------------------------------
# Scenario 2: uv already installed (idempotent re-run) -> no installer call.
# ---------------------------------------------------------------------------
: > "$MOCK_LOG"
HOME2="$WORKDIR/home-has-uv"
mkdir -p "$HOME2/.local/bin"

cat > "$HOME2/.local/bin/uv" <<'UVEOF'
#!/bin/sh
echo "UV $*" >> "$MOCK_LOG"
if [ "$1" = "tool" ] && [ "$2" = "install" ] && [ "$3" = "exomem" ]; then
    mkdir -p "$HOME/.local/bin"
    cat > "$HOME/.local/bin/exomem" <<'EXEOF'
#!/bin/sh
echo "EXOMEM $*" >> "$MOCK_LOG"
exit 0
EXEOF
    chmod +x "$HOME/.local/bin/exomem"
    exit 0
fi
if [ "$1" = "--version" ]; then
    echo "uv 0.0.0 (mock)"
fi
exit 0
UVEOF
chmod +x "$HOME2/.local/bin/uv"

output2=$(HOME="$HOME2" PATH="$HOME2/.local/bin:$MOCK_BIN:$CLEAN_PATH" sh "$INSTALL_SH" </dev/null 2>&1) \
    || fail "install.sh exited non-zero on the 'uv already installed' scenario. Output:
$output2"

if grep -q '^CURL ' "$MOCK_LOG"; then
    fail "install.sh invoked curl (the uv installer) even though uv was already on PATH. Log:
$(cat "$MOCK_LOG")"
fi

grep -q '^UV tool install exomem$' "$MOCK_LOG" \
    || fail "expected install.sh to still run 'uv tool install exomem' on a re-run. Log:
$(cat "$MOCK_LOG")"

if ! grep -q '^EXOMEM setup$' "$MOCK_LOG" && ! printf '%s' "$output2" | grep -q 'exomem setup'; then
    fail "expected install.sh to either invoke 'exomem setup' or print it as the next command on a re-run. Output:
$output2"
fi

echo "PASS: scenario 2 - uv already installed -> skips the installer, still installs + sets up exomem"

echo "PASS: all install.sh smoke tests"
