## MODIFIED Requirements

### Requirement: Collection manifests are discoverable without raw-record flooding
Ordinary recall SHALL index and return only strictly valid collection manifests (`_collection.md`) under the exact `Knowledge Base/Records/**` and `Knowledge Base/Planning/**` layers. A centralized pure recall policy SHALL classify every other descendant—including `_summary.md`, raw item files, chronological logs, datasets, archives, generated roadmaps/exports, and templates—as structured-only without opening raw items. On-demand `record_memory` and `plan_memory` JSON, Markdown, and CSV responses are derived views, are never persisted or promoted automatically, and remain queryable under their governing authorization. Persisted summary recall is deferred until a governed materializer/attestation and complete source-authorization closure exist; a `source_snapshot` alone is not proof. Background semantic indexing SHALL enforce local access-tier exclusion before reading candidates but SHALL NOT perform audience-specific release authorization; the existing release plane applies the caller's projection before return. Structured-only material SHALL remain addressable by its profile-scoped reference and queryable through the corresponding product command. Raw CSV/TSV/JSON rows and Planning items SHALL remain structured-query data rather than semantic hits.

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

#### Scenario: Thousand Planning items do not flood ordinary recall
- **WHEN** a Planning collection contains one thousand candidate work-item Markdown files under its canonical path
- **THEN** ordinary `ask_memory` returns none of the repetitive raw items while the valid Planning manifest remains discoverable

#### Scenario: Explicit Planning query still reaches suppressed items
- **WHEN** a caller queries that collection through `plan_memory`
- **THEN** authorized matching items are returned within structured-query caps despite their exclusion from ordinary semantic recall

#### Scenario: Planning reference still resolves
- **WHEN** a caller supplies a canonical `exomem://plan/...` reference through the Planning product surface
- **THEN** collection-aware resolution can find the authorized item even though semantic candidate indexes omit it

#### Scenario: Planning collection outside exact layer refuses validation
- **WHEN** a `planning` manifest attempts to place itself or canonical sources outside exact `Knowledge Base/Planning/` path segments
- **THEN** collection validation refuses it rather than allowing raw work items to bypass the centralized structured-only policy

### Requirement: Suppression is consistent and reconcilable
The pure structured-only predicate SHALL have one owner in the shared corpus/path layer and SHALL classify both exact Records and Planning layers through explicit profile rules. It SHALL be reused consistently by current and incremental BM25/FTS, semantic units, embedding, graph, claims, filter-only, relation-derived candidates, auto-widen, warmup, move/delete, watcher, audit, reconciliation, and final find-candidate defenses. Index fanout SHALL distinguish identity/resolver updates for all Markdown from recall updates for eligible manifests and semantic-only purge for structured-only paths. A suppressed-path upsert SHALL first delete legacy lexical/page/unit, vector/unit, graph source/placeholder, claim, and deferred-semantic rows and then skip semantic insertion, including when embeddings are disabled; it SHALL NOT delete canonical files, stable memory/Record/Planning references, resolver state, inbound identity state, or agent audit history.

Manual edits, moves, deletions, or recall-policy changes SHALL remove stale derived rows and preserve collection-level discoverability. Reconciliation SHALL identify present-but-policy-suppressed rows, report and prune them component by component, and never route a live raw structured item through generic identity deletion. `maintain_memory(mode="reconcile", dry_run=false)` SHALL own derived-index repair; `record_memory(action="inspect")` and `plan_memory(action="inspect")` SHALL remain report-only.

#### Scenario: Reconcile removes stale indexed item
- **WHEN** a formerly indexed record page becomes structured-only
- **THEN** reconciliation removes its lexical, semantic-unit, vector, graph, claim, and deferred candidate rows without deleting the canonical Markdown file or stable identity state

#### Scenario: Single chronological log yields one page candidate
- **WHEN** an X3-style log contains hundreds of sessions
- **THEN** ordinary recall returns the collection manifest only, never raw session, movement, or derived-summary items from the canonical log

#### Scenario: Legacy tracker remains explicitly inspectable
- **WHEN** a manifest-less legacy tracker falls under the structured-only Records path
- **THEN** explicit Records inspection by its supplied path can still identify the tracker subject to governance even though ordinary semantic recall does not index its raw body or enumerate legacy trackers

#### Scenario: Embeddings-disabled cleanup still prunes vectors
- **WHEN** a raw Record path has legacy vector rows and embeddings are disabled
- **THEN** model-free semantic purge removes those rows before skipping insertion and does not require loading or invoking an embedding model

#### Scenario: Planning item moved from an indexed path is purged
- **WHEN** a formerly indexed Markdown page becomes a raw item beneath an exact Planning canonical source
- **THEN** incremental publication and reconciliation remove its lexical, unit, vector, graph, claim, and deferred candidates without deleting the Markdown file, Planning identity, or audit history

#### Scenario: Records behavior remains unchanged
- **WHEN** the policy owner gains Planning classification
- **THEN** existing Records manifests remain discoverable, raw Records remain suppressed, and `record_memory` queries and stable references remain available exactly as before

#### Scenario: Embeddings-disabled cleanup still prunes Planning vectors
- **WHEN** a raw Planning item has legacy vector rows and embeddings are disabled
- **THEN** model-free semantic purge removes those rows before skipping insertion and does not load or invoke an embedding model

### Requirement: Recall freshness is projected without weakening identity freshness
Generic `kb` and `vault` freshness SHALL remain authoritative for resolver, inbound-link, stable-reference, collection/item-reference, and other identity consumers. Recall consumers SHALL use projected freshness triples/checkpoints over recall-eligible Records and Planning manifests, and every persistent semantic sidecar SHALL bind its identity to the recall-policy version plus the local access-policy fingerprint so a policy change converges even without a file event. Raw-only Records or Planning item edits SHALL not churn recall freshness; manifest edits SHALL. Fresh structured query and inspection SHALL still observe raw direct edits through identity freshness.

#### Scenario: Raw edit preserves identity freshness and recall cache stability
- **WHEN** a user directly edits a structured-only Record item
- **THEN** resolver/reference freshness observes the edit, a fresh structured query sees it, and recall-projected freshness/cache identity does not change solely because of the raw content

#### Scenario: Policy-version change converges without file edit
- **WHEN** the recall policy version changes while canonical files remain unchanged
- **THEN** lexical, vector, graph, claim, and related semantic sidecars detect identity drift and rebuild or prune to the new policy

#### Scenario: Incremental sidecars advance through complete deltas
- **WHEN** multiple recall-eligible files change before one resolver or graph callback runs
- **THEN** the consumer applies the complete coalesced delta from its exact prior checkpoint before publishing the new checkpoint, and an incomplete or drifting delta leaves the sidecar unavailable or forces a safe rebuild rather than stamping unapplied state as current

#### Scenario: Raw Planning edit preserves recall cache stability
- **WHEN** a user directly changes a raw Planning item's priority, horizon, title, body, or status without changing a manifest
- **THEN** identity freshness and a fresh `plan_memory` query observe the edit while recall-projected freshness does not change solely because of raw content

#### Scenario: Planning manifest edit invalidates recall
- **WHEN** a valid Planning `_collection.md` title, body, lifecycle, or discoverable metadata changes
- **THEN** recall-projected freshness changes and subsequent ordinary recall uses the new manifest state
