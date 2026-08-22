## Why

Exomem can now preserve observed state as first-class Records, but it still lacks a durable product layer for future intent: goals, desired outcomes, priorities, initiatives, encountered bugs, feature candidates, and work sequenced across weeks through years. The shared structured-collection substrate is ready, so Planning can be added without creating another database or duplicating Records mechanics.

## What Changes

- Add first-class Planning collections under `Knowledge Base/Planning/`, with ordinary Markdown manifests and one human-editable Markdown file per planning item as the first canonical storage shape.
- Add one finite `plan_memory` product command with `inspect`, `create`, `query`, `add`, `update`, and `triage` actions across MCP, REST, and CLI.
- Define the focused Planning model: ongoing areas, desired outcomes, bounded initiatives, and concrete work items; coherent priority, commitment, lifecycle, health, authored planning horizon, optional date window, parent/area links, and stable collection-scoped identities.
- Provide deterministic inbox, week, month, quarter, year, and multi-year queries plus bounded hierarchy renderings. These are derived views over canonical files, not a second source of truth.
- Preserve thin external-execution pointers for OpenSpec changes, repositories, issues, pull requests, releases, and deployments without copying requirements, task state, tests, or code into Exomem.
- Let plans carry opaque bounded Records collection/saved-view pointers without resolving them, inferring progress, or performing planned-versus-recorded Review in this change.
- Reuse the shipped collection schema, Markdown-item adapter, query evaluator, guarded mutation, same-vault serialization, audit/history, direct-edit inspection, governance, and publication machinery behind a profile-neutral internal boundary while retaining existing Records behavior and references.
- Keep raw Planning items out of ordinary semantic recall while leaving collection manifests discoverable and explicit structured Planning queries complete within bounds.
- Teach bootstrap, the generic skill scaffold, knowledge packs, and product documentation to route simple planning intents without requiring users to understand collection internals.
- Prove the public product path with both software planning and a materially different non-software multi-horizon fixture.
- Defer planned-versus-recorded Review, inline Records query evidence, dependencies, automatic progress or success judgments, capacity optimization, automatic repository reconciliation, persisted dashboards, forms/charts, and a dedicated Planning TUI.

## Capabilities

### New Capabilities

- `planning`: Planning semantics, lifecycle, hierarchy, horizons, evidence and execution pointers, guarded operations, derived views, compatibility, and product acceptance.

### Modified Capabilities

- `structured-collections`: Apply the shipped collection mechanics to the Planning profile, add Planning placement and stable-reference rules, and keep Records compatibility intact.
- `command-surface`: Add the finite generated `plan_memory` command with selector-aware read/write classification and parity across MCP, REST, and CLI.
- `agent-bootstrap-contract`: Route goals, bugs, feature candidates, initiatives, and multi-horizon questions into Planning while preserving the Planning/Records/OpenSpec boundary.
- `governance-kernel`: Authorize Planning manifests and items before parsing, ambiguity, hierarchy, counts, grouping, and rendering, and project responses through typed default-deny envelopes.
- `find-recall-efficiency`: Make Planning manifests discoverable while consistently excluding raw Planning items and generated views from ordinary semantic recall.
- `product-e2e`: Extend the installed-wheel public journey with Planning creation, capture, triage, guarded update, multi-horizon query, direct-edit visibility, Records/OpenSpec links, governance, and restart persistence.

## Impact

- New Planning service and `plan_memory` command modules, plus profile-neutral extraction or adapters behind the existing Records compatibility facades.
- Canonical command registry, selector classification, writer lease, idempotency, terminal receipts, governance projectors, MCP schema fixture, REST/CLI generation, and capability documentation.
- Structured-collection validation, audit metadata, Planning item references, recall policy, freshness/reconciliation, bootstrap, generic scaffold, knowledge packs, and user documentation.
- New software and non-software Planning fixtures, focused unit/integration/governance/recall tests, and an installed-wheel product E2E extension.
- No new database, reasoning model, optional heavy dependency, Obsidian plugin dependency, or breaking migration. Existing Records manifests, `record_audit`, `exomem://record/...` references, commands, and canonical files remain compatible.
