#!/usr/bin/env bash
here="$(cd "$(dirname "$0")" && pwd)"
py="$here/evermatter_sign_in_notice.py"
if command -v python3 >/dev/null 2>&1; then exec python3 "$py" "$@"; fi
command -v python >/dev/null 2>&1 && exec python "$py" "$@"
exit 0
