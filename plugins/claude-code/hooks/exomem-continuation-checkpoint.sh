#!/usr/bin/env bash
# Machine-agnostic Claude command-hook wrapper. The client identity is passed by
# the installed command and forwarded unchanged to the standalone Python core.
here="$(cd "$(dirname "$0")" && pwd)"
py="$here/exomem_continuation_checkpoint.py"
if command -v python3 >/dev/null 2>&1; then exec python3 "$py" "$@"; fi
command -v python >/dev/null 2>&1 && exec python "$py" "$@"
exit 0
