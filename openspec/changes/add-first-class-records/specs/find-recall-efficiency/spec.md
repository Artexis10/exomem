## ADDED Requirements

### Requirement: Collection manifests are discoverable without raw-record flooding
Ordinary recall SHALL index and return only strictly valid collection manifests (`_collection.md`) under the exact `Knowledge Base/Records/**` layer. A centralized pure recall policy SHALL classify every other descendant—including `_summary.md`, raw item files, chronological logs, datasets, archives, and templates—as structured-only without opening raw items. On-demand `record_memory` JSON, Markdown, and CSV responses are derived views, are never persisted or promoted automatically, and remain queryable under their governing authorization. Persisted summary recall is deferred until a governed materializer/attestation and complete source-authorization closure exist; a `source_snapshot` alone is not proof. Background semantic indexing SHALL enforce local access-tier exclusion before reading candidates but SHALL NOT perform audience-specific release authorization; the existing release plane applies the caller's projection before return. Structured-only material SHALL remain addressable by collection-scoped reference and queryable through `record_memory`. Raw CSV/TSV/JSON rows SHALL remain structured-query data rather than semantic hits.

#### Scenario: Thousand record files do not flood ordinary recall
- **WHEN** a collection contains one thousand Markdown item files under its Records canonical path
- **THEN** ordinary `ask_memory` returns no repetitive raw item hits while the collection manifest remains discoverable

#### Scenario: Explicit structured query still reaches suppressed items
- **WHEN** a caller queries that collection through `record_memory`
- **THEN** authorized matching items are returned within structured-query caps despite their exclusion from ordinary semantic recall

#### Scenario: Stable reference still resolves
- **WHEN** a caller supplies the stable identity of a structured-only Markdown item
- **THEN** direct collection-aware resolution can find it subject to governance even though semantic candidate indexes omit it

#### Scenario: Persisted summaries remain structured-only
- **WHEN** a `_summary.md` or other derived view exists beneath the Records layer
- **THEN** ordinary recall suppresses it; only an authorized on-demand `record_memory` response may return derived JSON, Markdown, or CSV, without persisting or promoting that response

#### Scenario: Records collection outside exact layer refuses validation
- **WHEN** a `records` manifest attempts to place itself or canonical sources outside exact `Knowledge Base/Records/` path segments
- **THEN** collection validation refuses it rather than allowing raw Record pages to bypass the centralized structured-only policy

### Requirement: Suppression is consistent and reconcilable
The pure structured-only predicate SHALL have one owner in the shared corpus/path layer and SHALL be reused consistently by current and incremental BM25/FTS, semantic units, embedding, graph, claims, filter-only, relation-derived candidates, auto-widen, warmup, move/delete, watcher, audit, reconciliation, and final find-candidate defenses. Index fanout SHALL distinguish identity/resolver updates for all Markdown from recall updates for eligible manifests and semantic-only purge for structured-only Record paths. A suppressed-path upsert SHALL first delete legacy lexical/page/unit, vector/unit, graph source/placeholder, claim, and deferred-semantic rows and then skip semantic insertion, including when embeddings are disabled; it SHALL NOT delete canonical files, stable memory/record references, resolver state, or inbound identity state.

Manual edits, moves, deletions, or recall-policy changes SHALL remove stale derived rows and preserve collection-level discoverability. Reconciliation SHALL identify present-but-policy-suppressed rows, report and prune them component by component, and never route a live raw Record through a generic identity deletion. `maintain_memory(mode="reconcile", dry_run=false)` SHALL own derived-index repair; `record_memory(action="inspect")` SHALL remain report-only.

#### Scenario: Reconcile removes stale indexed item
- **WHEN** a formerly indexed record page becomes structured-only
- **THEN** reconciliation removes its lexical, semantic-unit, vector, graph, claim, and deferred candidate rows without deleting the canonical Markdown file or stable identity state

#### Scenario: Single chronological log yields one page candidate
- **WHEN** an X3-style log contains hundreds of sessions
- **THEN** ordinary recall returns the collection manifest only, never raw session, movement, or derived-summary items from the canonical log

#### Scenario: Legacy tracker remains explicitly inspectable
- **WHEN** a manifest-less legacy tracker falls under the structured-only Records path
- **THEN** explicit Records discovery/inspection can still identify the tracker subject to governance even though ordinary semantic recall does not index its raw body

#### Scenario: Embeddings-disabled cleanup still prunes vectors
- **WHEN** a raw Record path has legacy vector rows and embeddings are disabled
- **THEN** model-free semantic purge removes those rows before skipping insertion and does not require loading or invoking an embedding model

### Requirement: Recall freshness is projected without weakening identity freshness
Generic `kb` and `vault` freshness SHALL remain authoritative for resolver, inbound-link, stable-reference, and other identity consumers. Recall consumers SHALL use projected freshness triples/checkpoints over recall-eligible manifests, and every persistent semantic sidecar SHALL bind its identity to the recall-policy version plus the local access-policy fingerprint so a policy change converges even without a file event. Raw-only Record edits SHALL not churn recall freshness; manifest edits SHALL.

#### Scenario: Raw edit preserves identity freshness and recall cache stability
- **WHEN** a user directly edits a structured-only Record item
- **THEN** resolver/reference freshness observes the edit, a fresh structured query sees it, and recall-projected freshness/cache identity does not change solely because of the raw content

#### Scenario: Policy-version change converges without file edit
- **WHEN** the recall policy version changes while canonical files remain unchanged
- **THEN** lexical, vector, graph, claim, and related semantic sidecars detect identity drift and rebuild or prune to the new policy

### Requirement: Record query scale is explicitly bounded
Structured Records query SHALL apply file-size, parsed-item, returned-row, aggregate-cardinality, and response-size bounds with pagination/snapshot metadata. Optional derived caching SHALL be soft-fail and rebuildable; no heavy resident database or model SHALL be required for correctness.

#### Scenario: Oversized collection refuses or pages safely
- **WHEN** a collection exceeds a documented parse or response bound
- **THEN** the query refuses with actionable split/index guidance or returns a bounded page, never an unbounded dump or partial answer presented as complete

#### Scenario: Optional cache failure preserves correctness
- **WHEN** a derived collection cache is absent, stale, corrupt, or disabled
- **THEN** the bounded canonical-file path remains correct or the operation refuses explicitly without promoting cache state to truth
