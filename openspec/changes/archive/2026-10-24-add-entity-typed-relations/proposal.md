# Proposal: add-entity-typed-relations

## Why

`connect_memory(operation="create-entity")` cannot state what an entity means
to another page. Its only edge input is `connections: list[str]`, and the
entity writer renders every item as `- relates_to [[Target]]`. A caller that
knows the entity supports, belongs to or depends on a target must either write
the generic edge or create the entity first and edit its `## Relations` section
afterwards. `create_note` already keeps an authored label, so the entity writer
is the one write path where a specific meaning cannot be expressed at all.

## What Changes

- `connect_memory` gains an optional `relations` list of `{kind, target}`
  objects for `create-entity`, the same item shape that `observe_memory` uses.
  Each pair is written as `- kind [[target]]` under `## Relations`.
- `connections` keeps its type, its comma-separated CLI form and its
  `relates_to` rendering. Both inputs can be used in one call.
- Targets resolve through the same writer resolver and visibility rules as
  `connections`, including forward references.
- Identical pairs collapse. Different kinds for one target stay distinct edges.
- A malformed pair, or a value that would not parse back as exactly one
  canonical relation bullet, refuses the create before any write.
- Whether the kind is registered and in scope stays with the existing creation
  preflight and its relation findings. This change adds no stricter label
  policy, classifier, quota or automatic retagging.
- `relations` on any other `connect_memory` operation is refused, so a caller
  never believes an ignored edge was written.
- The `connect_memory` wire ceiling rises by 150 bytes and the total tool
  budget by the same amount.

## Impact

- Code: `src/exomem/link.py`, `src/exomem/commands.py`.
- Generated: `tests/fixtures/mcp_tool_schemas.json`,
  `src/exomem/tool_surface_contract.json`, `docs/capabilities.md`, and the
  pending digest in `deploy/chatgpt/personal-plugin-contract.json`.
- No storage, graph schema or migration change. Existing entity pages and
  existing callers are unchanged.
