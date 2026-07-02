## ADDED Requirements

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
