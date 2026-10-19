# add-vault-overview

## Why

When an agent is asked "what does this vault look like?" it has no cheap answer:
`audit` lints only `Knowledge Base/`, so the observed real-world behavior (a new
user's assistant assessing a pre-existing daily-notes vault) is a brute-force read
of every file — 168 `get`/Read calls for one structural question. The upcoming
guided-setup wizard needs the same "scan a vault before `init`" primitive, so this
lands first.

## What Changes

This artifact describes the current public surface. The eight unchecked tasks
remain open; this contract refresh does not establish their acceptance.

- Read-only **Tier 1** structural operation `overview`: a dependency-free vault-structure
  report — folder tree with per-folder counts, frontmatter coverage, dominant
  filename patterns, junk detection (zero-byte files, sync-conflict duplicates),
  largest/oldest files, and explicit truncation markers. The current public contract exposes `browse_memory(mode="overview")` through
  MCP, CLI and REST. Every door delegates to the same `op_overview` leaf.
- Core function `overview.overview(root)` works on **un-initialized vaults** (no
  `Knowledge Base/` required) so the setup wizard can scan pre-init; the registry
  leaf wraps it with the usual resolved `vault_root`.
- Output is **bounded by construction** (depth/breadth caps, per-file read cap,
  capped junk/sample lists with exact counts) — token-bounded on arbitrarily large
  vaults. Pure filesystem walk + regex; no new dependencies, no model — measurement
  only, in line with the pure-substrate constraint.
- Scaffold `SKILL.md`: `browse_memory` documented in the Tier 1 table, phrasing mappings
  ("what does this vault look like" → `browse_memory(mode="overview")`), and a short "Assessing or adopting a vault you
  didn't build" block (`browse_memory(mode="overview")` →
  `browse_memory(mode="list")` → `ask_memory(scope="vault")` →
  targeted `read_memory`; never bulk-read). Generic wording only (leak test gates).
- README core-tools table gains one row.
- MCP schema fidelity baseline regenerated (`tests/fixtures/mcp_tool_schemas.json`).

Default-on and lean-safe: no optional/heavy dependency is involved, so there is no
off-switch to document; the op degrades nowhere (it never touches embeddings or
media extraction).

## Capabilities

### New Capabilities
- `vault-overview`: bounded, read-only structural report of any vault subtree,
  reachable from MCP/CLI/REST and callable pre-init as a plain function.

### Modified Capabilities

(none — no existing spec's requirements change; `audit`/`list_directory` behavior
is untouched)

## Impact

- New module `src/exomem/overview.py`; registry row + `op_overview` leaf in
  `src/exomem/commands.py`.
- `src/exomem/_scaffold/_Schema/SKILL.md` (guidance), `README.md` (tools table).
- Tests: new `tests/test_overview.py`; CLI-door case; REST route covered by the
  registry-driven machinery; regenerated `tests/fixtures/mcp_tool_schemas.json`
  (via `scripts/dump-tool-schemas.py`) so `test_mcp_schema_fidelity` stays green;
  `test_scaffold_no_leak.py` guards the SKILL.md wording.
- No API/dependency changes elsewhere; `EXOMEM_DISABLE_TIER2=1` deployments keep
  the public `browse_memory` door (Tier 1 — the tool that prevents bulk reads must not be hideable).
