# instant-start Specification

## Purpose
TBD - created by archiving change add-instant-start-boot. Update Purpose after archive.

## Requirements

### Requirement: Non-Blocking Boot

The system SHALL NOT block the MCP transport on any model preload or cache warm-up. `build_server()`
SHALL return, and `mcp.run()` SHALL begin serving, before the embedding model, reranker, CLIP model,
or lexical caches (parsed pages, BM25, wikilink resolver) have necessarily finished loading. This
SHALL hold identically for the stdio transport and the http transport.

#### Scenario: Stdio client is answered immediately

- **WHEN** exomem is started with `--transport stdio` against a vault whose models are not yet
  cached locally
- **THEN** the MCP `initialize` handshake completes without waiting for any model load or cache
  warm-up to finish
- **AND** the embedding model, reranker, CLIP model, and lexical caches continue loading on a
  background thread after `initialize` has already returned

#### Scenario: Http transport begins serving immediately

- **WHEN** exomem is started with an http transport
- **THEN** the server accepts connections and responds to requests before any model preload or
  lexical cache warm-up has necessarily finished
- **AND** a request that does not require an unready component (e.g. a keyword-mode `find`) returns
  normally without waiting on the warm-up

### Requirement: Lexical-First Warm Ordering

The background warm sequence SHALL warm lexical/derived caches (parsed pages, BM25 corpora for both
scopes, the wikilink resolver, and the embedding/CLIP matrices) before beginning any model preload.
Each stage SHALL mark its readiness component as soon as that stage completes, in the order:
lexical, embeddings, reranker, clip.

#### Scenario: Lexical readiness lands before model readiness

- **WHEN** the background warm-up runs against a vault with markdown content
- **THEN** the `lexical` readiness component becomes ready before the `embeddings` readiness
  component becomes ready
- **AND** `embeddings` becomes ready before `reranker`, and `reranker` before `clip`

#### Scenario: Keyword find is available as soon as lexical warm completes

- **WHEN** the `lexical` readiness component is ready but `embeddings` is not yet ready
- **THEN** a keyword-mode `find` call returns full results without deferring on any lane

### Requirement: Non-Blocking Degradation During Warm

The system SHALL check readiness before a hybrid, rerank, or image-aware `find` lane touches that
lane's model getter, SHALL skip the lane instead of blocking when its component is still warming,
and SHALL still return promptly using the lanes that are ready. The request SHALL NEVER block on a
model-loading lock held by the background warm thread.

#### Scenario: Hybrid find degrades to lexical-only results mid-warm

- **WHEN** a hybrid-mode `find` call arrives while the `embeddings` readiness component is not yet
  ready
- **THEN** the vector lane is skipped instead of calling the embedding model getter
- **AND** the call returns promptly using the BM25/keyword/graph lanes that are available
- **AND** the response records `embeddings` as a deferred/degraded component

#### Scenario: Rerank request degrades when the reranker is not ready

- **WHEN** a `find` call with `rerank=true` arrives while the `reranker` readiness component is not
  yet ready
- **THEN** the rerank stage is skipped instead of calling the reranker model getter
- **AND** the call returns the un-reranked ranking promptly

#### Scenario: Image-aware find degrades when CLIP is not ready

- **WHEN** a `find` call that would use the CLIP lane arrives while the `clip` readiness component
  is not yet ready
- **THEN** the CLIP lane is skipped instead of calling the CLIP model getter
- **AND** the call returns promptly using the remaining lanes

#### Scenario: Deferred lane never blocks on the warm thread's model lock

- **WHEN** the background warm thread is inside a model preload for a component
- **THEN** a concurrent `find` request needing that same component does not wait for the preload to
  finish
- **AND** the request instead defers that lane immediately and returns

#### Scenario: Degraded ranking is never stored in the hot find cache

- **WHEN** a `find` call produced mid-warm skipped one or more model lanes — including calls from
  internal callers (link suggestion, evolution, write-time sweeps) that receive no degradation
  signal
- **THEN** that lexical-only result is NOT stored in the hot find-result cache
- **AND** an identical query after the warm completes computes the full ranking instead of serving
  the degraded one

#### Scenario: Write-time corpus sweeps skip instead of blocking mid-warm

- **WHEN** a write (`add`, `note`, `edit`) or a context-pack assembly would run the
  duplicate/contradiction cosine sweep while the `embeddings` component is not yet ready
- **THEN** the sweep returns its documented empty no-op result without touching the embedding model
  getter
- **AND** the write or pack completes promptly without waiting for the warm thread

### Requirement: Warming Response Marker

The `find` response SHALL include a `warming` object alongside `hits` — listing the components
that were deferred and the number of seconds since the background warm began — whenever one or
more lanes were deferred because a component was not yet ready. When no lane was deferred, the
response SHALL NOT include a `warming` object and SHALL be unchanged from today's shape.

#### Scenario: Warming marker appears when a lane was deferred

- **WHEN** a `find` call defers at least one lane because its component is not ready
- **THEN** the response is an envelope of the form `{"hits": [...], "warming": {"components":
  [...], "since_s": N}}`
- **AND** `components` lists only the components that were actually deferred for that call

#### Scenario: No warming marker when nothing was deferred

- **WHEN** a `find` call completes without deferring any lane (warm-up complete, warm-up disabled,
  or a keyword-only call that needed no model)
- **THEN** the response contains no `warming` field
- **AND** the response shape matches the pre-existing default `find` response

### Requirement: Deferred Write Embedding With Post-Warm Drain

A write that lands while the `embeddings` readiness component is not yet ready SHALL have its
re-embed work item recorded for later processing instead of triggering a model load on the write
path. Once the `embeddings` component becomes ready, every deferred item SHALL be embedded exactly
once. A deferred item SHALL never be embedded twice and SHALL never be silently dropped while the
process stays up.

#### Scenario: Write during warm-up defers its embed

- **WHEN** a markdown file is written while the `embeddings` readiness component is not yet ready
- **THEN** the write completes without loading the embedding model
- **AND** the file's re-embed work is recorded for the post-warm drain

#### Scenario: Deferred writes are drained exactly once

- **WHEN** the `embeddings` readiness component becomes ready after one or more writes were
  deferred
- **THEN** every deferred file is re-embedded exactly once as part of that transition
- **AND** no deferred item is embedded a second time by a later, unrelated drain

#### Scenario: Process death before drain is recovered by existing drift tooling

- **WHEN** the process exits before a deferred write is drained
- **THEN** the affected file's on-disk mtime is newer than its embedding sidecar row
- **AND** the existing `embedding_drift` audit finding and `reconcile` command detect and heal the
  gap without any new recovery mechanism

### Requirement: Eager Boot Escape Hatch

Setting `EXOMEM_EAGER_BOOT` truthy SHALL restore the previous fully-synchronous boot: `build_server()`
SHALL run the complete warm sequence (lexical caches, then embedding, reranker, and CLIP preloads)
to completion before returning, identical in ordering and soft-fail behavior to the background warm
sequence, just performed inline.

#### Scenario: Eager boot blocks until warm-up completes

- **WHEN** exomem starts with `EXOMEM_EAGER_BOOT` set truthy
- **THEN** `build_server()` does not return until the lexical caches and all enabled model preloads
  have finished or soft-failed
- **AND** no readiness component ever reports `should_defer` as true during that process's lifetime,
  since the warm window has already closed by the time requests are served

#### Scenario: Eager boot preserves today's blocking behavior

- **WHEN** `EXOMEM_EAGER_BOOT` is set truthy
- **THEN** the observable boot behavior (order of log lines, soft-fail handling, total blocking
  time) matches the pre-instant-start boot sequence

### Requirement: Warm Readiness Logging

The background (or eager) warm sequence SHALL log a transition line as each readiness component
becomes ready, and SHALL log a final summary line with per-stage durations when the full sequence
completes.

#### Scenario: Final summary line reports durations

- **WHEN** the warm sequence completes (successfully or with soft-failed stages)
- **THEN** a log line reports the warm as complete along with per-stage durations

#### Scenario: Soft-failed stage is visible in the log

- **WHEN** a model preload soft-fails during the warm sequence
- **THEN** the failure is logged at warning level and the sequence continues to the next stage
  without raising

### Requirement: Explicit Model Pre-Download Command

The system SHALL provide an `exomem warm` CLI subcommand that explicitly preloads the embedding
model, reranker, and (when enabled) CLIP model, showing Hugging Face download progress on a TTY,
reporting per-step durations, and exiting `0` on success or `1` if any required preload failed. An
optional `--vault` flag SHALL additionally warm the lexical caches for that vault. The command
SHALL respect `EXOMEM_DISABLE_EMBEDDINGS`, skipping model preloads with an explanatory message
rather than failing.

#### Scenario: Warm command pre-downloads models with visible progress

- **WHEN** `exomem warm` is run on a TTY against an installation without cached models
- **THEN** Hugging Face download progress is visible for each model
- **AND** the command reports a duration for each step and exits `0` on success

#### Scenario: Warm command also warms lexical caches with --vault

- **WHEN** `exomem warm --vault <path>` is run
- **THEN** the parsed-page cache, BM25 corpora, and wikilink resolver for that vault are warmed in
  addition to the model preloads

#### Scenario: Warm command respects the embeddings kill switch

- **WHEN** `exomem warm` is run with `EXOMEM_DISABLE_EMBEDDINGS` set
- **THEN** model preloads are skipped with an explanatory message
- **AND** the command does not fail because of the skipped models

#### Scenario: Warm command reports failure

- **WHEN** `exomem warm` is run and a required model preload fails
- **THEN** the command exits `1`
- **AND** the failure is reported per step so the operator knows which model failed

### Requirement: Managed Full Rebuild Has One Owner

While managed retrieval is unavailable, the detached single-flight repair worker
SHALL be the only component allowed to perform a full lexical-catalogue rebuild.

#### Scenario: Startup delegates incomplete catalogue repair

- **WHEN** managed startup finds an incomplete or stale lexical catalogue
- **THEN** it schedules the detached repair worker
- **AND** does not start an in-place whole-vault rebuild beside it

#### Scenario: Ordinary maintenance cannot start a competing full rebuild

- **WHEN** a writer or watcher observes stale schema, identity, or checkpoints
  during an active detached repair
- **THEN** it records bounded repair demand for the single-flight owner
- **AND** does not perform an in-place whole-catalogue rebuild

#### Scenario: Refused reads coalesce behind the active generation

- **WHEN** repeated managed reads are refused while the current generation is
  already being repaired
- **THEN** their repair requests coalesce behind that flight
- **AND** one worker does not chain repeated whole-vault passes before yielding

### Requirement: Projection-First Runtime Activation

The local runtime SHALL start filesystem observation and seed both event-maintained recall scopes before maintained-catalog verification begins. Events observed during the seed SHALL be replayed after authoritative seed publication, and activation SHALL wait for that catch-up replay before catalogue verification begins. When watchdog is unavailable but event indexes remain enabled, reconcile-only polling SHALL establish and maintain the projection; when event indexes are explicitly disabled, the system SHALL run the existing full verification only as background startup work. Graph drain, media reconciliation, and other optional/heavy workers MUST NOT contend with the projection/catalogue path before retrieval is actually admitted. They SHALL serialize behind semantic-corpus work while the initial warm is active, but a terminal semantic soft-failure MUST NOT strand them.

#### Scenario: Watcher seed precedes catalogue verification

- **WHEN** a local server starts with event-maintained indexes and the watcher enabled
- **THEN** filesystem observation starts and both recall scopes become live before catalogue verification reads their checkpoints
- **AND** catalogue verification does not take the non-live projection walk fallback

#### Scenario: Watcher-free startup remains functional

- **WHEN** watchdog is unavailable while event-maintained indexes remain enabled
- **THEN** reconcile-only polling seeds and maintains the recall projection
- **AND** transport liveness remains available

#### Scenario: Event indexes disabled retains explicit rollback behavior

- **WHEN** event-maintained indexes are explicitly disabled
- **THEN** transport liveness remains available
- **AND** required catalogue verification may use the existing background walk fallback
- **AND** the declared legacy lazy request fallback remains available

#### Scenario: An edit observed during seed is not overwritten

- **WHEN** filesystem observation reports an edit while the startup seed is still deriving its replacement maps
- **THEN** dispatch retains that event until seed publication completes
- **AND** activation remains behind the seed barrier until replay updates the published generation
- **AND** catalogue verification cannot admit the stale pre-replay checkpoint

#### Scenario: Terminal catalogue warm failure keeps heavy recovery gated

- **WHEN** the first managed warm finishes without retrieval-catalog admission
- **THEN** graph drain, media reconciliation, and other optional heavy workers remain gated
- **AND** a later proven repair releases them without a process restart

#### Scenario: Terminal semantic warm failure remains soft

- **WHEN** retrieval is admitted but the one-shot semantic-corpus warm finishes unsuccessfully
- **THEN** graph drain, media reconciliation, and other optional heavy workers continue
- **AND** the missing semantic ready bit does not create an unrecoverable startup wait

### Requirement: Retrieval Readiness Recovers After Repair

Retrieval admission SHALL be derived from proven current projection and catalogue state, not only from the outcome of the first warm attempt. The proof SHALL bind both maintained catalogue checkpoints to the exact live projection checkpoints used by the request. If startup catalogue warming fails and a later seed or catalogue repair converges, the runtime SHALL promote retrieval to ready without a process restart. If a previously ready runtime loses its live projection, catalogue equality, or request-pinned generation, it SHALL demote before serving another recall request.

#### Scenario: Later repair heals failed startup admission

- **WHEN** the first catalogue warm attempt fails and readiness reports retrieval unavailable
- **AND** both recall projections later become live and both maintained catalogue checkpoints match them
- **THEN** retrieval admission transitions to ready without restarting the process

#### Scenario: Lost projection cannot retain ready admission

- **WHEN** retrieval was ready and a required live recall projection is subsequently lost or invalidated
- **THEN** the next server recall request does not use walk fallback or stale ready admission
- **AND** retrieval is reported as warming or unavailable until proof converges again

#### Scenario: Projection advance cannot mix request generations

- **WHEN** a projection advances after catalogue admission proof but before the request copies its allowed paths
- **THEN** the request returns the retryable retrieval-warming outcome
- **AND** it does not combine the older catalogue with the newer projection

### Requirement: All Retrieval Modes Obey Admission

Keyword, hybrid, and vector-only requests SHALL obey the same required recall-projection admission boundary. Optional model-backed lanes remain soft-failing after lexical admission and MUST NOT be promoted into required readiness components.

#### Scenario: Vector mode cannot bypass projection warming

- **WHEN** a vector-only server request arrives before the recall projection is live
- **THEN** it returns the same retryable retrieval-warming outcome as other modes
- **AND** it does not construct a walk-backed allowlist

### Requirement: Disabled Warmup Preserves Lazy Operation

Explicitly disabling startup warmup SHALL leave local recall in its unverified lazy mode. The runtime MUST NOT create a managed warming state that can never finish when no warm was started.

#### Scenario: Warmup kill switch does not strand readiness

- **WHEN** `EXOMEM_DISABLE_WARMUP=1` is set at local runtime construction
- **THEN** retrieval admission remains unverified rather than permanently warming
- **AND** the existing lazy caller behavior remains available

### Requirement: Governed writes are never fenced into a whole-vault rebuild by unattributed filesystem events

The projection-freshness fence raised by filesystem events the process cannot attribute to itself SHALL apply only to reads that require a current projection. A governed write SHALL always compute its predecessor from the checkpoint lineage. An unreadable predecessor SHALL be reported as a distinct outcome and routed to bounded incremental repair of the affected paths, never collapsed into the lineage-gap outcome that schedules a whole-vault rebuild. External marks SHALL be recorded per path and drained by the incremental repair. A governed write whose own refreshed paths are under a path-scoped mark SHALL queue exactly those paths for that repair and report its own pending outcome, and SHALL NOT schedule a whole-vault rebuild; the coverage SHALL be a durable receipt the drain converges, never an assumption that the observer's own repair will land. A mark that names no scope states that the affected set is unknown and SHALL keep scheduling the whole-vault rebuild. A governed write whose acknowledged lineage is behind its checkpoint SHALL NOT schedule a whole-vault rebuild when every generation the acknowledgement skipped is itself queued as durable repair; it SHALL take the bounded incremental path and report a pending outcome of its own, and SHALL NOT advance the acknowledgement it did not project. Coverage SHALL be proven from the durable receipts, which SHALL record the generation they were queued for; a receipt that does not record one SHALL NOT count as coverage. A process that proves a snapshot it did not publish SHALL adopt it as its delta origin, and SHALL do so even when a bounded set of paths is found unrepaired, by queueing exactly those paths for incremental repair. That adoption SHALL run on the unconditional start-up path, independently of any resource policy governing the preloading of rebuildable caches. A bail-out caused by a cache this process simply does not hold, rather than by stored state it cannot read, SHALL queue the affected paths and report its own pending outcome instead of scheduling a whole-vault rebuild.

#### Scenario: Writes after a worker replacement stay incremental
- **WHEN** a replacement worker starts with an empty self-attribution table and observes filesystem events from the previous worker and the migrator, and then serves ten governed writes
- **THEN** no write schedules a whole-vault rebuild on its own account: an adopted snapshot makes the first write incremental, at most one coalesced rebuild runs per proven lineage divergence between watcher repairs and governed writes, no write joins a rebuild past its budget, each write acknowledges within its bound, and the repair queue drains to zero

#### Scenario: Adoption tolerates a bounded residue
- **WHEN** a replacement process proves the previous snapshot and finds a bounded set of paths whose canonical bytes differ from the snapshot's recorded state
- **THEN** it adopts the snapshot at its checkpoint, queues exactly those paths for incremental repair, reads that require a current projection keep refusing until that repair lands, and the process's first write is incremental

#### Scenario: Adoption does not depend on the cache-preload policy
- **WHEN** a replacement worker starts in a resource mode that does not preload rebuildable CPU caches
- **THEN** start-up still proves and adopts the inherited snapshot, records its residue, and leaves a recall resolver resident, so the first governed write is incremental

#### Scenario: A cold resolver cache is queued repair, not a lineage gap
- **WHEN** a governed write's incremental pass has proven its predecessor and a complete recall delta but this process holds no resident recall resolver for that checkpoint
- **THEN** the delta is queued for incremental repair, the write is acknowledged with a pending outcome naming the cold resolver, no whole-vault rebuild is scheduled, and a later drain converges the queue

#### Scenario: An unrelated external edit does not fence a write
- **WHEN** an unattributed edit lands on one path while a governed write commits to another
- **THEN** the write takes the incremental path, the edited path is queued for repair, and reads that require a current projection continue to refuse until that repair lands

#### Scenario: A mark on the write's own paths is queued repair
- **WHEN** an unattributed edit lands on the very path a governed write is about to commit, the mark names that path, and it is still unrepaired when that write dispatches
- **THEN** the write defers, queues exactly its own paths as durable graph repair, reports a pending outcome naming the unattributed event, schedules no whole-vault rebuild, and a drain converges those paths whether or not the observer's own repair lands first

#### Scenario: A lineage gap the queue already owns is not a rebuild
- **WHEN** governed writes arrive faster than the repair queue drains, so each one's predecessor probe finds the acknowledgement a generation or more behind, and every skipped generation's paths are on the durable queue
- **THEN** each write takes the bounded incremental path, queues its own paths, reports a pending outcome naming the covered gap, schedules no whole-vault rebuild, and leaves the acknowledgement where it was until a drain actually projects it

#### Scenario: A gap the queue cannot account for still rebuilds
- **WHEN** a skipped generation has no durable receipts, or a queued receipt does not record which generation it was queued for
- **THEN** the gap is treated as a proven divergence and the whole-vault rebuild is scheduled

#### Scenario: A real lineage gap still rebuilds
- **WHEN** the checkpoint lineage proves the snapshot cannot be advanced incrementally
- **THEN** a whole-vault rebuild is scheduled and the outcome names the lineage gap, not an unreadable predecessor

### Requirement: Joins on graph work are bounded and rebuild demand coalesces

No caller SHALL wait on graph registration without a budget. The budget SHALL derive from the request deadline when one exists and from a bounded default otherwise; past it the caller receives a pending outcome carrying the checkpoint it can poll. Rebuild demand that arrives while a whole-vault rebuild is in flight SHALL coalesce into at most one follow-up rebuild under the single-flight owner.

#### Scenario: A standalone caller cannot block for a rebuild
- **WHEN** a caller that cannot carry a pending outcome commits a write while a whole-vault rebuild is in flight
- **THEN** it receives its acknowledgement within the budget with a pending graph outcome and the checkpoint to poll

#### Scenario: Ten writes during one flight yield one follow-up
- **WHEN** ten governed writes commit while one whole-vault rebuild is running
- **THEN** at most one further whole-vault rebuild runs after it and every write is registered by that pass

### Requirement: Readiness distinguishes serving from cutover

Runtime readiness SHALL keep the serving `ready` status and SHALL additionally report a cutover component set naming the lexical catalog, the embedding model when preload is allowed, the proven graph snapshot, and the semantic corpus context. A standby worker SHALL report which cutover component it is waiting on. After a promotion, readiness SHALL report which cutover components were carried forward from the standby. Queued vocabulary recovery SHALL drain in the background once the graph projection is current.

#### Scenario: A standby reports what it waits on
- **WHEN** a standby worker has warmed the lexical catalog but not yet proven the graph snapshot
- **THEN** readiness reports serving-ready and cutover-not-ready with `graph_snapshot` as the waiting component

#### Scenario: A standby that has not built the corpus is not cutover-ready
- **WHEN** a standby worker has proven its graph snapshot but has not built the semantic corpus context the write admission gate waits on
- **THEN** readiness reports cutover-not-ready with `semantic_corpus` as the waiting component

#### Scenario: Recovery drains without a review call
- **WHEN** vocabulary recovery rows were queued while the projection was warming and the projection becomes current
- **THEN** the activation sequence drains them within its bounded pass and no client review call is required

### Requirement: Retrieval stays admitted across a worker replacement

A serving process that inherits a maintained lexical catalogue published by an earlier process SHALL be able to bless that catalogue with an ordinary delta on its first governed write.

When warm-up proves an inherited catalogue checkpoint state-equal to this process's live projection, the serving process SHALL re-stamp that checkpoint in its own registry lineage. It SHALL do so under the catalogue publication barrier and without changing any row. A standby SHALL NOT re-stamp; it only proves.

A bounded catalogue mutation can apply its rows and still be unable to bless a live scope, because the scope has no stored checkpoint or no retained history bridges the stored checkpoint to the live one. In a managed runtime, such a mutation SHALL hand that scope to the catalogue repair owner, so readiness converges without a process restart. The readiness probe SHALL remain side-effect free.

#### Scenario: First write after a replacement keeps retrieval admitted

- **WHEN** a managed process starts on a catalogue an earlier process published, and warm-up admits retrieval
- **AND** the process then serves one governed write
- **THEN** the write blesses both recall scopes at this process's live checkpoint
- **AND** no catalogue repair runs
- **AND** the readiness proof keeps retrieval admitted

#### Scenario: A stranded scope converges through repair

- **WHEN** a governed write in a managed runtime applies its catalogue rows but cannot bless a live scope, because no retained history bridges that scope's stored checkpoint
- **THEN** the write hands the scope to the catalogue repair owner
- **AND** once that repair publishes a current catalogue, retrieval is admitted again without a process restart
