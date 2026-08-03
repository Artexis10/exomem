## 1. Acceptance Fixtures And Red Contracts

- [x] 1.1 Add dedicated per-test X3 fixture files for the canonical newest-first log, adjacent declarative manifest, undated archive, Push/Pull templates, opaque Planning link/query descriptor, completed/partial/aborted sessions, duplicate legacy keys, explicit markers, special repetition notation, fenced decoys, CRLF, and no-final-newline variants.
- [x] 1.2 Add a one-Markdown-file-per-item vehicle-maintenance fixture with duplicate dates, arrays, amounts/currencies, nullable due values, Evidence links, mixed governance, and direct-edit correction cases.
- [x] 1.3 Add an unrelated query-only dataset fixture with declared keys, time/category/numeric fields, deterministic expected rows, high-cardinality values, and direct-edit pagination invalidation.
- [x] 1.4 Write failing manifest/discovery/schema tests for supported and future versions, collection-scoped identity, canonical natural-key serialization, bounded governed UUID lookup, withheld duplicates, symlink/path escape, unknown profiles, template independence, and manifest-less tracker inspection-only behavior.
- [x] 1.5 Write failing adapter tests for fence-aware exact-span parsing, insertion edge, explicit/inferred/ambiguous item keys, reorder/date correction, child-row expansion, byte preservation, and query-only dataset refusal.

## 2. Structured Collection Core

- [x] 2.1 Implement immutable collection, storage, schema, link/query-descriptor, item identity, source-version, and diagnostic models in a profile-neutral module.
- [x] 2.2 Implement strict human-readable `_collection.md` parsing and validation with version checks, canonical vault-relative path validation, bounded schemas, templates, saved views, and opaque Planning descriptor round-trip.
- [x] 2.3 Implement bounded symlink-safe manifest discovery and resolution by path, memory ref, or UUID, authorizing candidate paths before parsing identity-bearing content and detecting ambiguity only among releasable candidates.
- [x] 2.4 Implement canonical collection-scoped record references and natural-key serialization with declared field order, schema-version binding, Unicode NFC, typed JSON scalars, ISO date/datetime normalization, explicit nulls, and collision diagnostics.
- [x] 2.5 Implement bounded advisory schema inference that returns provenance and never writes a manifest or canonical item.
- [x] 2.6 Run the focused core tests and correct all contract failures before adapter work proceeds.

## 3. Storage Adapters And Shared Query Evaluation

- [x] 3.1 Implement the adapter protocol plus the declarative fence-aware Markdown-log adapter with exact byte spans, section/heading/row grammar, insertion direction, explicit markers, compatibility keys, and ambiguity reporting without domain conditionals.
- [x] 3.2 Implement the Markdown-item adapter over ordinary typed YAML properties and optional readable bodies, using `(collection_id, record_id)` identity and exact-file source versions.
- [x] 3.3 Refactor `query_data.py` pure row filtering, projection, deterministic sorting, aggregation, and bounded distinct/profile evaluation so dataset and Record adapters share one evaluator without changing existing query behavior.
- [x] 3.4 Implement the query-only CSV/TSV/JSON adapter with optional declared keys, source snapshots, deterministic pagination, hard row/cardinality/response caps, and explicit append/update refusal.
- [x] 3.5 Implement snapshot-bound continuation tokens and `json`, provenance-bearing `markdown`, and derived `csv` query output without writing or promoting exports.
- [x] 3.6 Fix the existing `limit=0` unbounded-tail behavior and add regression coverage for zero, negative, omitted, and excessive limits plus unbounded distinct/profile results.
- [x] 3.7 Run adapter and existing dataset-query tests, including the exact X3 and dataset fixture expectations.

## 4. Guarded Markdown Mutation And Agent Audit

- [x] 4.1 Write failing append/update tests for schema refusal, create-only collection creation, exact replay, identity conflict, missing/ambiguous targets, container/item staleness, same-vault serialization, separate-vault independence, and no fuzzy fallback.
- [x] 4.2 Implement `create`, Markdown-log/Markdown-item `append`, and targeted `update` as structured operations that re-read and re-resolve inside the existing writer boundary and publish through guarded planned writes.
- [x] 4.3 Preserve untouched bytes, UTF-8 BOM, CRLF/LF choice, whitespace, and final-newline state; add case-insensitive collision, open-file replacement failure, caught-error rollback, and unrelated-file preservation regressions.
- [x] 4.4 Separate exact-source item/container hashes from canonical-payload replay hashes; require both current container hash and item version for targeted update.
- [x] 4.5 Add the defined agent-mutation activity entry and bounded terminal receipt fields without copying canonical item values or conflating governance receipts, operational journals, and activity history.
- [x] 4.6 Detect and report canonical/audit gaps caused by direct edits or simulated abrupt partial publication while keeping canonical Records authoritative and repair explicit.
- [x] 4.7 Run mutation, writer-lease, transactional-write, idempotency, and Windows-path focused tests.

## 5. Governance Before Reduction

- [x] 5.1 Write failing governance tests for L0–L6 disclosure (full Records require L6), withheld manifests, authorized-first UUID discovery with separate raw/public caps, file-per-item mixed release decisions, hidden malformed/cap-consuming items, hidden-only continuation stability, whole-log/dataset granularity, withheld values excluded before totals/latest/distinct/profile/pagination, safe conflict shapes, templates, Planning descriptors, and receipts.
- [x] 5.2 Implement Records authorization and egress projection so manifest/source/template paths are governed before parse/return and an immediate adapter callback authorizes each file-per-item candidate before public counts, caps, ordering, parsing, snapshots, diagnostics, pagination, or reduction.
- [x] 5.3 Implement default-deny typed Records envelopes and recursively project schema-declared links, Planning descriptors, templates, provenance, paths, identities, hashes, audit/conflict/continuation/count fields before reduction or rendering; do not release arbitrary nested `rows` through a top-level allowlist.
- [x] 5.4 Prove aggregates cannot reveal withheld rows, document that one log/dataset is an all-or-nothing governance artifact, and refuse mixed-release mutation when the caller cannot receive the complete canonical CAS snapshot.
- [x] 5.5 Run focused governance, excluded-tier, graph/reference release, receipt, and Records reduction tests.

## 6. Retrieval Isolation And Manual-Edit Repair

- [x] 6.1 Write failing high-cardinality tests proving one thousand raw Record item files and canonical log rows produce no ordinary recall flood; only strictly valid exact `_collection.md` manifests are discoverable; `_summary.md`, malformed/oversized/aliased descendants, stale derived views, and all other descendants remain structured-only; Records outside the exact layer refuse; and on-demand structured queries remain complete within caps.
- [x] 6.2 Implement one shared pure corpus policy for `Knowledge Base/Records/**`, plus recall-projected freshness and identity (static policy version plus local access-policy fingerprint), and reuse it across current/incremental lexical, semantic-unit, vector, graph, claim, filter-only, relation, auto-widen, warmup, watcher, move/delete, audit, and final-candidate paths without suppressing stable collection-aware resolution. Defer persisted summary recall pending governed materialization/attestation and complete source authorization.
- [x] 6.3 Split identity upsert, recall upsert, and model-free semantic-only purge; extend maintenance reconciliation to remove policy-stale lexical/unit/vector/graph/claim/deferred rows after create, edit, move, delete, or policy change even with embeddings disabled, without deleting canonical or identity state; keep `record_memory(action="inspect")` report-only and all `record_memory` JSON/Markdown/CSV responses non-persistent.
- [x] 6.4 Prove fresh Records queries immediately observe direct Markdown and dataset edits, snapshot continuations refuse after drift, derived responses are never persisted or promoted, and canonical files are never rewritten by inspection or maintenance.
- [x] 6.5 Run recall, find, index-sync, graph, watcher, reconciliation, memory-ref, and Records scale tests with embeddings disabled, including parity across every ingress and final-candidate defense.

## 7. One Generated Product Command

- [x] 7.1 Write failing registry, selector, argument-matrix, governance-projector, retry, MCP schema, REST, CLI, and bootstrap tests for exactly `inspect`, `create`, `query`, `append`, and `update`.
- [x] 7.2 Implement `record_memory` once in the canonical registry, with `inspect`/`query` lease-free, `create`/`append`/`update` writer-routed, unknown actions fail-closed, and conservative command-level write annotations.
- [x] 7.3 Implement report-only collection inspection and queryable agent-audit fields; leave generic derived-index repair under `maintain_memory(mode="reconcile")`.
- [x] 7.4 Regenerate shared MCP schema fixtures and verify MCP/REST/CLI/bootstrap parameter and response parity without adding storage-specific tools.
- [x] 7.5 Run focused command-surface, schema-fidelity, REST, CLI, bootstrap, retry, lease, and governance tests.

## 8. Templates, Packs, Scaffold, And Product Documentation

- [x] 8.1 Update existing health and personal-records pack guidance through the current validated pack surface; do not add blueprint activation, silent folders, migrations, or domain-specific storage behavior.
- [x] 8.2 Update the hand-authored generic scaffold, bootstrap guidance, README, capability/product-model docs, and new Records documentation with the eight-layer distinction, manual-first invariant, five actions, storage/granularity limits, tracker adoption, template independence, agent audit gaps, non-persistent derived-output provenance, deferred summary materialization, and Planning/OpenSpec boundaries.
- [x] 8.3 Document `Knowledge Base/Templates/` as the ordinary template root, verify that the Knowledge Base vault's `.obsidian/templates.json` maps `Templates` to that path, and record that Exomem preserves rather than mutates `.obsidian`.
- [x] 8.4 Document compatibility and rollback: manifest-less inspection only, adjacent-manifest opt-in, prospective markers, no forced tracker rewrite, query-only datasets, no automatic representation promotion, and rebuildable derived state.
- [x] 8.5 Run pack, bootstrap, docs/schema generation, scaffold integrity, and no-personal-leak tests.

## 9. Real Product Path, Review, And Delivery

- [x] 9.1 Add an end-to-end dispatcher test that preserves the X3 files/templates, simulates ordinary template insertion, performs guarded append, targeted update, structured query with non-persistent JSON/Markdown/CSV derived views, opaque Planning descriptor round-trip, neutral three-month rendering, governance enforcement, and direct-manual-edit visibility without recall flooding or persisted-summary admission.
- [x] 9.2 Add end-to-end vehicle and dataset paths proving targeted correction, evidence links, mixed-release pre-reduction filtering, dataset caps, snapshot invalidation, and unsupported dataset mutation refusal.
- [ ] 9.3 Run strict OpenSpec validation, focused Records suites, repository full tests with embeddings disabled, Ruff, mypy, generated-surface checks, scaffold leak checks, and proportional latency/scale gates; record exact results.
- [ ] 9.4 Run an independent architecture/code/security review focused on Planning overlap, duplicate substrate, exact-span writes, concurrency, manual edits, identity/schema evolution, tracker compatibility, governance/aggregate leakage, recall pollution, source-of-truth promotion, template coupling, hidden DB dependence, stale/ambiguous updates, X3 leakage, migration claims, and Windows publication.
- [ ] 9.5 Correct every material reviewer finding and rerun the affected plus full verification rather than merely documenting failures.
- [ ] 9.6 Run an independent end-to-end verifier through generated public surfaces and real fixture files, then record X3, vehicle, dataset, Planning-link, governance, retrieval, and manual-edit evidence.
- [ ] 9.7 Capture the durable architecture decision, delivered behavior, compatibility boundary, verification evidence, remaining deferred work, and any discovered failure modes back into Exomem with links to the earlier Planning and Records notes.
- [ ] 9.8 Commit only intended scope, integrate current `origin/main` in the feature checkout, rerun affected/full verification, push the feature branch, and open a ready pull request without merging it.
