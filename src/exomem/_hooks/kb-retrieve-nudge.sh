#!/usr/bin/env bash
# UserPromptSubmit-hook wrapper for kb_retrieve_nudge.py. Resolves the interpreter
# per machine (python3 on Linux/WSL/macOS, python on Windows Git Bash) and runs the
# sibling script, so the SAME yadm-synced ~/.claude/hooks works across machines.
# Registered as `bash ~/.claude/hooks/kb-retrieve-nudge.sh` — machine-agnostic.
# Exit 0 always: a hook must never break the session.
here="$(cd "$(dirname "$0")" && pwd)"
py="$here/kb_retrieve_nudge.py"
if command -v python3 >/dev/null 2>&1; then exec python3 "$py"; fi
command -v python >/dev/null 2>&1 && exec python "$py"
exit 0
