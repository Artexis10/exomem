## Why

Agents need SQL-grade analysis of documents and multi-million-row collections, linked to a governed epistemic graph, without exposing SQL to users. A wearable fitness export of roughly 244 MB cannot be ingested through the current file engine. Moving collections to SQLite solves write scaling but does not supply compact typed storage, streaming import, agent query composition or useful query-derived context on every turn.

Together with its graph sibling, this change is what separates Exomem from a simpler agent memory. With both delivered, one embedded store offers:

- governance that applies to every row and every graph hop;
- epistemic relations that carry provenance, so the store can say why a fact is present;
- SQL-grade structured data at the scale of a wearable export;
- a first-class ontology that users and agents can extend;
- a local-first, zero-operations store with an engine-agnostic path to hosted scale;
- a context compiler that assembles a governed working set on every turn.

The claim holds only if the foundations ship first: reliable service on every client, sub-second activation and simple onboarding. Until then, these capabilities remain gated behind those release criteria, not marketed ahead of them.

## What Changes

- Preserve the round-one structured query, legacy parity, declared indexes, keyset pages, grouped aggregates, exact percentile, declared joins and optional FTS5 contracts.
- Add an engine-neutral structured query IR with one admission path for documents, collection rows and graph references. SQLite remains the only backend delivered here; future hosted engines compile the same governed IR.
- Make agents first-class consumers through compose, explain, cost preview, dry-run, execution, incremental refinement and saved views on existing MCP tools, with repairable errors and on-demand skill/bootstrap guidance.
- Add migration-managed canonical typed-column encoding for large declared collections, a resumable streaming CSV/JSON importer, optional bulky-text compression, timestamp/type indexes and exact incremental daily/weekly rollups.
- Add query-derived working-set units selected from declared surfacing, with source/window/count provenance, exact freshness and a shared 30 ms query-stage budget inside the sub-second activation budget.
- Measure storage, RAM, ingestion, query latency, activation gains and real-MCP agent task success. A backend-only capability without an agent path is not delivered.
- Split recursive graph traversal, paths/patterns, ontology projection and graph-store consolidation into sibling [`add-graph-traversal-queries`](../add-graph-traversal-queries/proposal.md). It extends this IR and shared executor; this change supplies its query/admission interfaces. Collection features can ship first; graph-dependent promises wait for the sibling gates.

## Capabilities

### New Capabilities

- `collection-query-engine`: governed structured query IR, compact typed storage, index/query lifecycle, streaming ingestion, rollups, agent surfaces, saved views, context units and bounded performance.

### Modified Capabilities

None. The opt-in versioned contract supplements existing collection/dataset requests. A forward store encoding migration preserves their logical items, versions, hashes, audit, governed edit-back and portability contracts; implementation must reconcile any affected physical DDL with the parent before rollout.

## Impact

- Depends on `move-structured-collections-to-sqlite` P1a for store/registry/governance/parity. Q1–Q4 can proceed alongside P2–P4; declaration publication and generic surfacing wait for parent P4/P5. The owner's round-two direction explicitly adds the query-derived part of the formerly deferred §14.4 compiler work here; pinned historical links remain deferred.
- Implementation affects the collection schema/encoding and generic query leaf, importer, rollup maintenance, Records/Planning facades, document index adapters, shared tool schema generation, type/view validation, context compiler and generic skill/bootstrap guidance. This lane changes OpenSpec artifacts only.
- One store per vault, no raw SQL or server-side reasoning model, governance before reductions, and per-operator Python/SQL parity remain mandatory. Knowledge stays Markdown; document and graph rows remain rebuildable projections. Human-owned datasets remain file-canonical and query-only until explicitly imported into an authorized collection.
- Existing/frozen hosted candidates keep byte-identical contracts; new capability is published on local and non-frozen adapters. Tool-schema diet (#1458), bootstrap core ceiling (#1464) and installed-wheel/privacy gates bind both changes.
- Preview remains default-off. All numerical targets are proposed release gates, not measured achievements; design open questions retain round-one recommendations except where this direction changes them.
