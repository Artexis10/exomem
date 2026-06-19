#!/usr/bin/env bash
# Thin wrapper -> the cross-platform Python builder, scripts/rebuild-schema-zip.py.
# It strips the canonical's GENERIC markers (keeping your real content) so the
# claude.ai zip carries no marker comments. Resolves the vault from --vault or
# $KB_MCP_VAULT_PATH. Requires Python (no `zip` CLI needed anymore).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$here/rebuild-schema-zip.py" "$@"
fi
exec python "$here/rebuild-schema-zip.py" "$@"
