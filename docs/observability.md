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
| `archive/exomem-*.log`   | Prior sessions' `exomem.log`, archived (not deleted) on restart.  |

Each JSONL file rotates at a size cap (`EXOMEM_JSONL_MAX_MB`, default 64MB),
keeping exactly one prior generation (`<file>.1`). `usage.py` and `exomem
trace`/`exomem logs` read both the live file and that generation.
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

Joins `exomem.log`, `queries.jsonl`, `writes.jsonl`, `reads.jsonl`, and
`mutations.jsonl` for one request id into one time-ordered report. Best-effort:
a missing or unparseable source is skipped, never raised.

```
exomem logs tail --file server -n 50 [-f]
exomem logs grep --file mutations 'MUTATION_BUSY'
```

`--file` accepts `server | cli | media | queries | writes | reads | mutations`.

## Doctor

`exomem doctor` includes an `observability` check: log directory writability,
active/rotated file sizes, JSONL tail parseability, the NSSM `service.*`
rotation pile (warns above 50), and metrics-snapshot freshness (warns past 2×
the snapshot interval).

## Environment variables

See the "Observability" block in `env.example` for the full list
(`EXOMEM_LOG_DIR`, `EXOMEM_LOG_LEVEL`, `EXOMEM_LOG_MAX_MB`,
`EXOMEM_LOG_BACKUPS`, `EXOMEM_JSONL_MAX_MB`, `EXOMEM_METRICS_SNAPSHOT_SECONDS`,
`EXOMEM_DISABLE_ACCESS_LOG`, `EXOMEM_DISABLE_METRICS`).
