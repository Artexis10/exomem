## Why

Exomem can preserve knowledge and query raw datasets, but it has no first-class way to own continuously changing facts such as sessions, measurements, transactions, maintenance events, or current state. Planning now has a durable product boundary for intended future state; Records is the complementary observed-state layer, and both need one reusable structured-collection substrate rather than parallel identity, schema, mutation, query, history, view, and reconciliation stacks.

## What Changes

- Add a storage-neutral structured-collection foundation with stable collection-scoped item identities, typed schemas, declared canonical storage, guarded Markdown mutation, bounded query, agent-mutation audit history, provenance-bearing query views, and direct-edit visibility.
- Add Records as a distinct semantic profile for observed facts, events, measurements, transactions, sessions, and state transitions. Records remain separate from Sources, Evidence, compiled Notes, Entities, Planning intent, Review, and Imported staging.
- Support three human-owned canonical representations: mutable chronological Markdown logs, mutable one-file-per-item Markdown, and query-only CSV/TSV/JSON datasets. Derived indexes and rendered query views remain rebuildable and never replace those files silently.
- Add one multiplexed `record_memory` product command with five actions—`inspect`, `create`, `query`, `append`, and `update`—and expose it consistently through MCP, REST, CLI, and bootstrap. Existing `maintain_memory` remains the owner of derived-index repair.
- Preserve `type: tracker` and existing manual/Obsidian workflows. A manifest-less tracker remains directly inspectable at collection level when its path is supplied; item query or mutation requires an explicit adjacent manifest with a complete adapter descriptor. Stable IDs are introduced prospectively without rewriting history.
- Let manifests reference ordinary Markdown templates and update existing knowledge-pack guidance without making templates hidden schema truth, adding silent pack activation, or adding an Obsidian runtime dependency.
- Keep raw high-volume items and all non-manifest descendants out of ordinary semantic recall while making strictly valid collection manifests discoverable and records accessible through explicit structured queries. On-demand JSON/Markdown/CSV outputs are derived, never persisted or promoted automatically; persisted summary recall is deferred pending governed materialization and source-authorization closure.
- Apply governance before every record reduction or aggregate, serialize same-vault agent mutations through the existing writer boundary, and return stale/ambiguous refusals plus auditable mutation receipts.
- Prove the abstraction with the current X3 newest-first training log, an unrelated one-file-per-item vehicle-maintenance collection, and a query-only dataset fixture. Round-trip an opaque Planning reference plus bounded Records query descriptor without resolving Planning or comparing planned and recorded state.
- Defer the complete Planning product, dashboard/calendar/board UI, general spreadsheet/database behavior, advanced analytics, task execution, and automatic interpretation of record meaning.

## Capabilities

### New Capabilities

- `structured-collections`: Human-owned collection manifests, schemas, collection-scoped identities, storage adapters, guarded Markdown mutations, bounded queries, agent-mutation audit metadata, provenance-bearing output, and direct-edit checks reusable by Records and future Planning.
- `records`: Observed-state semantics, tracker compatibility, templates and pack guidance, storage-shape behavior, X3 and cross-domain acceptance, and an opaque Planning-reference contract.

### Modified Capabilities

- `command-surface`: Add one consistently generated Records front door without exposing a family of storage-specific tools.
- `governance-kernel`: Gate releasable manifests and record items before pagination totals, aggregates, profiles, and derived observed-state reductions, with path/item provenance preserved through egress.
- `find-recall-efficiency`: Discover strictly valid collection manifests while preventing repetitive raw or derived Record items from flooding ordinary recall.
- `agent-bootstrap-contract`: Teach the Records boundary, natural capture/query routing, template behavior, and Planning-versus-Records distinction.

## Impact

- New collection/record parsing, validation, Markdown mutation, query, inspection, and audit modules under `src/exomem/`, reusing `vault`, `writer_lease`, reference, query, governance, freshness, maintenance, and command-registry primitives.
- One new generated product command across MCP, REST, and CLI plus its governance/projector and schema-fidelity coverage.
- Knowledge-pack schema/data, generic scaffold guidance, and public documentation updates; no personal or X3-specific material ships under `src/exomem/`.
- Focused fixtures and integration tests for chronological Markdown, file-per-item Markdown, query-only datasets, tracker compatibility, templates, stale/ambiguous writes, manual edits, governance reductions, recall pollution, opaque Planning links, partial audit-history publication, and Windows-safe atomic publication.
- No new required runtime service, opaque canonical database, resident model, Obsidian plugin, or third-party planning dependency.
