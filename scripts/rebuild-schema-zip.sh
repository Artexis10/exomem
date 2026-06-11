#!/usr/bin/env bash
# Rebuild Knowledge Base/_Schema.zip from the canonical _Schema/ folder, so it can
# be re-uploaded as a skill to claude.ai when SKILL.md / references /
# project-keys.yaml change. Cross-platform counterpart to
# scripts/rebuild-schema-zip.ps1 (Windows).
#
# Resolves the vault from $KB_MCP_VAULT_PATH (the vault root that contains
# Knowledge Base/). Requires the `zip` CLI.
#
# Usage: KB_MCP_VAULT_PATH=/path/to/Obsidian bash scripts/rebuild-schema-zip.sh

set -euo pipefail

if [[ -z "${KB_MCP_VAULT_PATH:-}" ]]; then
    echo "KB_MCP_VAULT_PATH is not set. Point it at your vault root (the folder that contains Knowledge Base/)." >&2
    exit 1
fi

VAULT="$KB_MCP_VAULT_PATH"
SCHEMA_DIR="$VAULT/Knowledge Base/_Schema"
ZIP_PATH="$VAULT/Knowledge Base/_Schema.zip"

[[ -f "$SCHEMA_DIR/SKILL.md" ]] || { echo "KB_MCP_VAULT_PATH=$VAULT does not contain Knowledge Base/_Schema/SKILL.md" >&2; exit 1; }
command -v zip >/dev/null 2>&1 || { echo "the 'zip' CLI is required (macOS ships it; Linux: apt install zip)." >&2; exit 1; }

echo "vault:      $VAULT"
echo "schema dir: $SCHEMA_DIR"
echo "zip target: $ZIP_PATH"

# Surface the version from SKILL.md frontmatter so the operator sees what ships.
version="$(sed -n 's/^[[:space:]]*version:[[:space:]]*\([0-9.]*\).*/\1/p' "$SCHEMA_DIR/SKILL.md" | head -n1)"
if [[ -n "$version" ]]; then echo "version:    $version"; else echo "warning: could not parse version from SKILL.md frontmatter." >&2; fi

rm -f "$ZIP_PATH"

# zip from inside _Schema/ so SKILL.md lands at the archive root (claude.ai expects
# SKILL.md at the top level, not nested under _Schema/). -r recurse, -X drop extras.
( cd "$SCHEMA_DIR" && zip -r -X "$ZIP_PATH" . -x '.DS_Store' )

size_kb=$(( $(wc -c < "$ZIP_PATH") / 1024 ))
echo "wrote $ZIP_PATH (${size_kb} KB)"
