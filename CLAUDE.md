# kb-mcp — instructions for Claude

## Editing the skill — BUMP THE VERSION (this keeps getting missed)

The knowledge-base skill's canonical source is the **vault** `_Schema/`
(`$KB_MCP_VAULT_PATH/Knowledge Base/_Schema/`), **not** the repo. The repo's
`src/kb_mcp/_scaffold/_Schema/` (generic, for friends) and the claude.ai
`_Schema.zip` (personal) are **derived** from it.

Whenever you change skill content (SKILL.md or any `references/*.md`), in the SAME change:

1. **Bump `version:` in the canonical SKILL.md frontmatter** (semver). A content
   edit without a version bump is a bug — do not skip it.
2. **Re-derive both surfaces:**
   - `python scripts/genericize-schema.py --vault <root>` → regenerates the repo
     scaffold (generic, leak-guarded). Commit the result.
   - `python scripts/rebuild-schema-zip.py --vault <root>` → rebuilds the vault
     `_Schema.zip` (real content, markers stripped). Hugo re-uploads it to claude.ai.
3. **Never hand-edit `src/kb_mcp/_scaffold/_Schema/`** — it's generated (see CONTRIBUTING.md).

## Connector triage ("MCP not working" / forced reconnect)

claude.ai connector problems are almost always **connection-side, not the service**.
A healthy service returns a fast `401` at the funnel. The most common cause is the
**Tailscale Funnel relay throttling the connector's request burst** — the connector
looks disconnected but the kb-mcp service is RUNNING and fine. **Diagnose from the
access log before touching the server** (look for the claude.ai gateway IPs); don't
restart the service reflexively. Full triage table: KB note "kb-mcp connector triage
— read the access log before blaming the server".
