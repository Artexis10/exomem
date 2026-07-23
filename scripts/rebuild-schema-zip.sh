#!/usr/bin/env bash
# Thin wrapper -> the cross-platform Python builder, scripts/rebuild-schema-zip.py.
# It assembles the claude.ai zip from the public scaffold (src/exomem/_scaffold/_Schema),
# overlaying your real project-keys.yaml only when --vault is passed explicitly.
# Requires Python (no `zip` CLI needed).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$here/rebuild-schema-zip.py" "$@"
fi
exec python "$here/rebuild-schema-zip.py" "$@"
