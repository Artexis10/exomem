## Why

Exomem's registry is now powerful enough to express the full governed-knowledge architecture, but the first-use MCP and CLI surface still asks users and agents to reason in tool names such as `propose_compilation`, `graph_context`, `suggest_relations`, and `audit_fix`. Basic Memory's simpler product surface shows the gap: Exomem needs an approachable front door without weakening the underlying governance model.

## What Changes

- Add a small set of product verbs for humans and generic agents: `ask`, `remember`, `capture`, `review`, `connect`, `adopt`, and `maintain`.
- Map those verbs deterministically onto existing registry commands instead of duplicating command logic.
- Expose the simple verbs through bootstrap metadata, docs, and CLI aliases.
- Keep the full registry available for advanced users, tests, REST, and MCP clients that already call canonical tools.
- Keep optional/heavy behavior default-off and soft-failing: graph enrichment remains explicit, model-backed relation suggestions remain opt-in, and the server continues to measure rather than reason.
- No breaking changes: existing command names, schemas, REST endpoints, and MCP tools remain valid.

## Capabilities

### New Capabilities
- `simple-command-surface`: A beginner-safe, product-oriented action layer that routes common knowledge intents to canonical Exomem operations.

### Modified Capabilities
- `command-surface`: The command registry exposes simple action metadata and CLI aliases while preserving canonical operations as the source of truth.
- `agent-bootstrap-contract`: Bootstrap and scaffold guidance present simple actions first, then disclose advanced tools when needed.

## Impact

- Affected code: `src/exomem/commands.py`, `src/exomem/command_surface.py`, `src/exomem/__main__.py`, CLI rendering helpers, bootstrap payloads, and scaffold docs.
- Affected tests: bootstrap contract tests, CLI tests, command-registry metadata tests, MCP schema fidelity tests if any MCP descriptions are intentionally changed.
- Affected docs: `docs/ai-assistant-guide.md`, `docs/capabilities.md`, `QUICKSTART.md`, and `_Schema` scaffold references.
- No new runtime dependency and no server-side reasoning model.
