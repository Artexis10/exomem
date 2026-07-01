## Why

exomem exposes its operations across three surfaces — **MCP tools** (24, for Claude), a personal **REST facade** (9 hand-wired `/api/*` routes), and a **CLI** (admin-only). They share leaf functions (no business-logic duplication — good), but the *surface definitions* are duplicated and inconsistent:

- Exposing one operation takes **3–4 edits in different spots** (`@mcp.tool` decorator, a `@mcp.custom_route` block, the `post_tools` OpenAPI list, optionally a CLI handler).
- **REST covers only 9** of the operations; the rest (replace, link, preserve, provenance_report, query_data…) have no HTTP endpoint.
- The **OpenAPI spec is a hand-maintained list** (`post_tools`, server.py:1031) with generic `{type: object}` schemas — it drifts and documents nothing useful.
- The **CLI can't run the core operations at all** — no `find`, `get`, `audit` from a terminal/script.

engraph and Endstate each have **one clean, consistent command contract**. exomem should too — **one declarative registry as the genuine single source of truth** that drives *every* surface (MCP, REST, CLI, OpenAPI), so adding an operation is one entry and the surfaces can't drift.

## What Changes

- New **`commands.py` registry** — one declarative source of truth. Each operation = `{name, leaf_fn, description, params, surfaces, tier}`, carrying the **full tool description** Claude reads.
- **MCP tools are generated from the registry** via a **`bind_vault(leaf)` helper** that presents each leaf's real signature (minus the injected `vault_root`) + its registry description to FastMCP. A **byte-identical schema-fidelity snapshot test** asserts the generated tools' input-schemas + descriptions match today's exactly — so Claude's tool view provably cannot regress. Any tool whose generated schema can't match cleanly (e.g. the wide `note`) **stays hand-registered as an explicit, listed exception**.
- **REST routes + OpenAPI derive from the registry** (replaces the hand-wired `/api/*` blocks + the `post_tools` list): every op marked `rest` gets `/api/<name>`; OpenAPI gains real per-parameter schemas. Existing route names/behavior preserved.
- **A real CLI over the operations** (reads AND writes), generated from the registry, with **Endstate-style ergonomics**: verb-first `python -m kb_mcp find "query"`, global `--json` envelope, structured error **codes + remediation**, exit codes (0/1/2). `note`'s wide signature gets a `--field k=v` escape for its type-specific args. CLI is local-only.
- **Shared result/error envelope** (`{success, data, error: {code, message, remediation}}`) for CLI `--json` and REST.

Out of scope (future): CLI/REST **auth** changes (CLI stays local-only; REST keeps its API key); streaming; merging the SKILL/scaffold tool docs (handled separately).

## Capabilities

### New Capabilities
- `command-surface`: one operation registry that generates the MCP tools, the REST facade, the OpenAPI spec, and the CLI from a single declaration — a genuine single contract across all surfaces, with a schema-fidelity guard so the migration can't change what Claude sees.

## Impact

- Code: new `src/kb_mcp/commands.py` (registry + `bind_vault`) + `src/kb_mcp/cli_ops.py` (CLI arg→kwargs + envelope); `server.py` MCP registration + REST section refactored to loop the registry (hand-registered exceptions remain for non-matching tools); `__main__.py` gains registry-driven core-op subcommands.
- Behavior: **MCP tool schemas + descriptions are byte-identical** (snapshot-pinned) — zero change to what Claude sees. REST **expands** from 9 to all registered ops; existing 9 keep names + leaf calls; REST + CLI `--json` adopt the shared envelope (personal facade — low blast radius). CLI gains all core ops (reads + writes).
- No new dependencies (signature binding via stdlib `inspect`).
