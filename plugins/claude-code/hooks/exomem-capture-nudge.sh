#!/usr/bin/env bash
# Stop-hook wrapper for exomem_capture_nudge.py. Resolves the interpreter per machine
# (python3 on Linux/WSL/macOS, python on Windows Git Bash) and runs the sibling
# script, so the SAME yadm-synced ~/.claude/hooks works across machines. Registered
# in settings.json as `bash ~/.claude/hooks/exomem-capture-nudge.sh` — machine-agnostic,
# unlike an absolute interpreter path. Exit 0 always: a hook must never break the session.
here="$(cd "$(dirname "$0")" && pwd)"
py="$here/exomem_capture_nudge.py"
if command -v python3 >/dev/null 2>&1; then exec python3 "$py"; fi
command -v python >/dev/null 2>&1 && exec python "$py"
exit 0
