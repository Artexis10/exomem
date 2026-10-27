<!-- authority:non-specification -->

# Observability

Structured logs, metrics, and correlation across exomem's MCP, REST, hosted,
and CLI surfaces. Everything here is stdlib-only, default-on, and soft-fails
independently — a bug in logging or metrics can never break a tool call, an
HTTP request, or a mutation.

## Files

All under the resolved log directory (`logging_config.resolve_log_dir()`):
`$EXOMEM_LOG_DIR` when set (the Docker image sets it to `/data/logs`), else
`<checkout>/logs` when running from a genuine source checkout, else a
per-platform location for a packaged (wheel) install with no override:
`%ProgramData%\exomem\logs` on Windows (machine-wide, since the service
commonly runs as `LocalSystem` while the `exomem` CLI runs as the logged-in
user), `~/Library/Logs/Exomem` on macOS, `$XDG_STATE_HOME/exomem/logs`
(falling back to `~/.local/state/exomem/logs`) on Linux. `exomem doctor`
reports the resolved path under the `observability` check's `log_dir` detail
(`--json`), so a misresolved directory is never silent. Every file below
resolves through this SAME function — none of them may compute their own
directory independently.

| File                     | Contents                                                     |
| ------------------------ | -------------------------------------------------------------- |
| `exomem.log`             | Server-process structured JSONL log (one line per record).     |
| `exomem-cli.log`         | One-shot CLI invocations (`doctor`, `status`, product ops, …).  |
| `exomem-media.log`       | The media worker child process.                                |
| `queries.jsonl`          | One record per `find()`/`search_memory` call.                      |
| `writes.jsonl`           | One record per note/add/replace write.                          |
| `reads.jsonl`            | One record per `get()` read.                                     |
| `mutations.jsonl`        | One record per mutation attempt (the mutation journal).          |
| `ledger.jsonl`           | One record per **MCP tool call** — the call ledger (below).       |
| `ledger-archive/`        | The call ledger's rotated segments, content-addressed.           |
| `ledger.gap`             | Empty; its mtime marks the latest call whose row failed to append. |
| `archive/exomem-*.log`   | Prior sessions' `exomem.log`, archived (not deleted) on restart.  |

Each JSONL file rotates at a size cap (`EXOMEM_JSONL_MAX_MB`, default 64MB),
keeping exactly one prior generation (`<file>.1`). `usage.py` and `exomem
trace`/`exomem logs` read both the live file and that generation. `ledger.jsonl`
rotates differently — see below.
`exomem.log`/`exomem-cli.log`/`exomem-media.log` use Python's standard
`RotatingFileHandler` (`EXOMEM_LOG_MAX_MB` default 5MB, `EXOMEM_LOG_BACKUPS`
default 5).

Windows cannot share a single `RotatingFileHandler` across processes, which is
why the server, CLI, and media worker each get their own file instead of one
shared `exomem.log`.

## Derived graph recovery

Graph status is typed: `current`, `recovery_required`, or `unavailable`.
Relation-queue reads project that state as `available`, `warming`, `pending`, or
`unavailable`, rather than serving an old graph as current. A registry-only
change may complete by rebinding the existing sidecar when its source proof
remains valid; if that proof is absent, the same durable recovery path uses a
rebuild. The public write terminal reports successful convergence as
`graph_sync="completed"`; graph status reports whether the derived graph is
current.

Readiness and coordination status add a fourth state, `unproven`: the sidecar
passed every cheap check, but it was inherited from an earlier process and
nothing has yet run the source-bytes proof for its current identity. Status
probes never run that proof themselves. The first reader that needs it -- the
graph drain or a graph recall -- runs it once and remembers the verdict; only
one proof runs at a time. A recall that arrives while another reader's proof is
running serves without the graph lane for that request and reports `graph` as
degraded.

Use `maintain_memory(mode="reconcile", rebuild_graph=true)` for an unavailable
derived graph lineage. This recovery concerns rebuildable sidecar state only;
it neither changes Markdown nor selects, promotes, deprecates, or authors a
relation.

### Which repair a write chose, and why

A committed write that leaves graph work behind reports it as `graph_sync`
`pending` with a code: `GRAPH_SYNC_REPAIR_QUEUED` when the affected pages are on
the durable queue and a drain will converge them, `GRAPH_SYNC_REBUILD_IN_PROGRESS`
when a whole-vault pass is running. Both are healthy. Neither requires rereading
the written note or running maintenance.

Behind that terminal the dispatch chose one of six repairs, and the log says
which:

- `graph dispatch routed an unreadable predecessor to incremental repair` — the
  sidecar could not be read at that instant but proved nothing against itself.
  The write takes the incremental path, and the affected pages go to the durable
  queue if it cannot finish; the dispatch outcome is
  `graph_repair_unreadable_predecessor`. No whole-vault pass runs.
- `graph incremental refresh fell back reason=resolver_snapshot_unavailable` —
  this process holds no resident recall resolver for the checkpoint, which is a
  cold cache rather than evidence about the graph. The recall delta was already
  proven complete, so the pass queues it and a later drain re-runs the same
  repair with a resolver resident; the dispatch outcome is
  `graph_repair_cold_resolver`. No whole-vault pass runs. A run of these means
  start-up never primed the resolver — check for the `graph snapshot adoption`
  line in the warm-up.
- `graph incremental refresh deferred reason=external_event_covers_these_paths` —
  an unattributed edit covers the very paths this write touched, so the write
  cannot trust its own view of them. It defers, queues exactly those paths as
  durable graph repair, and the dispatch outcome is
  `graph_repair_external_pending`. No whole-vault pass runs, and the drain
  converges the paths whether or not the watcher's own repair lands first. The
  same line with `unscoped=True` is the watcher's fail-closed default, which
  names no affected set at all; that one still rebuilds, and the rebuild line
  below says so.
- `graph dispatch routed a receipt-covered lineage gap to incremental repair` —
  the sidecar's acknowledgement is behind this write's checkpoint, and every
  generation it skipped is already on the durable repair queue (the preceding
  `graph lineage gap is covered by durable receipts` line says how many). The
  gap is real; the claim that nothing is converging it is not. The write takes
  the incremental path and queues its own paths; the dispatch outcome is
  `graph_repair_covered_gap`, and the acknowledgement is never advanced on a
  promise. No whole-vault pass runs.
- `graph dispatch routed a covering acknowledgement to incremental repair` — the
  sidecar already acknowledges this exact checkpoint, and only its availability
  marker is withdrawn. The watcher's echo of a write that a drain already
  repaired is the usual source. A background caller, such as the watcher, queues
  the paths and reports `graph_repair_queued`. No whole-vault pass runs.
- `graph dispatch left an unproven predecessor to the standing full marker reason=…`
  — the sidecar's acknowledgement is not this checkpoint's predecessor, but a full
  marker already owes the whole-vault pass. A request or a background caller queues
  its paths, advances the marker over its generation and reports
  `graph_repair_queued`. The drain pays the marker once. The first writes after a
  worker replacement usually take this route.
- `graph dispatch registered a whole-vault rebuild reason=…` — a proven verdict:
  `graph_sync_predecessor_mismatch` or `…_absent` (the sidecar's acknowledgement
  is genuinely not this checkpoint's predecessor) or `graph_sync_snapshot_unusable`
  (no sidecar, a schema or relation-registry drift, a corrupt checkpoint). The
  reason `graph_sync_acknowledgement_covers` is a standalone caller in the state
  above: it must see a converged graph, so it still rebuilds.

A run of whole-vault rebuilds with an *unreadable* reason is the defect
`seamless-managed-worker-handoff` removed; a run of them with a mismatch or
unusable reason is a real lineage problem and belongs in reconcile.

Filesystem events the process cannot attribute to itself are recorded per path.
Any unrepaired external path still fences every read that requires a current
projection — relation-filtered recall reports `warming` rather than serving stale
edges — while a governed write on an unaffected path takes the incremental path
as usual. `freshness.snapshot()["external_pending_paths"]` names what is still
unrepaired.

When an incremental pass reports `stored_resolver_entries_unreadable`, the
preceding `graph stored resolver proof declined` record identifies its proof
branch. The reason distinguishes retained deletions, a caller create outside
the delta, a changed path outside the knowledge base, a missing row without
create proof, and an existing row with create proof. The record contains only
numeric counts; it emits no paths, titles or source text. These are provenance
failures, not evidence of a SQLite read error. A later sidecar snapshot cannot
identify which branch a historical attempt took.

## Event schema

A structured log record (`src/exomem/log_events.py`) carries:

- `event` — a machine-readable name from a fixed catalog (`tool_start`,
  `tool_success`, `tool_failure`, `rest_failure`, `hosted_call`,
  `http_request`, …).
- `fields` — always content-free: tool names, codes, durations, scope kinds,
  request ids.
- `content` — present only for events the catalog explicitly allows to carry
  it (e.g. `tool_failure.content.message`, truncated to 300 characters).

In a hosted cell, a structured record for a cataloged event has its `content`
dropped and keeps the content-free `event`/`fields` skeleton; any other
(non-`log_event()`) record keeps today's full-blanking, fail-closed behavior.
`exc_info` is always stripped hosted, structured or not.

## Correlation

Every HTTP request gets one `x-exomem-request-id` (extracted from the inbound
header if UUIDv4-shaped, else minted), injected as both a request header (so
downstream MCP tool code resolves the same id) and a response header. The
access log (`event=http_request`), the tool trace
(`tool_start`/`tool_success`/`tool_failure`), `queries.jsonl`/`writes.jsonl`/
`reads.jsonl` (additive `request_id` field), and `mutations.jsonl` all carry
it, so one request id joins every source that touched that request.

The access event keeps `duration_ms` as the inner ASGI application await.
It excludes earlier authentication and body handling, plus its own logging and metrics.
Two additive fields describe the worker boundary:

- `worker_pre_access_ms` measures worker middleware entry through the existing access timer start.
- `worker_to_final_body_ms` measures that entry through completion of the final body send await.
  A successful final send remains known if later cleanup fails.

The entry stamp precedes FastMCP authentication and carrier handling.
The server error middleware and configured body limit can remain outside that stamp.
Missing entry wiring or an unfinished final send produces `null`, never zero.
Absent fields in older records also mean unknown.
Early refusals before access still produce no access event.

These intervals overlap; do not add them.
Final body timing excludes trailers, later cleanup, managed forwarding and client receipt.
A streaming response measures stream lifetime when its final send completes.
Compare finite MCP calls separately from streaming responses.

## The call ledger (`ledger.jsonl`)

One durable, structured row per **MCP tool call** — read or write, success or
refusal or error. It is the only source that covers every call: `queries.jsonl`,
`writes.jsonl`, `reads.jsonl`, and `mutations.jsonl` each cover one family, so a
tool that is none of those leaves no structured trace anywhere else. And it is
the only one that records **which client called**.

Read it with `exomem logs --file ledger`, join it with `exomem trace`, and check
it with `exomem logs verify`.

### What a row carries

`schema_version`, `sequence`, `prev_hash`, `row_hash`, `ts_utc`, `request_id`,
`session_id`, `client_name`, `client_version`, `transport`,
`caller_principal_hash`, `principal_kind`, `tool`, `arg_names`, `args`,
`target_paths`, `outcome`, `error_code`, `duration_ms`, `total_ms`,
`request_bytes`, `truncated`, `access`, `item_count`.

- **Latency, on every call — read, write, success, refusal.** `duration_ms` is
  the tool leaf, the number the prose trace and `exomem_tool_duration_ms` have
  always reported. `total_ms` is server-wrapper wall time, including the content
  guard and argument normalization, which run *before* the leaf clock starts.
  It excludes connector transit and model planning; those require a client
  trace. When the two diverge, the gap identifies wrapper overhead outside the
  leaf, not necessarily admission alone. `request_bytes` is the total
  serialized argument size, so a slow call is interpretable rather than merely
  slow.

  For a multi-call workflow, `scripts/durable_closure_ledger.py` reports both
  summed execution and interval-union occupancy. Time between calls remains
  unattributed without a client trace. See the
  [durable-closure investigation](benchmarks/durable-closure-investigation-2026-09.md).

- **`outcome`** is `ok`, `abstained`, `refused`, or `error`. `refused` is a governance
  refusal — the tool wrapper returns an error *envelope* rather than raising, so
  control flow alone cannot distinguish it from success; the ledger reads the
  wrapper's per-call breadcrumb to tell them apart. `error` is an uncaught
  exception. `error_code` is the refusal's `OpError.code`, or the exception
  class. A completed activation records `abstained` only when the final served
  packet has strict `abstained: true`. Its error code is null. Refusals and
  exceptions take precedence. A non-abstained budget-limited packet remains
  `ok`; this outcome does not certify usefulness, completeness or relevance.
- **`activation_result`** is an optional schema-1 observation for activation.
  It carries strict booleans `abstained` and `budget_limited`, plus a bounded
  `reason`: `ambiguous`, `unresolved`, `index_warming`, `unavailable`, `disabled`,
  `withheld`, or null. Unknown reasons become null; invalid booleans omit the
  block. Strict abstention still determines the outcome when metadata is invalid.
  The wrapper observes the final postfiltered packet and retains no material.
  An absent block means unknown server activation semantics. Historical rows
  and hashes stay unchanged. Timing joins can match historical activation `ok`
  with null error to client `ok` or `abstained` with null error. Readers keep
  those client facts separate from the raw server outcome and unknown semantics.
  Current disagreements and failure mismatches still invalidate the join.
- **`client_name` / `client_version` / `transport` / `session_id`** come from
  the MCP initialize handshake and are recorded **in the clear**: they identify
  software, the same class of value as a `User-Agent`. This is what makes one
  client's calls separable when several are connected to one vault at once. It
  is client-declared, so treat it as a diagnostic hint, not an authorization
  input. `caller_principal_hash` is the separate, hashed, authenticated
  identity. A stateless request carries no handshake; there the name comes
  from `User-Agent`. On a cloud cell, the gateway's `x-exomem-oauth-client`
  header comes first, because the gateway forwards no `User-Agent`.
- **`principal_kind`** says which kind of caller that is: `owner` (stdio, the
  CLI or the REST key), `owner-oauth` (a remote sign-in the host bound as the
  owner with `EXOMEM_OWNER_OAUTH_SUBJECT`), `principal`, or `unresolved`. An
  `owner-oauth` row keeps the remote identity's `caller_principal_hash`, so an
  owner action taken from a remote connector stays traceable to that door.
- **`args`** is `{name: {len, sha256}}` and **never a value**. Note bodies,
  query text, and credentials are reduced to a length and a hash by
  construction, not by a downstream filter — `privacy_log`'s process-wide
  redactor is gated on `EXOMEM_HOSTED_CELL` and is off for local installs.
  Identical arguments hash identically on purpose: that is what answers "is this
  client retrying the same call?".
- **`target_paths`** records the page a call addresses, verbatim. A path is
  structure, not content, and it is the first thing a forensic pass needs.
- **`access`** and **`item_count`** arrive with schema version 2. `access` is
  `read` or `write`, as the dispatcher classified the call, or null when no
  classification reached the row. `item_count` counts what a successful call
  touched: the distinct items a read released, or the distinct pages a write
  committed. The dispatcher reports the pages a commit names, so a write whose
  destination the server chose still counts. Only when a commit reports no page
  does a write count the distinct targets its arguments name; the two are never
  added, because an argument can name a committed page by its reference. It is null
  for any other outcome and for a write that names no page. Null means
  unknown, never zero. Both fields carry no content, so a cloud cell keeps
  them. A version 1 row lacks both; readers treat them as unknown, and
  `exomem logs verify` accepts a chain that runs from version 1 into version 2.

### Where a write's time went (`spans`)

A row carries `spans`: named phases of that one call, slowest first, each with
a `count` and a total `ms`. They exist because two boundary clocks can prove a
call was slow and locate nothing between them — a live `edit_note` once
recorded `total_ms=24,394` with `boundary_hold_ms=3,348` and no account of the
other 21 seconds.

The names are stable and are the vocabulary a latency diagnosis uses:

| Span | What it covers |
| --- | --- |
| `corpus_context.build` | Semantic corpus context for the write. |
| `derived.canonical_commit` | Staging through canonical bytes landing on disk. |
| `derived.canonical_to_committed` | Canonical bytes landing through the mutation row reaching `canonically_committed` — the derived fan-out and terminal bookkeeping that used to be unattributed. |
| `derived.fanout`, `derived.terminal_persist` | The two halves of that umbrella: the derived work itself, then the hooks and the row write that follow it. They partition it, so a large `canonical_to_committed` says immediately which half to read. A near-zero `derived.fanout` means the write took the fast-ack route and its derived work is reported under `derived.acknowledgement` instead. |
| `derived.deferred_index_store` | The durable-defer arm of the semantic/embedding dispatch, which had no span: a write that took the cheap path used to look, in a row, like a write that did nothing. |
| `index.path_partition`, `index.semantic_states`, `index.policy_revalidate`, `index.corpus_publish`, `index.semantic_purge`, `index.path_custody` | The steps of `index.upsert_after_write` that run outside any component: the policy partition, parent-state resolution, the two policy re-proofs, the corpus publication, the raw-Record purge, and the read-side path custody. They existed unnamed, and `index.upsert_after_write` reported their cost with nothing to attribute it to. |
| `index.self_write_registration`, `index.graph_epoch_handoff` | The fan-out driver's own two steps, between the canonical mark and `index.upsert_after_write`. |
| `advisory.best_cosine` | The advisory's cosine sweep. On the encoding surface `texts`/`chars` say what was encoded. A note write's post-commit sweep adds `reused`: how many draft chunks took the vector its own commit had just published instead of being encoded again, so `texts=0 reused=N` is a sweep that encoded nothing. On every span `reused` counts only chunks whose vector came from the page's own published rows. `recalled`, disjoint from `reused`, counts draft chunks that took a vector a sweep had encoded moments before for text no row holds: an edit's sweep of the page's previous paragraphs. A sweep that recalled everything it did not reuse reads `texts=0` with `reused` plus `recalled` equal to its chunk count. `texts=0` with a `vectors` count and no `chars` is a surface that reuses published vectors and encodes nothing: the deferred write advisory, and the recall pack's proximity pass, which adds `pages` for how many packed pages had exact stored rows. |
| `advisory.overlap_groups` | Grouping and emitting that sweep's candidates — a ref batch and a review-state read per candidate. |
| `preflight.contract_eval`, `preflight.corpus_context`, `preflight.page_states`, `preflight.read_guarded`, `preflight.registries`, `preflight.relation_review`, `preflight.validity_token` | The write's preflight stages, from the per-stage collector (`MutationTimings`). |
| `commit.boundary_acquire`, `commit.creation_lock`, `commit.embedding_prewarm`, `commit.locked_commit`, `commit.manifest`, `commit.resolver_prime`, `commit.revalidate`, `commit.stamp_check` | Its commit stages, from the same collector. These are emitted into every row regardless of `EXOMEM_WRITE_TIMINGS`; that flag governs only whether the *caller* is handed a timing envelope on its response. |
| `writer.resolver_snapshot` | The wikilink resolver a writer prepares its draft with. One field names the route: `forked` from the warm resolver cache, `corpus_entries` from the current corpus context, or `disk`, a full read of the vault's titles. When the corpus route declines, one `declined_<reason>` field says why: `external_pending`, `events_not_live`, `checkpoint_absent`, `freshness_differs`, `corpus_absent`, `checkpoint_differs`, `proof_failed`, `identity_changed`, `changed_during_proof` or `unkeyable`. |
| `preflight.creation` | A page creation's preflight, before the commit. `structural_ms` is its structural evaluation and `relation_review_ms` its relation review when the page needs one, both in whole milliseconds; the commit reuses that relation review while its validity stamp stays current. |
| `commit.source_closure`, `commit.relation_review_prepare`, `commit.catalog_prepare`, `commit.relation_review`, `commit.relation_review_attempt` | A page creation's commit stages, recorded directly. Before the guard: the source closure, the relation-review preparation and the model warm-up (`commit.embedding_prewarm`). Inside it: the catalog preparation and the relation-review commit. `commit.relation_review_attempt` is the commit's in-lock revalidation, with one field for its outcome: `reused`, `revalidated` or `unavailable`. `commit.relation_review_prepare` carries the same field for the preliminary attempt before the guard, which is offered the preflight's relation review: `reused` means the commit did not repeat it. |
| `post_commit.structure_suggestion`, `post_commit.records_routing`, `post_commit.observation_delta`, `post_commit.records_delivery`, `post_commit.due_state`, `post_commit.capture_sweep`, `post_commit.entity_candidate` | The advisory blocks a write attaches after its commit, one span each, for a page creation and an existing-page commit alike. |
| `advisory.default_sweep` | The whole default duplicate and overlap sweep that a write runs after its commit. `advisory.best_cosine` and `advisory.overlap_groups` are parts of it. |
| `derived.receipt_prepare`, `derived.receipt_proof`, `derived.acknowledgement`, `derived.pending_visibility` | The receipt and acknowledgement path. |
| `index.upsert_after_write` | The whole derived fan-out, and the sum the per-component spans below break down. |
| `index.completion_check` | Verifying full-index completion after component dispatch, including current publication state. `paths` counts the written paths checked. |
| `index.full_refresh_store` | Persisting durable full-index refresh work when completion is incomplete or dispatch raises. `paths` counts the written paths submitted for repair. |
| `index.memory_refs`, `index.resolver`, `index.lexstore`, `index.epistemic_graph`, `index.embeddings` | One per derived component, recorded at the shared dispatch seam. |
| `graph.refresh_paths` | The graph's incremental pass inside `index.epistemic_graph`. |
| `lexical.rebuild_atomic` | A whole-corpus lexical rebuild. |
| `lexical.connect`, `lexical.connect.guard`, `lexical.connect.sqlite`, `lexical.connect.owner_family` | One ordinary lexical-store read connection, and three of its parts: entering the reserved-identity guard, the SQLite open with its pragmas, and the owner-family write. The rest of `lexical.connect` is the owner-target scope and the guard's exit. `lexical.connect.sqlite` also counts setup connections, which `lexical.connect` does not wrap. |
| `lexical.readiness`, `lexical.proof`, `lexical.query` | A catalogue read's readiness verdict, its re-proof inside the pinned read transaction (schema, checkpoint, policy and catalogue identity), and the query itself. `lexical.proof` also covers capturing a selection's read view and the resolver-entries proof. |
| `lexical.resolver_entries` | Reading a scope's resolver entries from the catalogue; `rows` counts the entries returned. |
| `recall.projection.checkpoint`, `recall.projection.snapshot`, `recall.projection.scope_snapshot`, `recall.projection.relativize` | The recall projection when no admitted catalogue view answers it: choosing the checkpoint, copying the live registry's projection (`snapshot`) or the offline scope snapshot (`scope_snapshot`), each with `rows`, and turning the entries into vault-relative paths. |
| `embeddings.model_load` | Lazy load of the embedding model weights. |
| `embeddings.encode` | Encoding text to vectors. |
| `embeddings.matrix_load` | A full vector-matrix load from the sidecar; the log line `embedding matrix full load: … rows=… cached_gen=…` names the reason. |
| `embeddings.matrix_load.policy` | Read the recall policy identity. |
| `embeddings.matrix_load.connect` | Open the sidecar, including identity coordination and connection setup. |
| `embeddings.matrix_load.snapshot` | Begin the snapshot and read its generation, vector space and row count. |
| `embeddings.matrix_load.allocate` | Allocate the final matrix and metadata list. Physical pages can be allocated later during row copying. |
| `embeddings.matrix_load.rows` | Read, validate and copy vector rows. Fields report `expected_rows`, completed `rows`, `width_sum` and copied `vector_bytes`, including failed loads. |
| `embeddings.matrix_load.release` | Sum transaction rollback and connection close. |
| `embeddings.matrix_load.finalize` | Observe the snapshot identity, read the sidecar timestamp and construct the cache. |
| `embeddings.disk_score.connect` | Open the sidecar for the bounded, read-only `service-v1` chunk scan. |
| `embeddings.disk_score.snapshot` | Begin the snapshot, read its generation and vector space, prepare queries and check the producer space. |
| `embeddings.disk_score.fetch` | Execute the ordered vector query and fetch each block, including the final empty fetch. |
| `embeddings.disk_score.decode` | Allocate each block, validate vector widths and copy vectors. Fields report completed `rows` and copied `vector_bytes`, including failed scans. |
| `embeddings.disk_score.score` | Compute each block's matrix product. |
| `embeddings.disk_score.select` | Apply admission, select each query's winners and merge them into the stable top-k. |
| `embeddings.disk_score.hydrate` | Build results and, for searches that return text, read winning chunks from the scoring snapshot. |
| `embeddings.disk_score.release` | Roll back the read transaction and close the connection. |
| `embeddings.matrix_catch_up` | The bounded alternative to that full load. |
| `embeddings.unit_matrix_load` | A full load of the semantic-unit vectors into the resident unit matrix: the first unit query in a process, and the first after each unit write; the log line `semantic-unit matrix full load: … rows=…` names the reason. |
| `delivery.vocabulary_after_commit` | Vocabulary delivery after the commit. |
| `derived.advisory_execute`, `derived.component_dispatch`, `derived.component_completion` | The derived drain. |
| `recall.*` | Retrieval phases, named by `find`'s own timings. |
| `recall.semantic.search` | The semantic entry and any separate deferred completion, aggregated under the existing name. `entry_ms` records the entry call, including inline completion. `finish_ms` records only a separate deferred completion. Both fields use whole milliseconds, rounded down per interval, and remain absent until their phase starts. |
| `recall.semantic.preflight` | Lazy imports, registry bases, snapshot setup and catalogue readiness before semantic candidate collection. This interval is inside `recall.semantic.search`. |
| `recall.due_state` | Serving the advisory due-state block a recall carries. On a memo hit its re-checks are `recall.due_state.verdicts` (`paths` = release verdicts re-asked), `recall.due_state.role` and `recall.due_state.exists` (`rows`); on a miss, `recall.due_state.build`. When the key matches no build but an older build of the same audience exists, `recall.due_state.reuse` (`rows`) cuts that build to what the projection and review state still hold, and no build runs on the request. |
| `recall.due_state.emit` | Deciding whether that block is new to the session and recording its delivery. |
| `recall.working_set.carry.phrase`, `recall.working_set.carry.title` | An activation's retrieval carry, one span per rung inside `recall.working_set.carry`: the phrase rung, which names pages by a phrase of distinctive words, and the title rung, which lists pages whose title the turn says. Fields: `admitted` (the request's admitted pages), `stem_rows` (admitted catalogue rows its per-word rarity counts examined, summed over the words), `candidates` (rows its corroboration test examined) and `stats_rebuilt` (1 when the rung rebuilt ranking statistics over the admitted pages instead of reusing them; summed over calls). An owner whose configuration can withhold no page (no governance policy, collection-store marker or lifecycle marker) ranks on the catalogue's own statistics, so its `stats_rebuilt` stays 0. A restricted reader, and the owner under a governance policy, ranks on its admitted pages and rebuilds once per catalogue revision, whether or not any page is withheld. |
| `recall.working_set.lanes.<position>` | One activation role lane, named by its position, never by its role. Fields: `rows_read` (rows the lane's maintained-catalogue reads returned), `pages_parsed` (unit-parent pages it read from Markdown and parsed), `catalogue_ms` and `hydration_ms` (whole milliseconds inside those two reads, rounded down). The rest of the lane's duration is its own selection work. |
| `runtime.gc` | Garbage-collection pauses inside one `recall_context` call: the duration is the pause time and `collections` the number of collections. A collection stops every thread, so the pauses counted are those inside the call's interval, whichever thread triggered them. Absent when no collection ran. |
| `command.leaf`, `command.postfilter` | The dispatcher envelope, then the MCP-layer filter and scrub. The dispatcher envelope includes its command body, first terminal filter and disclosure receipt. |
| `command.body`, `command.terminal_filter`, `command.disclosure_receipt` | Dispatcher stages on the read-only and the mutation path: invoke the command, filter its result or error, then, on a read, emit the disclosure receipt. Failed stages retain their duration. |
| `mutation.idempotency_claim` | Keyed claim attempts in the command body, including SQLite commit and close. Excludes capture queue waits, pending-replay polling, and the mutation operation. Wall and thread CPU totals aggregate repeated attempts. |
| `command.refusal_floor` | A restricted caller's direct-read refusal, held to the fixed disclosure floor. `work_ms` is the refusal's work, from dispatch to the refusal, rounded up to whole milliseconds. `overrun` is 1 when that work alone ran past the floor, so the refusal completed late, and 0 otherwise. `deferred` is 1 when the MCP or REST surface waits out the floor after its own handling of the refusal; the span's duration is then near 0. Otherwise the dispatcher's thread waits, and the duration is that wait. Absent on successes and on owner refusals. |
| `egress.cross_check`, `egress.scrub` | The terminal filter's withheld cross-check and credential scrub, beside `egress.artifact_gate`. On MCP the filter runs twice, under `command.terminal_filter` and again under `command.postfilter`, so each sums both passes. A raised error has no cross-check; its filter runs the gate and the scrub once per text value. |
| `spans.overflow` | Every span recorded under a name past the per-call name bound, summed: how many and how long. Present only when a call ran out of names. |
| `egress.artifact_gate` | Artifact-gate construction and payload filtering, including failures. Fields report observed `thread_cpu_us`, measured `thread_cpu_calls`, and vault census `index_calls`. |
| `egress.artifact_gate.tombstones` | Capture the lifecycle tombstone set once for this gate pass. Each later pass captures a fresh set. |
| `egress.artifact_gate.index` | Build the artifact census and tombstone aliases when this pass needs to resolve free-text references. |
| `lexical.publication_wait` | Time a request spent waiting for the lexical publication barrier, with `timeout_ms` (the bound it chose) and `acquired` (1 or 0). Recorded only on the MCP path. |
| `recall.pack.parents`, `recall.pack.units`, `recall.pack.neighborhood`, `recall.pack.tension` | The deep pack's phases: reading the packed parents, extracting claims and packing semantic units, the one-hop wikilink neighbourhood, and supersession plus proximity tension (the sidecar reads). `recall.graph_enrich` remains the fifth. |
| `encode.by.<module>` | Beside every `embeddings.encode`: the Exomem module that asked for the encode (`context_pack`, `semantic_segments`, `index_sync`, ...). The name is the attribution because span fields are integers. |
| `index.embeddings.reuse` | Inside `index.embeddings`, once for the page's chunks and once for its semantic units: `texts` the write needed vectors for and `reused` how many came from the page's published rows instead of the encoder. `reused` near `texts` is an append; `reused` 0 on a long page is a first index or a rewrite. `reused` counts only texts whose vector came from the page's own published rows, the same meaning as on `advisory.best_cosine`. `recalled`, when present and disjoint from `reused`, counts texts that took an advisory sweep's just-encoded vector instead: an `add` commit taking the vectors its pre-commit sweep encoded. The encoder was handed `texts` minus both. |
| `read.page`, `read.history`, `read.links` | Decode the prepared page, read the edit log (`entries`), and build the wikilink summary (`inbound`, `outbound`). Page preparation precedes `read.page`. |
| `read.resolve_identifier` | Resolve the requested page identifier. `pending_custody_unknown` counts refusals caused by unprovable custody. `pending_identity_unknown` counts refusals because pending identities cannot prove uniqueness. These counters describe only this phase. Missing counters leave the branch unknown. |
| `memory_refs.identity_census` | Resolve the requested page identifier through the current identity projection, or a full KB scan. Vault state chooses the source before identifier lookup. `snapshot` is 1 for projection reuse and 0 for a scan. `cpu_ms` records thread CPU time; `pages` records readable pages after a scan. A read request also takes the projection while the file watcher holds an edit it has not yet published. One `declined_<reason>` counter names an acquisition failure or revalidation failure. Fields sum across calls and contain no paths or identities; an absent decline counter leaves the cause unknown. |
| `read.prepare`, `read.reference`, `read.release` | Acquire the page snapshot, resolve its stable reference and pending custody, then apply its release policy. These phases sit outside `read.page`; their durations include any waits. For a restricted caller, `read.release` also runs before `read.history` and `read.links` to refuse a withheld page early, so a read that is not refused counts it twice. |
| `read.prepare.resolve_path` | Check the literal and shortcut paths, then resolve the canonical vault path. Exclusion checks follow this phase. |
| `read.prepare.resolve_physical`, `read.prepare.inspect`, `read.prepare.held_read` | Resolve the physical spelling, inspect its identity, then acquire the held byte snapshot. These intervals include nested identity work. |
| `reserved.identity.baseline_build`, `reserved.identity.baseline_wait` | Build the cold baseline catalogue, or wait on another caller's build. These spans carry no flight or holder identity. A background builder without a call token emits no request span. |

Spans are aggregated by name within a call, so a phase entered once per changed
path reports a count and a total rather than hundreds of rows. Instrumentation
never raises. A missing span leaves that phase's duration unknown.
Nested intervals overlap; do not add them to enclosing spans.
A wait span does not identify its leader or guard holder.
Missing baseline spans do not prove a cache hit.

Artifact-gate CPU is partial when `thread_cpu_calls` is less than the span's
`count`. An unavailable clock contributes no CPU sample. Both filtering passes
share this aggregate; it does not identify their separate costs.

Some spans also carry `fields`: named integer counts beside the duration. A
duration alone cannot separate a slow step from a large one — `embeddings.encode`
at 15.6 s with `texts=1` and at 15.6 s with `texts=400` are different defects
with different fixes — so the spans that cover per-item work report how many
items they covered (`paths` on the `index.*` components, `texts`/`chars` on
`embeddings.encode` and `advisory.best_cosine`, `candidates` on
`advisory.overlap_groups`). The key is absent on a span that measured only time,
so nothing about an existing row changes.

An `embeddings.encode` span also reports `execution_wait_us` for execution-context
entry and `model_encode_us` for the `model.encode` call. Each field sums attempted
intervals across batches and calls, including failures. Unattempted phases leave
their fields absent. Encoding includes tokenization, inference and normalization;
entry time does not identify a gate holder or prove contention.
These fields survive truncation when the enclosing encode span is retained.

An `embeddings.matrix_catch_up` span reports `prepare_us` for connection setup,
identity coordination, the decision snapshot, vector decoding and connection release.
It reports `splice_us` for constructing fresh metadata and matrix containers.
Both fields sum attempted intervals, including failures. Unattempted phases remain absent.
Prepared eligible attempts also report changed `paths` and expected matrix `rows`.
These counts describe the replacement workload; field presence does not prove successful publication.
The span can cover a refused or failed attempt before a full reload.

During MCP calls, spans opened with `call_spans.span` and recall stage spans
also carry `thread_cpu_us` and `process_cpu_us`, including spans that raise.
Spans recorded directly, such as `egress.artifact_gate` and
`lexical.publication_wait`, carry them only when their caller measured them. `thread_cpu_us` measures CPU on the executing thread.
`process_cpu_us` measures CPU of the whole process over the same interval; it
does not name the thread that used it, and it can read slightly below
`thread_cpu_us`. Wall time far above both clocks is a wait on a lock, I/O,
SQLite or another process. Process CPU far above thread CPU means other threads
ran meanwhile; it does not prove they blocked this one.
Nested stage measurements overlap; do not add them. These fields do not change
public wall-time diagnostics. Outside MCP, recall does not sample this CPU clock.
Missing CPU fields leave consumption unknown.

The existing retrieval-refusal log names its validated site and the bound MCP
`request_id`. Direct calls and invalid request IDs report `unbound`; they do not
mint an ID. Match a valid ID to the call ledger before attributing a refusal site.
A nearby log timestamp alone does not bind the site to a call.

Matrix-load child spans break down the outer load. They do not include the caller's
lock wait or vector scoring. Snapshot and release each aggregate two intervals.
The ledger retains the 32 slowest spans. A row that dropped any carries
`spans_truncated`, the number it dropped; a complete row has no such key. An
omitted phase has no retained duration. A call that records more distinct names
than that bound sums the rest under `spans.overflow`, its count and time.
The row fields sum across loads, so `width_sum` equals the vector width only for one load.
A slow row phase alone does not distinguish storage, row conversion, copying or
memory pressure.

Disk-score spans describe chunk scans that keep vectors in bounded blocks rather
than a resident corpus matrix. Repeated phases aggregate within the call. Decode
fields sum completed work across blocks and scans, including repeated scans for
query blocks. They carry only integer counts, never paths, chunk text, queries or
identifiers. These phases can sit inside broader search or advisory spans, so
overlapping durations must not be added blindly. The same 32-span retention limit
applies: an omitted phase means unknown duration, never zero.

Governed walks reuse membership only for the same canonical path, vault, exact
source bytes, policy fingerprint and current registry assignment. A valid hit
skips Markdown parsing. Each walk still checks source freshness, tombstones,
content floors, projection authority, grants, sessions and bridges. Each fresh
carry still admits the whole non-raw corpus. Reuse depends on resident entries
in the bounded memo. A corpus walk larger than that memo can still parse every
page. This reuse does not establish a live latency improvement.

### Mutation-lock events dominate the log under writes

`mutation_lock_acquired`/`released` are logged per boundary acquisition, and the
derived drain re-enters the boundary once per claimed `(batch, component)` pair.
One busy write window on the personal service produced 758 of these lines —
`reserved_identity:deferred-index-store` (400), `refs-store` (246), `gate` (62),
`embeddings-store` (20). At the default rotation of 5 MB with 5 backups, that
window rotated the *earlier* part of the same incident out of the live file
within minutes, which is how a first write's evidence was lost while it was
still being diagnosed.

These lines are not per-call: they carry `operation`, `holder_kind` and
`wait_ms`, not a request id, and the drain that produces most of them runs on a
background thread, so a burst next to a slow write is not necessarily that
write's. Two knobs, neither changed by default:

- `EXOMEM_LOG_MAX_MB` (default `5`) and `EXOMEM_LOG_BACKUPS` (default `5`) size
  the rotation. Raising `EXOMEM_LOG_MAX_MB` to `50` before reproducing a latency
  incident keeps the whole window in one file.
- Setting the `exomem.mutation_lock` logger to `WARNING` drops the routine
  acquisitions while keeping the long-holder warnings, which is the right trade
  when the boundary is not what you are investigating.

`lease_acquired` logs at INFO when the grant changes the fencing token (a first
grant, a takeover or a re-grant after idle release) and at DEBUG when it
repeats the held token, because a governed write batch grants the writer lease
thousands of times an hour. `lease_reclaimed`, `lease_renew_rejected`,
`lease_idle_released` and `lease_released` stay at INFO. A mutation-lock hold
from a command or lifecycle boundary keeps its INFO audit pair. A reserved-state
or machine-frequency hold (`background`, `graph`, `derived-worker`,
`collection-read`) logs at DEBUG
when its acquire waited less than one poll interval (25 ms by default) and its
hold took less than 250 ms; `mutation_lock_hold_summary` counts these. A hold
that waited, took 250 ms or more, ran overdue or was refused logs at INFO or
higher. A boundary with no declared holder kind, such as `unknown`, also keeps
its INFO pair.

Two log lines close loops the spans cannot: `lexical deferred upsert retry
completed paths=… outcome=…` says what became of a lexical upsert the
publication barrier was too busy to take, and `file watcher: startup graph
validation …` says whether seed-time validation waited out a busy boundary or
gave up on it.

### Integrity

Rows carry a monotonic `sequence` and a `prev_hash`/`row_hash` chain (genesis =
64 zeros), so a dropped, reordered, or edited row is detectable rather than
silent. A process restart resumes the chain from the live file's last row —
`sequence` never resets.

```
exomem logs verify
```

Walks the archive and the live file as one chain, **anchored** to the genesis
row, and exits non-zero listing every break. The anchor is what catches a
dropped *oldest* segment: without it, nothing precedes the first surviving row
to contradict it and the chain merely appears to start later than it did.

The append does not fsync. It sits in every call's critical section and costs a
median 0.35 ms / p99 0.72 ms per row (Windows, 4 KB argument) — ~0.05 % of a
write, ~3 % of a read. Rows lost to a hard crash show up as a `sequence` gap,
which is exactly the visibility that makes the unsynced append safe.

### Rotation

Unlike `queries.jsonl` and its siblings, the ledger keeps a bounded live file:
past `EXOMEM_CALL_LEDGER_ROTATE_BYTES` (default 8MB) the oldest rows beyond
`EXOMEM_CALL_LEDGER_KEEP_ROWS` (default 2000) move byte-exact into a
content-addressed `ledger-archive/ledger-<hash>.jsonl`. `sequence` does not
reset and the chain spans the boundary. Archive filenames are content-addressed,
so they say nothing about age — every reader orders segments by the `sequence`
the rows carry. Archives are not pruned automatically.

### How it differs from the neighbours

- `queries.jsonl` / `writes.jsonl` / `reads.jsonl` / `mutations.jsonl` each
  record one *family* of call and carry no caller identity; the ledger records
  every call and names the client.
- The `exomem.calls` prose lines in `exomem.log` are evictable by volume —
  including by the traceback storm that accompanies the incident whose calls you
  need. The ledger is structured, chained, and bounded rather than evicted.
- `.idempotency-<vault>.sqlite` is a **replay cache**, not a ledger:
  mutations-only, keyed so a replay overwrites the row, and TTL-pruned.

### Configuration

`EXOMEM_DISABLE_CALL_LEDGER=1` turns it off. `EXOMEM_CALL_LEDGER_DIR` moves it
off the log directory. `EXOMEM_CALL_LEDGER_ROTATE_BYTES` and
`EXOMEM_CALL_LEDGER_KEEP_ROWS` size the live file. Every ledger operation
soft-fails: a failure to build, hash, append, or rotate a row never raises into
the call path and never changes a call's result.

## Metrics

`src/exomem/metrics.py` is one process-wide registry (counters + fixed-bucket
histograms) exposed at `GET /metrics.json` (beside `/health/ready`,
unauthenticated, `Cache-Control: no-store`). It persists to the writer-lease
state directory every `EXOMEM_METRICS_SNAPSHOT_SECONDS` (default 60; `0`
disables the snapshotter thread) so counts survive a restart. Key metrics:

- `exomem_tool_calls_total{tool,outcome}`, `exomem_tool_failures_total{tool,code}`,
  `exomem_tool_duration_ms{tool}` — per-tool call outcomes and latency.
- `exomem_mutation_busy_total{code}`, `exomem_boundary_wait_ms`,
  `exomem_boundary_hold_ms`, `exomem_boundary_overdue_total` — mutation
  contention: busy refusals by code, acquire-wait and hold histograms, and
  holds that exceeded the long-holder threshold.
- `exomem_lease_ops_total{op,outcome}`, `exomem_coordinator_errors_total{code}` —
  writer-lease coordination. An `acquire` with outcome `reused` is a collection-store
  write nested in an operation that kept the operation's grant instead of
  asking the coordinator again. A non-preferred replica also voluntarily releases
  an idle lease after `EXOMEM_WRITER_LEASE_IDLE_SECONDS` (default tracks the
  TTL: `max(60, TTL)`; `0` disables; preferred replicas never idle-release).
- `exomem_idempotency_replays_total` — identical retries served from the
  receipt store instead of re-executing.
- `exomem_http_requests_total{status}`, `exomem_edge_ingress_total{outcome}`,
  `exomem_stale_session_serves_total` — HTTP/edge traffic.
- `exomem_log_write_errors_total{where}` — a logging/journal write that failed
  (itself never fatal).

`EXOMEM_DISABLE_METRICS=1` turns the registry off entirely (`/metrics.json`
returns 404; counters stop accumulating).

## Tracing a request

```
exomem trace <request-id>
exomem trace <request-id> --json
```

Joins `exomem.log`, `ledger.jsonl`, `queries.jsonl`, `writes.jsonl`,
`reads.jsonl`, and `mutations.jsonl` for one request id into one time-ordered
report. Best-effort: a missing or unparseable source is skipped, never raised.

```
exomem logs tail --file server -n 50 [-f]
exomem logs grep --file mutations 'MUTATION_BUSY'
exomem logs grep --file ledger '"outcome":"refused"'
exomem logs verify
```

`--file` accepts
`cli | ledger | media | mutations | queries | reads | server | writes`.

## Latency watch

`request_latency_contract.json` is the packaged source for latency bars. Each
bar names its owner, boundary, profile, population, outcomes, reference and
window. Python consumers load the validated resource with `importlib.resources`.
The source keeps provisional p90 recall diagnostics separate from live warm
activation's strict p95 bar. Pending bars have null thresholds and no pass.

The existing watch retains up to 2,048 content-free observations. MCP dispatch,
REST command and public activation have separate boundary identities. REST
command timing includes response construction and excludes transmission. Earlier
REST refusals count as excluded ingress coverage with unknown duration.
Activation timing includes packet finalization and every known terminal outcome.
Existing `total_ms`, `duration_ms` and metrics keep their original meanings.

The ring reports retained samples, its oldest retained time, errors, refusals,
abstentions, missing joins, invalid readings and evictions. Its requested window
is not a traffic census. Evictions across the window boundary have an unknown
window count and a known lifetime count. Unknown timings never become zero.
Incomplete coverage cannot establish acceptance. A breach still diagnoses the
retained slow observations. The existing startup grace, conditional sweep and
hourly breach interval remain unchanged.

`bootstrap` reports only the calling client's breaches. Structured breach events
omit caller identities. `doctor` reads the ledger and activation log independently,
including their archived or rotated segments, and also reports the process ring.
A failed segment does not hide older samples. Each source exposes malformed rows,
failed reads and unknown window membership. Doctor warnings remain advisory;
absence of a breach means no observed breach, not accepted live performance.

Activation collects the existing named stages for host-local attribution even
without `include_timings`. Public packet timings remain opt-in. The activation
log preserves its outcome codes and stores timing-only stage rows. Observation
adds no request-path source reads or fsync and cannot change a request result.
The automatic runtime profile does not prove pre-call warmth or installed live
acceptance, so it uses a pending bar. Matching live acceptance remains separate.

`scripts/recall_latency_gate.py` resolves the current governed recall ceilings,
diagnostic targets and reference premise from the same source. It identifies
its legacy inner-timing boundary and reports full acceptance as not measured.
The owning task 6.1 remains open until reference, principal, encoder, contention
and stage evidence cover its current contract. Missed diagnostic targets fail
neither a ceiling nor a release.

## Recall availability

The packaged MCP canary checks private cases through `search_memory`,
`recall_context` and `read_memory`.
On a schema mismatch, the attempt can retain `schema_failure` with the validator
keyword and `schema_path_sha256`. The hash covers the first rejected schema
path encoded with compact JSON separators. It comes from the same validation
pass and does not include raw paths, messages, constants or response content.

Join this fingerprint to the case-file digest, case identity and observer
version. It supports correlation; different assertions can share a validator
path through references or composition. It does not uniquely identify an
assertion, establish its product cause or list every failure. Predictable paths
can have guessable hashes;
the fingerprint permits correlation and does not guarantee secrecy. Missing
diagnostic metadata does not change the failed result.

## Doctor

The `observability` check covers log-directory writes, rotation sizes, and
metrics-snapshot freshness. It reports those conditions separately from latency.

`exomem doctor` reports observation coverage beside each measured percentile.
It reports unavailable or partial sources independently and preserves healthy
request results when observation fails. Consult `sources` and each row's
`coverage` before using a percentile. The latency check does not contact a
service or change runtime state.

## Environment variables

See the "Observability" block in `env.example` for the full list
(`EXOMEM_LOG_DIR`, `EXOMEM_LOG_LEVEL`, `EXOMEM_LOG_MAX_MB`,
`EXOMEM_LOG_BACKUPS`, `EXOMEM_JSONL_MAX_MB`, `EXOMEM_METRICS_SNAPSHOT_SECONDS`,
`EXOMEM_DISABLE_ACCESS_LOG`, `EXOMEM_DISABLE_METRICS`,
`EXOMEM_DISABLE_CALL_LEDGER`, `EXOMEM_CALL_LEDGER_DIR`,
`EXOMEM_CALL_LEDGER_ROTATE_BYTES`, `EXOMEM_CALL_LEDGER_KEEP_ROWS`).
