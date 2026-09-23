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
| `queries.jsonl`          | One record per `find()`/`ask_memory` call.                      |
| `writes.jsonl`           | One record per note/add/replace write.                          |
| `reads.jsonl`            | One record per `get()` read.                                     |
| `mutations.jsonl`        | One record per mutation attempt (the mutation journal).          |
| `ledger.jsonl`           | One record per **MCP tool call** — the call ledger (below).       |
| `ledger-archive/`        | The call ledger's rotated segments, content-addressed.           |
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

Behind that terminal the dispatch chose one of five repairs, and the log says
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
- `graph dispatch registered a whole-vault rebuild reason=…` — a proven verdict:
  `graph_sync_predecessor_mismatch` or `…_absent` (the sidecar's acknowledgement
  is genuinely not this checkpoint's predecessor) or `graph_sync_snapshot_unusable`
  (no sidecar, a schema or relation-registry drift, a corrupt checkpoint).

A run of whole-vault rebuilds with an *unreadable* reason is the defect
`seamless-managed-worker-handoff` removed; a run of them with a mismatch or
unusable reason is a real lineage problem and belongs in reconcile.

Filesystem events the process cannot attribute to itself are recorded per path.
Any unrepaired external path still fences every read that requires a current
projection — relation-filtered recall reports `warming` rather than serving stale
edges — while a governed write on an unaffected path takes the incremental path
as usual. `freshness.snapshot()["external_pending_paths"]` names what is still
unrepaired.

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
`request_bytes`, `truncated`.

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

- **`outcome`** is `ok`, `refused`, or `error`. `refused` is a governance
  refusal — the tool wrapper returns an error *envelope* rather than raising, so
  control flow alone cannot distinguish it from success; the ledger reads the
  wrapper's per-call breadcrumb to tell them apart. `error` is an uncaught
  exception. `error_code` is the refusal's `OpError.code`, or the exception
  class.
- **`client_name` / `client_version` / `transport` / `session_id`** come from
  the MCP initialize handshake and are recorded **in the clear**: they identify
  software, the same class of value as a `User-Agent`. This is what makes one
  client's calls separable when several are connected to one vault at once. It
  is client-declared, so treat it as a diagnostic hint, not an authorization
  input. `caller_principal_hash` is the separate, hashed, authenticated
  identity.
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

### Where a write's time went (`spans`)

A row carries `spans`: named phases of that one call, slowest first, each with
a `count` and a total `ms`. They exist because two boundary clocks can prove a
call was slow and locate nothing between them — a live `edit_memory` once
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
| `derived.receipt_prepare`, `derived.receipt_proof`, `derived.acknowledgement`, `derived.pending_visibility` | The receipt and acknowledgement path. |
| `index.upsert_after_write` | The whole derived fan-out, and the sum the per-component spans below break down. |
| `index.completion_check` | Verifying full-index completion after component dispatch, including current publication state. `paths` counts the written paths checked. |
| `index.full_refresh_store` | Persisting durable full-index refresh work when completion is incomplete or dispatch raises. `paths` counts the written paths submitted for repair. |
| `index.memory_refs`, `index.resolver`, `index.lexstore`, `index.epistemic_graph`, `index.embeddings` | One per derived component, recorded at the shared dispatch seam. |
| `graph.refresh_paths` | The graph's incremental pass inside `index.epistemic_graph`. |
| `lexical.rebuild_atomic` | A whole-corpus lexical rebuild. |
| `embeddings.model_load` | Lazy load of the embedding model weights. |
| `embeddings.encode` | Encoding text to vectors. |
| `embeddings.matrix_load` | A full vector-matrix load from the sidecar; the log line `embedding matrix full load: … rows=… cached_gen=…` names the reason. |
| `embeddings.matrix_catch_up` | The bounded alternative to that full load. |
| `delivery.vocabulary_after_commit` | Vocabulary delivery after the commit. |
| `derived.advisory_execute`, `derived.component_dispatch`, `derived.component_completion` | The derived drain. |
| `recall.*` | Retrieval phases, named by `find`'s own timings. |
| `recall.due_state` | Serving the advisory due-state block a recall carries. On a memo hit its re-checks are `recall.due_state.verdicts` (`paths` = release verdicts re-asked), `recall.due_state.role` and `recall.due_state.exists` (`rows`); on a miss, `recall.due_state.build`. |
| `recall.due_state.emit` | Deciding whether that block is new to the session and recording its delivery. |
| `command.leaf`, `command.postfilter` | The two halves of a tool's `duration_ms`: the command itself, then the MCP-layer post-filter and scrub of its result. |
| `lexical.publication_wait` | Time a request spent waiting for the lexical publication barrier, with `timeout_ms` (the bound it chose) and `acquired` (1 or 0). Recorded only on the MCP path. |
| `recall.pack.parents`, `recall.pack.units`, `recall.pack.neighborhood`, `recall.pack.tension` | The deep pack's phases: reading the packed parents, extracting claims and packing semantic units, the one-hop wikilink neighbourhood, and supersession plus proximity tension (the sidecar reads). `recall.graph_enrich` remains the fifth. |
| `encode.by.<module>` | Beside every `embeddings.encode`: the Exomem module that asked for the encode (`context_pack`, `semantic_segments`, `index_sync`, ...). The name is the attribution because span fields are integers. |
| `index.embeddings.reuse` | Inside `index.embeddings`, once for the page's chunks and once for its semantic units: `texts` the write needed vectors for and `reused` how many came from the page's published rows instead of the encoder. `reused` near `texts` is an append; `reused` 0 on a long page is a first index or a rewrite. `reused` counts only texts whose vector came from the page's own published rows, the same meaning as on `advisory.best_cosine`. `recalled`, when present and disjoint from `reused`, counts texts that took an advisory sweep's just-encoded vector instead: an `add` commit taking the vectors its pre-commit sweep encoded. The encoder was handed `texts` minus both. |
| `read.page`, `read.history`, `read.links` | A direct read's phases: the page read, the edit log (`entries`), and the wikilink summary (`inbound`, `outbound`). |

Spans are aggregated by name within a call, so a phase entered once per changed
path reports a count and a total rather than hundreds of rows. Instrumentation
never raises: a missing span means that path did not run, not that the call
failed.

Some spans also carry `fields`: named integer counts beside the duration. A
duration alone cannot separate a slow step from a large one — `embeddings.encode`
at 15.6 s with `texts=1` and at 15.6 s with `texts=400` are different defects
with different fixes — so the spans that cover per-item work report how many
items they covered (`paths` on the `index.*` components, `texts`/`chars` on
`embeddings.encode` and `advisory.best_cosine`, `candidates` on
`advisory.overlap_groups`). The key is absent on a span that measured only time,
so nothing about an existing row changes.

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
  writer-lease coordination. A non-preferred replica also voluntarily releases
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

The ledger is also read back, so a latency regression is found by the system
and not by a person getting annoyed. `src/exomem/latency_watch.py` keeps a
bounded, content-free ring of recent recall calls (tool, client, deep or not,
`total_ms`, the five largest spans), fed beside the ledger row and under the
same rule that it can never break or slow a call. Over a trailing 24 h window
it reports p50 and p90 per (tool, client, deep) and compares the p90 against
PROVISIONAL ceilings held in that one module: 1,000 ms for `ask_memory`
without `deep`, `read_memory` and `find`; 5,000 ms for `ask_memory` with
`deep`. A verdict needs 20 samples, and calls in the first 10 minutes after
the process started do not count, so the cold window after a promotion is not
reported as a regression. There is no environment override: change the
constants through a spec change.

Where a breach shows up:

- `bootstrap` carries a `latency` block, only while the calling client's own
  recalls breach: per tool `{deep, samples, p50_ms, p90_ms, ceiling_ms,
  dominant_spans: [{name, ms, calls}]}`. A healthy service returns today's
  response shape. Another client's slowness is never reported to this one.
- `event=latency_ceiling_exceeded` (WARNING) with the same fields, at most
  once per hour per (tool, client, deep).
- `exomem doctor` `latency` (below), which reads the file instead of the ring.

## Doctor

`exomem doctor` includes an `observability` check: log directory writability,
active/rotated file sizes, JSONL tail parseability, the NSSM `service.*`
rotation pile (warns above 50), and metrics-snapshot freshness (warns past 2×
the snapshot interval).

It also includes a `latency` check: the same figures as the latency watch,
computed from `ledger.jsonl` (and the newest archive generations when the 24 h
window reaches past the active file) so they survive restarts, per tool and
calling client, with the dominant spans among the calls over the ceiling. It
warns above a ceiling, passes otherwise, notes pairs with fewer than 20 calls,
and passes with a note rather than failing when the ledger is absent or
unreadable. The startup grace is not applied here: a slow post-promotion
window is worth seeing in a diagnosis, and its spans say what it was.

## Environment variables

See the "Observability" block in `env.example` for the full list
(`EXOMEM_LOG_DIR`, `EXOMEM_LOG_LEVEL`, `EXOMEM_LOG_MAX_MB`,
`EXOMEM_LOG_BACKUPS`, `EXOMEM_JSONL_MAX_MB`, `EXOMEM_METRICS_SNAPSHOT_SECONDS`,
`EXOMEM_DISABLE_ACCESS_LOG`, `EXOMEM_DISABLE_METRICS`,
`EXOMEM_DISABLE_CALL_LEDGER`, `EXOMEM_CALL_LEDGER_DIR`,
`EXOMEM_CALL_LEDGER_ROTATE_BYTES`, `EXOMEM_CALL_LEDGER_KEEP_ROWS`).
