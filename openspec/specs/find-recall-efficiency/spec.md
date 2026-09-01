# find-recall-efficiency Specification

## Purpose
Keep `find` fast and token-efficient as the vault grows and while sidecars are
being written: opt-in per-lane timing diagnostics, compact and full result
surfaces, a freshness-invalidated hot result cache, a process-lifetime
incrementally-patched vector matrix cache over WAL sidecars, per-request freshness
snapshots with delete/rename/backdated-aware corpus keys, per-page derived-text
reuse, and startup cache warm-up. Measurement and caching only — any
retrieval-architecture rewrite (ANN/LSH/new vector DB) is deferred until the timing
diagnostics justify it.
## Requirements
### Requirement: Optional Find Timing Diagnostics

The system SHALL expose opt-in timing diagnostics for `find` calls. When requested, the response
SHALL include total elapsed time, cache status, and per-stage timing entries for the retrieval work
that may affect latency, including freshness/cache lookup, keyword, BM25, vector, CLIP, graph,
temporal, fusion, filtering/hit construction, rerank, out-of-KB widening, date filtering, pack
assembly, and serialization. A skipped or unavailable optional lane MUST be represented as skipped
or unavailable rather than causing the call to fail. Timing diagnostics MUST NOT include note bodies,
excerpts, vectors, or other bulk content.

#### Scenario: Timing diagnostics are returned when requested

- **WHEN** `find` is called with timing diagnostics enabled
- **THEN** the result includes `timings.total_ms`
- **AND** the result includes per-stage timing entries for the stages that ran or were skipped
- **AND** the hit ranking is the same as the same request without timing diagnostics

#### Scenario: Timing diagnostics are omitted by default

- **WHEN** `find` is called without timing diagnostics enabled
- **THEN** the response shape is unchanged from the existing default `find` response
- **AND** no timing object is included in the returned hits

#### Scenario: Optional lane failure remains soft-fail

- **WHEN** an optional vector, CLIP, or rerank lane is unavailable during a timed `find` call
- **THEN** `find` still returns the fallback results it would return today
- **AND** the timing diagnostics identify that lane as skipped, unavailable, or failed without
  exposing bulk content

### Requirement: Compact and Full Find Result Surfaces

The system SHALL support a `find` result detail mode with `full` and `compact` values. `full` SHALL
be the default and SHALL preserve the current hit dictionary shape. `compact` SHALL return the same
ranked hits in a token-cheap shape that includes routing fields such as path, title, type, scope,
updated date, lifecycle status, media pointers, out-of-KB marker, and clip timestamp when present,
and MUST omit excerpt and detailed ranking signals unless a future explicit option asks for them.

#### Scenario: Compact mode omits token-heavy fields

- **WHEN** `find` is called with compact detail mode
- **THEN** each returned hit includes its path and title
- **AND** each returned hit omits `excerpt`
- **AND** each returned hit omits detailed `signals`

#### Scenario: Full mode remains the default

- **WHEN** `find` is called without a detail mode
- **THEN** each returned hit has the existing full shape, including `excerpt` and any existing
  optional fields that would have been present before this change
- **AND** the ranking and default return type are unchanged

#### Scenario: Compact mode preserves ranking and routing metadata

- **WHEN** the same `find` request is made once with full detail and once with compact detail
- **THEN** the ordered paths are identical
- **AND** compact hits still include the metadata needed to choose a follow-up `get` call

### Requirement: Hot Find Cache With Freshness Invalidation

The system SHALL maintain a small bounded in-process cache for repeated identical `find` requests.
The cache key MUST include every request parameter that can affect ranking or filtering. The cache
MUST be invalidated or bypassed when markdown freshness for the relevant scope changes, when an
embedding or CLIP sidecar that can affect the request changes, or when the active ranking config
identity changes. Cache hits MUST return copies or immutable results so caller mutation cannot alter
future cached responses.

#### Scenario: Repeated identical request can use cache

- **WHEN** the same `find` request is executed twice without vault, sidecar, or ranking-config
  freshness changes
- **THEN** the second call may be served from the hot cache
- **AND** timing diagnostics, when requested, report a cache hit
- **AND** the returned hits match the uncached result

#### Scenario: Markdown edit invalidates cached recall

- **WHEN** a markdown file that is in scope for a cached `find` request is created, edited, moved,
  or deleted
- **THEN** the next matching `find` request does not reuse the stale cached hit list
- **AND** the next result reflects the changed vault contents

#### Scenario: Sidecar freshness invalidates semantic recall cache

- **WHEN** an embedding or CLIP sidecar that can contribute to a cached hybrid, vector, or visual
  `find` request changes
- **THEN** the next matching `find` request does not reuse stale cached semantic results

#### Scenario: Different request knobs do not collide

- **WHEN** two `find` calls differ by query, filters, limit, scope, mode, graph/rerank options,
  date filters, activity preferences, or ranking configuration
- **THEN** they do not share the same cached hit list

### Requirement: Retrieval Architecture Changes Are Deferred Until Measured

The system SHALL NOT adopt a retrieval-architecture change (a new vector index backend,
LSH, ANN, or a new vector database) without per-lane timing measurement identifying the
lane and cost the change addresses. The vec0 SQL-native backend is adopted under this
rule: the per-lane timing diagnostics and the latency-vs-scale curve identified the vector
lane's in-memory O(N) matrix load and scan as the corpus-linear cost, and the backend is
held to exactness (full precision) or the golden retrieval floors (quantized). Any FURTHER
retrieval-architecture change (including an ANN index) SHALL be considered only with the
same evidence: a measured lane cost it addresses, plus the golden floors as its recall
gate.

#### Scenario: Backend adoption is evidence-gated

- **WHEN** this change adopts the vec0 backend for the vector lane
- **THEN** the latency-vs-scale harness records the in-memory scan's cost curve before the
  swap and the backend's curve after it
- **AND** the golden retrieval floors hold in every shipped configuration

#### Scenario: Further rewrites remain deferred until measured

- **WHEN** a future ANN index or retrieval rewrite is proposed
- **THEN** it is adopted only with per-lane measurement identifying the cost it addresses
- **AND** the golden retrieval floors gate its recall

#### Scenario: Change adds measurement before retrieval rewrite

- **WHEN** this change is implemented
- **THEN** the existing BM25, vector, keyword, CLIP, graph, temporal, fusion, rerank, and auto-widen
  paths remain the retrieval architecture
- **AND** the new behavior is limited to timing visibility, result serialization, cache reuse, and
  freshness-safe invalidation

### Requirement: Per-Request Freshness Snapshot

The system SHALL compute markdown freshness for a single `find` request at most once per scope and SHALL distinguish activated managed-server requests from explicit offline callers. An activated managed-server request MUST use one exact live checkpoint pair that is proven equal to both maintained catalogue checkpoints, pin that proof into its primary and optional-enrichment freshness snapshots, and MUST NOT perform a KB or vault markdown stat-walk to construct recall admission. When a required server projection is not live, its catalogue is not equal, or the projection advances before path-copy completion, the request SHALL return the retryable retrieval-warming outcome and schedule or observe repair without walking. Optional enrichment SHALL omit itself when it cannot consume the primary find's exact checkpoint proof. An offline/CLI caller with no activated runtime MAY fall back to the walk-based computation, still bounded to at most one KB walk and one vault walk per request. A `scope="kb-only"` request MUST NOT perform a vault-wide walk in either execution context. A live registry's triple and path set MUST equal a fresh policy-projected walk over the same state.

#### Scenario: Activated server request never walks for recall admission

- **WHEN** any keyword, hybrid, or vector `find` request runs in an activated server process and its required recall projection is not live
- **THEN** the request returns the retryable retrieval-warming outcome
- **AND** no KB or vault markdown stat-walk is performed by that request

#### Scenario: Offline caller retains one-walk correctness fallback

- **WHEN** `find` is invoked by an explicit offline/CLI caller with no activated runtime and the event-maintained registry is not live
- **THEN** the KB markdown tree is stat-walked at most once for the request
- **AND** the vault markdown tree is stat-walked at most once when vault scope is required
- **AND** the result reflects the same policy-projected source state as before this change

#### Scenario: One KB walk and one vault walk per request

- **WHEN** an explicit offline `find` caller uses `scope="kb"` with a non-empty query that also triggers auto-widen's vault-scope check and the event-maintained freshness registry is not live
- **THEN** the KB markdown tree is stat-walked at most once for that request
- **AND** the vault markdown tree is stat-walked at most once for that request, shared between auto-widen and every other vault-scope freshness check
- **AND** the returned hits are identical to the same request over the same policy-projected source state

#### Scenario: kb-only scope never walks the vault

- **WHEN** `find` is called with `scope="kb-only"`
- **THEN** no vault-wide markdown stat-walk occurs for that request

#### Scenario: A live registry answers freshness with no walk and identical results

- **WHEN** `find` is called for a scope whose event-maintained recall projection is live
- **THEN** that scope's freshness and allowed paths are obtained from the registry with no filesystem stat-walk
- **AND** the returned hits are identical to a policy-projected walk over the same vault state

#### Scenario: Catalogue proof is pinned through path acquisition

- **WHEN** a managed request proves catalogues against live checkpoints and one projection advances before its path set is copied
- **THEN** the request declines with the retryable retrieval-warming outcome
- **AND** it does not serve a mixed-generation result

#### Scenario: Referent enrichment shares the primary proof

- **WHEN** optional referent enrichment runs after a managed primary find
- **THEN** it consumes the same exact checkpoint pair admitted by that find
- **AND** it omits the enrichment if the projection advances rather than reading a different generation

### Requirement: Corpus Freshness Keys Detect Deletes, Renames, And Backdated Replacements

The BM25 index cache and the wikilink resolver cache SHALL use a freshness key that changes whenever
the set of markdown files in their scope changes by deletion, rename, or replacement with a file at
an older mtime than the file it replaced, in addition to changing on file-count or max-mtime
increases. A rebuild MUST be triggered whenever this key changes.

#### Scenario: Deleting a file invalidates the BM25 index

- **WHEN** a markdown file indexed by a previously built BM25 index is deleted and no remaining
  file's mtime increases
- **THEN** the next matching `find` request rebuilds the BM25 index for that scope

#### Scenario: A rename invalidates the wikilink resolver

- **WHEN** a markdown file is renamed without changing the vault's file count or any file's mtime
- **THEN** the next `find` request that needs the wikilink resolver rebuilds it rather than reusing
  the resolver built before the rename

#### Scenario: A backdated replacement invalidates the BM25 index

- **WHEN** a markdown file is replaced by a new file at the same path with an older mtime than the
  file it replaced, such that the scope's max mtime does not increase
- **THEN** the next matching `find` request rebuilds the BM25 index for that scope

### Requirement: Per-Page Derived-Text Reuse

The system SHALL compute each page's normalized body text, normalized title text, and stemmed token
set at most once per page revision, and SHALL reuse the computed values for every `find` call made
against that revision. A page revision change (the markdown file's mtime changing) MUST invalidate
the previously computed derived text for that page, and the next access MUST reflect the new
content.

#### Scenario: Repeated queries against an unchanged page reuse derived text

- **WHEN** two different `find` queries are evaluated against the same unchanged page
- **THEN** the page's normalized body, normalized title, and stemmed token set are computed once and
  reused for both queries
- **AND** both queries observe the same derived text and the same match/no-match outcome they would
  have observed if it had been recomputed for each query

#### Scenario: Editing a page invalidates its derived text

- **WHEN** a page's content is edited and its mtime changes
- **THEN** the next `find` call against that page computes fresh normalized body, normalized title,
  and stemmed token set from the new content

### Requirement: Startup Cache Warm-Up

The system SHALL warm the BM25 index (KB and vault scope), the wikilink resolver,
and the parsed-page cache during server startup when startup warm-up is enabled
and the active resource policy allows CPU cache preloading, so that a subsequent
`find` call does not pay first-call index/resolver/page-parse construction cost.
Warm-up SHALL be skipped when disabled by `EXOMEM_DISABLE_WARMUP` or when the
active resource policy is `quiet`, where low RAM residency is preferred over
first-query latency. Warm-up SHALL soft-fail per stage without preventing server
startup and MUST NOT change `find`'s returned results.

#### Scenario: Warm-up primes caches before the first query

- **WHEN** the server starts with warm-up enabled
- **AND** the active resource policy allows CPU cache preloading
- **THEN** the BM25 index for KB scope, the BM25 index for vault scope, the
  wikilink resolver, and the parsed-page cache are populated before the first
  `find` call is served
- **AND** the first `find` call's results are identical to what it would return
  without warm-up

#### Scenario: Warm-up can be disabled

- **WHEN** the server starts with `EXOMEM_DISABLE_WARMUP` set
- **THEN** no warm-up work is performed at startup
- **AND** `find` still returns correct results, built lazily on first use as it
  does today

#### Scenario: Quiet mode skips CPU cache warm-up

- **WHEN** the server starts in `quiet` mode
- **THEN** startup warm-up does not populate BM25 corpora, the wikilink resolver,
  parsed-page cache entries, embedding matrices, or CLIP matrices solely for
  warm-up
- **AND** `find` still returns correct results by building the required data
  lazily on first use

#### Scenario: A warm-up stage failure does not block startup

- **WHEN** one warm-up stage (for example, building the BM25 vault-scope index)
  fails
- **THEN** the server still starts successfully
- **AND** the failure is logged without raising, and other allowed warm-up stages
  still run

### Requirement: Process-Lifetime Embedding Matrix Cache

The system SHALL load the embedding (and CLIP) vector matrix from its sidecar at most once
per unchanged sidecar state and reuse it across `find` calls, via a process-shared
per-vault index instance, whenever the in-memory scan serves vector search. A brand-new
call site MUST NOT construct an index whose in-memory matrix starts empty and forces a
full reload. Startup warm-up MUST prime the backend that `find` will actually use: the
shared matrix when the in-memory scan serves search, or the vec0 tables' readiness (sync
check and first-touch) when the vec0 backend serves search — in which case the matrix MAY
remain unloaded and no `find` call may force a matrix load for search purposes.

#### Scenario: Repeated finds reuse a single matrix load

- **WHEN** two or more `find` requests run under the in-memory scan against a vault whose
  embedding sidecar has not changed between them
- **THEN** the vector matrix is loaded from the sidecar at most once for that unchanged
  state
- **AND** the later requests reuse the already-loaded matrix rather than re-reading and
  re-stacking every row

#### Scenario: Warm-up primes the backend find actually uses

- **WHEN** startup warm-up runs and a subsequent `find` executes with the sidecar
  unchanged
- **THEN** that `find` is served by a backend warm-up already primed — the shared matrix
  under the in-memory scan, or synced vec0 tables under the vec0 backend — without paying
  first-touch construction cost

#### Scenario: The vec0 backend does not hold the matrix resident

- **WHEN** the vec0 backend serves vector search for a process
- **THEN** `find` calls do not load the full vector matrix into Python memory for search

#### Scenario: Warm-up primes the matrix find actually uses

- **WHEN** startup warm-up loads the embedding matrix and a subsequent `find` runs
  with the sidecar unchanged
- **THEN** that `find` reuses the warmed matrix without a fresh full load

### Requirement: Write-Independent Find Latency

An in-process embedding write (upsert or delete) SHALL update the shared in-memory
matrix incrementally so that a concurrent `find` does not pay a full vault-sized
matrix reload per call while the sidecar is being written. A change to the sidecar
made outside the shared instance (for example an out-of-process writer) MUST still
be detected and reflected by the next `find`. An incremental update that cannot be
applied consistently MUST fall back to a correct full reload rather than return a
wrong or partial result.

#### Scenario: In-process write does not force a reload

- **WHEN** a file's rows are upserted or deleted through the shared index while the
  matrix is already loaded
- **THEN** the change is reflected on the next read without a full matrix reload
- **AND** the number of full reloads does not grow with the number of in-process
  writes

#### Scenario: A changed file's search results stay correct after an incremental update

- **WHEN** a file is upserted (including a change to its chunk count) or deleted and
  the matrix is patched in place
- **THEN** a search reflects the new content — the upserted file is findable, a
  deleted file's rows are gone — with the same ranking a full reload would produce

#### Scenario: Out-of-instance sidecar change is still reflected

- **WHEN** the embedding sidecar is modified by a writer that did not go through the
  shared index, advancing the sidecar's freshness
- **THEN** the next `find` detects the change and reflects the new sidecar contents

#### Scenario: An inconsistent incremental update falls back to a full reload

- **WHEN** an incremental matrix update cannot be applied consistently
- **THEN** the cache is invalidated and the next read performs a full reload
- **AND** no `find` returns a torn, partial, or incorrect matrix as a result

### Requirement: Sidecar Concurrency Mode

The embedding and CLIP sidecar connections SHALL use a journaling mode that lets a
reader proceed without blocking a concurrent writer and a writer without blocking
concurrent readers, so `find` latency does not track sidecar write churn. Enabling
this mode MUST soft-fail to the default journal without failing the operation when
the mode is unavailable.

#### Scenario: Reads are not blocked by a concurrent sidecar write

- **WHEN** a `find` reads the sidecar while a backfill is writing it
- **THEN** the read is not serialized behind the writer by the sidecar's journaling
  mode

#### Scenario: Concurrency mode failure is non-fatal

- **WHEN** the concurrency journaling mode cannot be enabled on a sidecar connection
- **THEN** the connection falls back to the default journal
- **AND** the operation still succeeds

### Requirement: Reproducible Benchmark Report

The retrieval eval harness (`scripts/eval_retrieval.py`) SHALL provide a `--report markdown` mode
that runs the existing golden query set once per retrieval mode — keyword, hybrid, and hybrid with
reranking — and emits a single markdown artifact containing, per mode, the harness's existing
aggregate ranking-quality metrics (NDCG@5, NDCG@10, MRR, recall@10) and median/p90 `find()`
wall-clock latency measured over repeated runs of the golden set. The report generation MUST be
reproducible: re-running the command against any vault and golden set that follow the documented
harness contract (a `tests/golden/queries.yaml`-shaped golden set and a resolvable vault) MUST
produce a report in the same shape, including against the bundled `tests/fixtures` sample vault as
a deterministic smoke path.

#### Scenario: Report includes per-mode metrics and latency

- **WHEN** `scripts/eval_retrieval.py --report markdown` is run
- **THEN** the emitted markdown includes one row for each of keyword, hybrid, and hybrid-with-rerank
- **AND** each row includes NDCG@5, NDCG@10, MRR, and recall@10
- **AND** each row includes median and p90 `find()` latency measured over the run

#### Scenario: Report is reproducible against the bundled sample vault

- **WHEN** `scripts/eval_retrieval.py --report markdown` is run against the bundled
  `tests/fixtures` sample vault instead of a private vault
- **THEN** the harness produces a report in the same shape as against any other vault
- **AND** no private-vault content is required to produce a smoke-scale report

#### Scenario: Existing sweep and baseline markdown modes are unchanged

- **WHEN** `scripts/eval_retrieval.py` is run with `--sweep` or the existing baseline `--markdown`
  flag without `--report markdown`
- **THEN** the output matches the harness's existing behavior before this requirement existed

### Requirement: Aggregate-Only Publication

The markdown report produced by `--report markdown` SHALL contain only aggregate values: per-mode
mean metrics, per-mode latency percentiles, and rounded corpus counts (files, notes, media). It
MUST NOT contain per-query rows, golden query text, vault-relative paths, excerpts, or any other
content that could reveal what a private vault contains. The report-rendering logic MUST accept
only plain aggregate data as input (no vault path, no query text argument) so this constraint is
structural rather than a formatting convention.

#### Scenario: No golden query text appears in the report

- **WHEN** `--report markdown` is generated from the golden set in `tests/golden/queries.yaml`
- **THEN** none of the golden set's query strings appear anywhere in the emitted markdown

#### Scenario: No vault-relative path appears in the report

- **WHEN** `--report markdown` is generated from the golden set in `tests/golden/queries.yaml`
- **THEN** none of the golden set's `expect_any_of` or `graded` target paths appear anywhere in the
  emitted markdown

#### Scenario: Corpus stats are rounded counts only

- **WHEN** the report includes corpus statistics
- **THEN** the statistics are rounded integer counts of files, notes, and media
- **AND** no exact file name, path, or content excerpt is included

#### Scenario: Report has one row per mode, not one row per query

- **WHEN** the report is rendered for N modes over a golden set of any size
- **THEN** the report contains exactly N result rows, one per mode
- **AND** the row count does not scale with the number of golden queries

### Requirement: Timing Diagnostics Include Request Profile Metadata
The system SHALL include compact request/profile metadata in `find` timing
diagnostics when `include_timings=true`. This metadata MUST be limited to
diagnostic flags and compute policy, and MUST NOT include note content, excerpts,
expanded query text, vectors, or private vault paths.

#### Scenario: Timed find includes profile metadata
- **WHEN** `find` is called with `include_timings=true`
- **THEN** `timings` includes a profile block identifying request knobs such as
  mode, detail, pack, graph, and rerank request state
- **AND** `timings` includes the current compute policy
- **AND** the hit ranking is unchanged compared with the same timed request before
  metadata serialization

#### Scenario: Untimed find shape is unchanged
- **WHEN** `find` is called without `include_timings=true`
- **THEN** the default response shape remains the existing hit list or pack envelope
- **AND** no timing profile metadata is returned

### Requirement: Structured Upload Success Metadata
The system SHALL return structured upload success metadata from `/upload` in
addition to the existing stored path fields. The metadata SHALL include the stored
binary path, byte size, SHA-256 hash, hash algorithm, media identifier, and content
type when available.

#### Scenario: Upload response identifies stored artifact
- **WHEN** a file is uploaded successfully through `/upload`
- **THEN** the JSON response includes existing `path` and `sidecar_path` fields
- **AND** it includes `stored_path`, `size`, `hash`, `hash_algorithm`,
  `media_id`, and `content_type`

#### Scenario: Upload metadata does not change authorization or duplicate behavior
- **WHEN** upload auth fails, a file is oversized, or a duplicate path is uploaded
- **THEN** the existing error codes and status codes are preserved

### Requirement: Release decisions do not fragment the recall cache

Governance release decisions SHALL NOT be stored in the `find` hot cache and SHALL
NOT be part of its key. The hot cache SHALL remain keyed on content and policy
fingerprints only, storing principal-free candidates; release decisions SHALL be
computed per request after a cache hit and memoized separately. Declared purpose
SHALL NOT enter the recall cache key.

#### Scenario: Cache hit still decides per principal

- **WHEN** a query result is served from the hot cache to a second audience
- **THEN** decisions are recomputed for that audience and no cached candidate
  carries a prior audience's decision

#### Scenario: Purpose does not bust the recall cache

- **WHEN** the same query is issued with different declared purposes
- **THEN** both are served from the same cached candidate set, with decisions
  applied afterward

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

### Requirement: Record query scale is explicitly bounded
Structured Records query SHALL apply file-size, parsed-item, returned-row, aggregate-cardinality, and response-size bounds with pagination/snapshot metadata. Optional derived caching SHALL be soft-fail and rebuildable; no heavy resident database or model SHALL be required for correctness.

#### Scenario: Oversized collection refuses or pages safely
- **WHEN** a collection exceeds a documented parse or response bound
- **THEN** the query refuses with actionable split/index guidance or returns a bounded page, never an unbounded dump or partial answer presented as complete

#### Scenario: Optional cache failure preserves correctness
- **WHEN** a derived collection cache is absent, stale, corrupt, or disabled
- **THEN** the bounded canonical-file path remains correct or the operation refuses explicitly without promoting cache state to truth

### Requirement: SQL-Native Lexical Search Backend

The system SHALL be able to serve the bm25 lane from an FTS5 inverted index in a
per-vault lexical sidecar, selected by `EXOMEM_LEXICAL_BACKEND` (`auto` | `fts5` |
`python`, default `auto`), so that per-query lexical cost scales with the query's
matching terms rather than with corpus size. Indexed text MUST be stemmed with the
same Snowball tokenization the in-process scorer uses, applied identically to
queries, so token and stemming semantics are unchanged. Because FTS5's BM25
scoring is a different variant, promotion of this backend MUST be gated by the
golden retrieval floors and their per-query pins (including the stemming pin) —
not by rank-identity. When the backend is unavailable in any way — FTS5 missing
from the SQLite build, the sidecar unreadable, or a runtime error — the lane MUST
soft-fail to the in-process scorer with unchanged results and without recording a
lane degradation. `EXOMEM_LEXICAL_BACKEND=python` MUST force the in-process paths
unconditionally.

#### Scenario: Indexed backend serves the bm25 lane

- **WHEN** the lexical sidecar is healthy and `EXOMEM_LEXICAL_BACKEND` is `auto`
- **THEN** the bm25 lane's ranked paths are produced by the FTS5 index
- **AND** the lane's interface to fusion is unchanged

#### Scenario: Golden floors gate the backend

- **WHEN** the golden retrieval evaluation runs with the FTS5 backend serving the
  bm25 lane
- **THEN** the golden floors and per-query pins hold, including the
  morphological-variant (stemming) pin

#### Scenario: Unavailable backend falls back silently

- **WHEN** FTS5 is unavailable or the lexical sidecar cannot be used
- **THEN** the bm25 lane returns the in-process scorer's results
- **AND** `find` records no lane degradation for the fallback itself

#### Scenario: Kill switch restores in-process behavior

- **WHEN** `EXOMEM_LEXICAL_BACKEND=python` is set
- **THEN** the bm25 and keyword lanes use the in-process paths even where the
  FTS5 backend is available

### Requirement: Keyword Substring Contract Preserved At Scale

The system SHALL preserve the keyword lane's exact matching contract — strict
case-insensitive substring, every whitespace token present, in title or body,
including mid-word matches — when the lane is served by the indexed backend. This
lane's gate is exact parity, not floors: for any query and corpus state, the
indexed keyword lane MUST return the same match set as the reference in-process
substring scan. Needles below the trigram indexable length MUST still honor the
contract via a fallback lookup over the stored raw text.

#### Scenario: Indexed keyword lane matches the reference scan

- **WHEN** the same keyword-mode query runs once under the indexed backend and
  once under the in-process scan, over the same corpus
- **THEN** the match sets are identical, including mid-word substring matches

#### Scenario: Short needles still honor the contract

- **WHEN** a keyword query contains a token shorter than the trigram indexable
  length
- **THEN** the returned match set still equals the reference scan's

### Requirement: Caller Can Bound Reranker Candidate Count

The system SHALL expose optional `rerank_max_candidates` on product recall and canonical find paths. When reranking runs, the scorer SHALL receive at most `min(3 * limit, rerank_max_candidates)` candidates. An explicit cap MUST be at least `limit` and no greater than the existing hard candidate ceiling; omission SHALL preserve current behavior.

#### Scenario: Caller selects a small rerank batch
- **WHEN** `limit=5`, reranking is enabled, and `rerank_max_candidates=5`
- **THEN** exactly the leading five available fused candidates are passed to the reranker
- **AND** optional-lane failure still returns deterministic fused results

#### Scenario: Caller supplies an invalid cap
- **WHEN** the cap is smaller than the requested result limit or exceeds the hard ceiling
- **THEN** the request fails locally with a validation error before model invocation

### Requirement: Reranker Bounds Are Observable And Cache-Safe

The requested and effective reranker candidate counts SHALL be included in timing/explanation metadata when requested, and `rerank_max_candidates` MUST participate in hot-cache identity. Ranking evidence MUST distinguish candidates scored by the reranker from fused-order tail candidates.

#### Scenario: Same query uses different caps
- **WHEN** otherwise identical requests use different reranker candidate caps
- **THEN** they do not share a cached ranked result
- **AND** diagnostics report the cap effective for each request

### Requirement: Reranking Remains Optional And Soft-Failing

The candidate cap MUST NOT enable reranking by itself or change the existing mode/device auto policy. A missing, warming, disabled, or failed reranker SHALL continue to return fused results with its existing explicit reason. The system SHALL NOT claim a hard wall-clock budget around a synchronous model call.

#### Scenario: Cap is provided while reranking is off
- **WHEN** `rerank=false` and a valid candidate cap is supplied
- **THEN** no reranker model is loaded or invoked
- **AND** the retrieval profile reports the existing explicit-false reason

#### Scenario: Reranker dependency is unavailable
- **WHEN** reranking is selected but its optional dependency cannot load
- **THEN** the request succeeds with fused results
- **AND** diagnostics report dependency-unavailable rather than a timeout guarantee

### Requirement: Reproducible Structured Category Latency Gate

The workstation retrieval harness SHALL run page-level and unit-level exact-category lanes in a live service process with semantic catalog and OS file cache warm. Each cold sample SHALL clear the in-process result and parsed-page caches, then run `scope=kb-only`, `mode=keyword`, empty query, graph/rerank/pack disabled, and two indexed candidates. It SHALL collect 30 samples and compute nearest-rank p95, excluding connector RTT and startup/catalog construction. Hot samples SHALL repeat an unchanged request with the result cache live. Cold `filter_eligibility` p95 MUST be below 100 ms, cold total p95 below 250 ms, and hot total p95 below 10 ms.

#### Scenario: Page and unit lanes expose equivalent stages

- **WHEN** the latency harness runs both result levels
- **THEN** each report contains comparable `filter_eligibility` and total stage distributions
- **AND** its sample count, cache reset policy, candidate bucket, and percentile method are recorded

### Requirement: Structural Scaling Is The CI Gate

CI SHALL use operation-count tests proving Markdown hydration tracks candidate count plus fixed overfetch rather than corpus size. Timing thresholds SHALL remain workstation release evidence and MUST NOT make shared-runner tests flaky.

#### Scenario: Corpus growth cannot hide a scan regression

- **WHEN** the same two candidates are embedded in 2,000-page and 8,000-page fixtures
- **THEN** parent opens remain within the same fixed bound
- **AND** the test fails if eligibility invokes a corpus walk

### Requirement: Real-Vault Reports Are Aggregate And Anonymized

Committed or shared real-vault performance evidence SHALL use synthetic category labels, anonymous run IDs, corpus-size buckets rounded to 500, candidate-count buckets, and latency distributions only. It MUST NOT contain exact category values or frequencies, query text, paths, excerpts, project names, or exact candidate counts.

#### Scenario: Report cannot reveal category usage

- **WHEN** a real-vault category benchmark report is rendered
- **THEN** category and candidate identities are replaced by synthetic labels and buckets
- **AND** no source path, excerpt, query text, or exact category frequency appears

### Requirement: Limited Exact Unit Recall Bounds Parent Work

For an empty-query semantic-unit request with a finite limit and a complete exact category/kind plan that requires no canonical structured post-filter, the system SHALL retrieve candidates in stable catalog order through a leading prefix bounded initially by a function of the requested limit. It SHALL apply canonical access policy and selected-parent validation, geometrically expand and recompute the complete prefix when a leading candidate is rejected, and stop after the requested number of eligible hits or catalog exhaustion. Expansion MUST NOT use a moving offset across separate catalog transactions. When leading candidates are eligible, the number of parents opened MUST be independent of total matching-category cardinality. Catalog readiness, exact DNF correlation, scope, and final ordering MUST remain unchanged.

#### Scenario: Broad category opens only a bounded leading window

- **WHEN** at least 128 current accessible semantic units share one exact category, the normalized query is empty, and `limit=3`
- **THEN** the three newest units are returned in the established deterministic order
- **AND** no more than eight candidate parents are validated or hydrated
- **AND** no Markdown scope walk occurs

#### Scenario: Inaccessible leading candidates do not false-empty

- **WHEN** the newest exact category candidates are excluded by canonical access policy and a later candidate is accessible
- **AND** an ordinary catalog writer deletes or reorders a leading row between prefix reads
- **THEN** ordered retrieval recomputes a complete expanded prefix until the accessible candidate is returned or the catalog is exhausted
- **AND** excluded content is never returned

#### Scenario: Page post-filter remains correctness-first

- **WHEN** an exact category seed is combined with a page predicate and more than one bounded window of newer candidates fails that predicate
- **THEN** the system evaluates candidates beyond those windows before applying the result limit
- **AND** it returns the first canonically eligible result rather than an empty list

#### Scenario: Incomplete catalog never becomes an authoritative empty result

- **WHEN** a limited exact unit request encounters a stale, transient, unsupported, or fatal catalog outcome
- **THEN** it preserves the typed incomplete exact-recall behavior
- **AND** it does not return or cache an authoritative empty hit list

### Requirement: Broad Category Latency Is Reproducibly Gated

The aggregate category-recall latency harness SHALL support an explicit broad-cardinality preflight in addition to its selective exact-cardinality default. The broad preflight SHALL require at least a configured number of candidate parents before running timed lanes, SHALL run the same fixed empty-query exact-category request shape, and SHALL apply the existing cold and hot percentile gates. Reports MUST continue to exclude the category, query text, vault path, exact candidate count, note paths, and excerpts.

#### Scenario: Broad profile rejects a selective category

- **WHEN** the broad-cardinality profile requires at least 100 candidates and preflight finds fewer than 100
- **THEN** timed lanes do not run
- **AND** the aggregate report marks cardinality preflight as failed without exposing the exact category or paths

#### Scenario: Broad profile runs the existing latency gates

- **WHEN** broad preflight meets its configured minimum and the catalog is ready
- **THEN** the harness samples the existing page/unit cold and hot lanes
- **AND** the existing filter-eligibility and total-latency percentile thresholds determine pass or failure
- **AND** only bucketed candidate cardinality is reported

### Requirement: SQL-Native Vector Search Backend

The system SHALL be able to serve vector KNN search from vec0 virtual tables co-located in
the existing embedding and CLIP sidecars, selected by `EXOMEM_VEC_BACKEND` (`numpy` |
`sqlite-vec`, default `numpy`). The default backend is the in-memory numpy scan; the vec0
backend MUST activate ONLY when `EXOMEM_VEC_BACKEND` is set explicitly to `sqlite-vec`. Any
other value — unset, the legacy `auto`, or an unrecognized string — MUST resolve to the
numpy scan. Under the default, the system MUST NOT probe for or load the sqlite-vec
extension, so installing the package MUST NOT change which backend serves search. When the
vec0 backend is opted into, in full-precision mode it MUST be exact: the ranked (path,
chunk) results and cosine scores MUST match what the in-memory scan returns for the same
sidecar state, within floating-point tolerance. When the opted-in backend is unavailable in
any way — the package is not installed, the Python build cannot load SQLite extensions, or a
runtime error occurs — vector search MUST soft-fail to the in-memory scan with unchanged
results and without recording a lane degradation. `EXOMEM_VEC_BACKEND=numpy` MUST force the
in-memory scan unconditionally. When the vec0 backend serves search, the process MUST NOT
need to hold the full vector matrix resident in Python memory for that search path. While
the vec0 backend is off, sidecar writers MAY skip vec dual-writes; a later opt-in MUST heal
any resulting blob-vs-vec drift from the stored blobs before serving vec0 results.

#### Scenario: Numpy is the default and the extension is never probed

- **WHEN** `EXOMEM_VEC_BACKEND` is unset (or set to `auto` or any unrecognized value)
- **THEN** vector search is served by the in-memory numpy scan
- **AND** the sqlite-vec extension is neither probed nor loaded, and no vec tables are
  written

#### Scenario: The vec0 backend is opt-in and exact

- **WHEN** `EXOMEM_VEC_BACKEND=sqlite-vec` is set and the same query runs once under the
  vec0 full-precision backend and once under the in-memory scan, over an unchanged sidecar
- **THEN** the ordered (path, chunk) results are identical
- **AND** the scores match within floating-point tolerance

#### Scenario: Installing sqlite-vec does not change the serving backend

- **WHEN** the sqlite-vec package becomes importable but `EXOMEM_VEC_BACKEND` is not set to
  `sqlite-vec`
- **THEN** vector search continues to use the in-memory numpy scan
- **AND** no vec tables are created by the search or write paths

#### Scenario: Unavailable extension falls back silently when opted in

- **WHEN** `EXOMEM_VEC_BACKEND=sqlite-vec` is set but sqlite-vec is not importable or the
  connection cannot load extensions
- **THEN** vector search returns the in-memory scan's results
- **AND** `find` records no vector-lane degradation for the fallback itself

#### Scenario: Kill switch forces the in-memory scan

- **WHEN** `EXOMEM_VEC_BACKEND=numpy` is set
- **THEN** vector search uses the in-memory scan even where the vec0 backend is available

#### Scenario: A runtime vec failure degrades to the scan for the process

- **WHEN** the vec0 backend is opted in and a vec0 KNN query raises at runtime
- **THEN** that search call returns correct results via the in-memory scan
- **AND** subsequent searches in the process stop attempting the vec0 backend

#### Scenario: Re-enabling vec0 heals drifted shadow tables

- **WHEN** a sidecar was advanced while the numpy default was in effect (vec shadow tables
  drifted from the blob tables) and a process later opts into `EXOMEM_VEC_BACKEND=sqlite-vec`
- **THEN** the first opt-in use rebuilds the vec rows from the stored blobs in pure SQL
- **AND** the vec0 backend then serves results identical to the in-memory scan

#### Scenario: Full-precision backend returns identical ranking

- **WHEN** the same query runs once under the vec0 full-precision backend and once under
  the in-memory scan, over an unchanged sidecar
- **THEN** the ordered (path, chunk) results are identical
- **AND** the scores match within floating-point tolerance

#### Scenario: Unavailable extension falls back silently

- **WHEN** sqlite-vec is not importable or the connection cannot load extensions
- **THEN** vector search returns the in-memory scan's results
- **AND** `find` records no vector-lane degradation for the fallback itself

### Requirement: Opt-In Quantized Vector Mode

The system SHALL support a binary-quantized vector search mode, enabled only by
`EXOMEM_VEC_QUANT=binary` (default `off`). Quantized search MUST rescore its candidate set
against the stored full-precision vectors and return true cosine scores, so downstream
score semantics are unchanged. The quantized configuration MUST clear the golden retrieval
floors (the same NDCG/MRR/recall floors and per-query zero-recall guard the default
configuration is held to) before being recommended, and the mode MUST NOT be enabled
implicitly by corpus size or any other heuristic.

#### Scenario: Quantized mode passes the golden retrieval gate

- **WHEN** the golden retrieval evaluation runs with `EXOMEM_VEC_QUANT=binary`
- **THEN** mean NDCG@10, MRR, and recall@10 clear the same floors as the default
  configuration
- **AND** no golden query drops to zero recall

#### Scenario: Quantized scores are full-precision cosine

- **WHEN** a query runs in quantized mode
- **THEN** returned scores are cosine similarities computed from full-precision vectors,
  not quantized distances

#### Scenario: Quantization is never implicit

- **WHEN** `EXOMEM_VEC_QUANT` is unset
- **THEN** vector search never uses the quantized tables, at any corpus size

### Requirement: Quiet Mode Uses Evictable Find Caches

The system SHALL make find's large CPU-side caches evictable in quiet mode. This
includes parsed pages, hot find-result entries, resolver state, BM25 corpora and
token caches, embedding matrices, and CLIP matrices. Eviction MUST NOT delete
sidecar rows, mutate vault files, or disable future `find` calls.

#### Scenario: Entering quiet evicts find caches

- **WHEN** the process has populated find-related RAM caches
- **AND** the effective mode changes to `quiet`
- **THEN** the process evicts the large find-related RAM caches that can be
  rebuilt lazily
- **AND** no vault file or sidecar row is deleted as part of cache eviction

#### Scenario: Idle quiet cache is evicted

- **WHEN** the effective mode is `quiet`
- **AND** a find-related RAM cache has been idle longer than the configured idle
  threshold
- **THEN** the idle resource reaper evicts that cache

#### Scenario: Find after eviction is correct

- **WHEN** a `find` request runs after quiet-mode cache eviction
- **THEN** `find` rebuilds or reloads the required cache data from the vault or
  sidecar
- **AND** the ranked result paths match a warm-cache request over the same vault
  and sidecar state

### Requirement: Resource Status Reports Find Cache Residency

The system SHALL expose best-effort residency diagnostics for find-related caches
without loading those caches. Diagnostics SHALL include whether each large cache
class is loaded and SHOULD include counts or byte estimates when those values are
available from existing in-memory objects.

#### Scenario: Status reports matrix cache residency

- **WHEN** an embedding or CLIP matrix cache is resident
- **THEN** resource status reports that the matrix cache is loaded and includes
  its row count or byte estimate
- **AND** the status call does not read the sidecar to compute that value

#### Scenario: Status reports absent cache without loading it

- **WHEN** a BM25 corpus or parsed-page cache is not resident
- **THEN** resource status reports it as absent or zero-sized
- **AND** the status call does not build the cache to answer the query

### Requirement: Optional recall stages remain checkpoint-bounded and post-cache
The referents stage SHALL compute from released hits after the shared hit cache, SHALL never change the cache key or cached object, and SHALL record a `referents` timing span only when a deterministic cue is eligible.

#### Scenario: Hot cache hit
- **WHEN** a cue query is served from the find hot cache
- **THEN** referents are recomputed post-cache and match the cold response

#### Scenario: Scale bound
- **WHEN** the warm stage is measured at 2k/125 and 8k/500 pages/entities
- **THEN** 2k remains below 1000 ms and 8k remains within max(1.5x, +25 ms)

### Requirement: Managed Catalogue Recovery Has One Owner

Managed server startup SHALL either prove the maintained catalogue current or delegate recovery to the existing single-flight background repair worker. It MUST NOT run synchronous in-place catalogue reconciliation concurrently with watcher-driven repair. The repair worker SHALL promote retrieval readiness only after publishing a checkpoint-current catalogue. An explicit offline caller MAY retain synchronous reconciliation when no managed repair owner exists.

#### Scenario: Stale managed startup delegates instead of rebuilding twice

- **WHEN** watcher seeding has completed but the managed server catalogue is not current
- **THEN** startup requests the single-flight background repair and leaves retrieval unadmitted
- **AND** startup does not invoke synchronous in-place catalogue reconciliation
- **AND** successful background publication promotes retrieval without a process restart

#### Scenario: Repeated stale probes coalesce into the active full repair

- **WHEN** health or request probes repeatedly observe the same stale catalogue while its full background rebuild is active
- **THEN** the repair scheduler treats those observations as one level-triggered repair request
- **AND** it does not queue another full-corpus pass behind the active pass
- **AND** a stronger full-rebuild request arriving during a targeted repair is still honoured

#### Scenario: A declined or superseded pass does not acknowledge uncovered work

- **WHEN** a full repair request arrives during a pass that declines publication or cannot prove its publication current
- **THEN** that request remains pending after the active worker yields
- **AND** the worker does not immediately chain another whole-corpus scan
- **AND** a later caller starts one fresh bounded repair flight for the pending work

#### Scenario: A post-proof generation is not acknowledged by the older proof

- **WHEN** a repair proves generation N current and generation N+1 is observed before the worker clears its active marker
- **THEN** the generation-tagged N+1 repair request remains pending
- **AND** generation N's proof does not acknowledge or discard it

#### Scenario: Watcher catch-up after publication retries admission

- **WHEN** a full repair publishes but the live projection advances before that publication can promote retrieval
- **AND** the watcher successfully applies the newer generation through one bounded catalogue batch, including any changed and deleted paths together
- **THEN** the watcher persists only the exact checkpoint whose complete delta and path coverage it proved while the publication barrier remains held
- **AND** neither half of a mixed changed/deleted generation is admitted separately
- **AND** the watcher path re-proves both scopes read-only after releasing the publication barrier
- **AND** it promotes retrieval only when the catalogue exactly matches both live projections
- **AND** it does not require another whole-corpus rebuild to clear stale process admission

### Requirement: Recall Projection Timing Is Attributed

Timing diagnostics SHALL attribute recall admission/projection acquisition, watcher-seed waiting observed by a request, resolver acquisition, and referent enrichment as named stages. Material time spent in those stages MUST NOT appear only as `unattributed_ms`.

#### Scenario: Projection acquisition appears in timings

- **WHEN** a timed `find` acquires a recall projection or declines because it is not live
- **THEN** timing diagnostics include a recall-projection stage with its elapsed time and outcome
- **AND** the same elapsed work is not counted only as unattributed time

