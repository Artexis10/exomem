#!/usr/bin/env bash
# Once the notice has been shown, skip starting an interpreter at every session start. The marker's
# name is MARKER in evermatter_sign_in_notice.py, and tests/test_plugin_sync.py fails if they differ.
if [ -n "$CLAUDE_PLUGIN_DATA" ] && [ -e "$CLAUDE_PLUGIN_DATA/sign-in-notice-shown" ]; then exit 0; fi
here="$(cd "$(dirname "$0")" && pwd)"
py="$here/evermatter_sign_in_notice.py"
if command -v python3 >/dev/null 2>&1; then exec python3 "$py" "$@"; fi
command -v python >/dev/null 2>&1 && exec python "$py" "$@"
exit 0
