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
- Add source-recorded local-day buckets (UTC plus each record's offset, or its supplied calendar date; missing time basis is flagged), lossless numeric/history encoding and continuing import authority. Add migration-managed canonical typed-column encoding for large declared collections, a resumable streaming CSV/JSON importer, optional bulky-text compression, timestamp/type indexes and exact incremental daily/weekly rollups.
- Add query-derived working-set units selected from declared surfacing, with source/window/count provenance, exact freshness and a shared 30 ms query-stage budget inside the sub-second activation budget.
- Measure storage, RAM, ingestion, query latency, activation gains and real-MCP agent task success. A backend-only capability without an agent path is not delivered.
- Split recursive graph traversal, paths/patterns, ontology projection and graph-store consolidation into sibling [`add-graph-traversal-queries`](../add-graph-traversal-queries/proposal.md). It extends this IR and shared executor; this change supplies its query/admission interfaces. Collection features can ship first; graph-dependent promises wait for the sibling gates.

## Capabilities

### New Capabilities

- `collection-query-engine`: governed structured query IR, compact typed storage, index/query lifecycle, streaming ingestion, rollups, agent surfaces, saved views, context units and bounded performance.

### Modified Capabilities

- `structured-collections`: amend capacity/view/edit-back, lossless typed/history encoding and routing/snapshot/adoption. Store-routed Records/Planning use SQLite and generated Markdown; existing file collections retain file authority until proven migration. A single durable marker permits NEW store summary collections alongside legacy files. Items retains the parent 100,000-row contract; summary has bounded pages/store capacity; populated mode conversion is refused in v1, with explicit copy/import to a separate collection. File-canonical datasets remain unchanged.

## Impact

- First deliver preserved source → preview → NEW built-in Records summary collection → streaming typed import → governed query/daily rollup on the owner's real vault, through existing MCP tools and the separate S1 gate. No preview vault and no migration of existing Records. Parent P1a writer/registry/parity plus the assigned P1b.5–7 snapshot/replica/head/lineage/portability subset and schema/state/coordinator fences are mandatory before create/access. Location-field admission and separate owner-only raw-source release protection precede external reads, including without configured policy; the external placement contract is unchanged. A pre-GA schema change may require re-import from the preserved source. General migration of existing collections still requires parent P1b/P2/P3; arbitrary type publication and generic surfacing wait for P4/P5. The owner's round-two direction explicitly adds the query-derived part of the formerly deferred §14.4 compiler work here; pinned historical links remain deferred.
- Implementation affects the collection schema/encoding and generic query leaf, importer, rollup maintenance, Records/Planning facades, document index adapters, shared tool schema generation, type/view validation, context compiler and generic skill/bootstrap guidance. This lane changes OpenSpec artifacts only.
- One store per vault, no raw SQL or server-side reasoning model, governance before reductions, and per-operator Python/SQL parity remain mandatory. Knowledge stays Markdown; document and graph rows remain rebuildable projections. Human-owned datasets remain file-canonical and query-only until explicitly imported into an authorized collection.
- Existing/frozen hosted candidates keep byte-identical contracts; new capability is published on local and non-frozen adapters. Tool-schema diet (#1458), bootstrap core ceiling (#1464) and installed-wheel/privacy gates bind both changes.
- Capabilities remain default-off until their gates pass. The owner and orchestrator rulings dated 2026-09-30 settle all fourteen CQ questions; performance targets remain provisional until measured. The first slice is independent of joins, FTS, ontology, graph and generic activation.
