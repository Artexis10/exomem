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
`caller_principal_hash`, `tool`, `arg_names`, `args`, `target_paths`, `outcome`,
`error_code`, `duration_ms`, `total_ms`, `request_bytes`, `truncated`.

- **Latency, on every call — read, write, success, refusal.** `duration_ms` is
  the tool leaf, the number the prose trace and `exomem_tool_duration_ms` have
  always reported. `total_ms` is the wall clock the caller actually waited,
  including the content guard and argument normalization, which run *before*
  the leaf clock starts. When the two diverge the gap is itself the finding: it
  says the cost was in admission, not in the work. `request_bytes` is the total
  serialized argument size, so a slow call is interpretable rather than merely
  slow.

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
- **`args`** is `{name: {len, sha256}}` and **never a value**. Note bodies,
  query text, and credentials are reduced to a length and a hash by
  construction, not by a downstream filter — `privacy_log`'s process-wide
  redactor is gated on `EXOMEM_HOSTED_CELL` and is off for local installs.
  Identical arguments hash identically on purpose: that is what answers "is this
  client retrying the same call?".
- **`target_paths`** records the page a call addresses, verbatim. A path is
  structure, not content, and it is the first thing a forensic pass needs.

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

## Doctor

`exomem doctor` includes an `observability` check: log directory writability,
active/rotated file sizes, JSONL tail parseability, the NSSM `service.*`
rotation pile (warns above 50), and metrics-snapshot freshness (warns past 2×
the snapshot interval).

## Environment variables

See the "Observability" block in `env.example` for the full list
(`EXOMEM_LOG_DIR`, `EXOMEM_LOG_LEVEL`, `EXOMEM_LOG_MAX_MB`,
`EXOMEM_LOG_BACKUPS`, `EXOMEM_JSONL_MAX_MB`, `EXOMEM_METRICS_SNAPSHOT_SECONDS`,
`EXOMEM_DISABLE_ACCESS_LOG`, `EXOMEM_DISABLE_METRICS`,
`EXOMEM_DISABLE_CALL_LEDGER`, `EXOMEM_CALL_LEDGER_DIR`,
`EXOMEM_CALL_LEDGER_ROTATE_BYTES`, `EXOMEM_CALL_LEDGER_KEEP_ROWS`).
