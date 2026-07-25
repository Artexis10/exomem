## Why

Exomem fails in ways nobody can diagnose. `logs/exomem.log` does not exist today —
`scripts/restart.ps1` and `restart.sh` delete it on every restart, so every app log
line since the last restart is gone. Every business error is invisible: the MCP
tool wrapper (`command_surface.py`) and the REST facade (`server_rest.py`) convert
`OpError` (`MUTATION_BUSY`, lease refusals, validation refusals) into a normal
response envelope without logging anything, and `CallTraceMiddleware` then logs
`event=tool_success` for the same call — server-side error rate reads 0% no matter
how often clients see failures. There is no way to join the access log, the
`exomem.calls` trace, and `logs/{queries,writes,reads}.jsonl` (no request_id, no
UTC timestamp, unbounded growth) into one picture of a single request. There is no
metrics surface at all; every counter that exists is in-process and resets on
restart.

## What Changes

- Structured JSONL logging core: a `log_event()` helper that attaches
  `{event, fields, content}` to every log record, JSONL + key=value formatters,
  an `EVENT_CATALOG` that declares which fields of which events may carry
  content, per-process log files (`exomem.log` / `exomem-cli.log` /
  `exomem-media.log`), and an upgraded hosted privacy ceiling that drops only
  the `content` payload of a classified structured record while keeping today's
  full-blanking, fail-closed behavior for any unstructured record.
- `restart.ps1` / `restart.sh` archive `exomem.log` (keeping the newest 10) and
  prune the NSSM `service.*` rotation pile (keeping the newest 20) instead of
  deleting the log outright.
- A metrics registry (`metrics.py`): counters and fixed-bucket histograms under
  one lock, with a background snapshotter that atomically persists to the
  writer-lease state directory so a restart does not lose the counters, and
  restores them at startup. Every metrics operation soft-fails — a bug in the
  registry can never break a tool call.
- An error-plane: every site that converts an `OpError`/exception into a
  response envelope (the MCP tool wrapper, the REST facade, the hosted command
  routes) logs `event=tool_failure`/`event=rest_failure` with content-free
  fields plus a truncated content message, and bumps the matching metrics
  counters, before returning the same envelope byte-for-byte. A bounded,
  locked signal dict hands the outcome to `CallTraceMiddleware` so it emits
  `tool_failure` instead of `tool_success` for the same call, without changing
  what any client receives.
- Request correlation: an ASGI access-log middleware with UTC timestamps,
  method/path/status/duration/request-id, silencing the unstructured
  `uvicorn.access` logger in the same commit; additive (never renamed) JSONL
  fields (`request_id`, `ts_utc`, `outcome`, `error_code`, `duration_ms`) on the
  existing `logs/{queries,writes,reads}.jsonl`; size-capped rotation.
- A mutation journal (`logs/mutations.jsonl`): one record per mutation attempt
  at the terminal/interrupted/replayed seam, with wait/hold timing and
  content-classified targets.
- Surfacing: a `/metrics.json` route beside `/health/ready`; an
  `observability` block on `runtime_readiness`; richer `coordination_status`
  (TTL remaining, renewer liveness, last coordinator error, lease op counters).
- Operator tooling: `exomem trace <request_id>` and `exomem logs tail|grep`;
  a doctor check for the log directory, rotation, and snapshot health.
- Docs: `docs/observability.md`, removing the stale "observability is a
  non-goal" line from `docs/deployment.md`, and the new environment variables
  in `env.example`.

This capability is default-on, not default-off: every piece is stdlib-only,
sub-millisecond per call, and soft-fails independently, so there is no heavy or
optional runtime cost to gate behind a flag the way a model-backed capability
would be. `EXOMEM_DISABLE_ACCESS_LOG` and `EXOMEM_DISABLE_METRICS` exist as an
operator escape hatch, not because the default posture is unsafe.

## Capabilities

### New Capabilities

- `observability`: exomem exposes structured, correlated, content-safe logs and
  a metrics registry across MCP, REST, hosted, and CLI surfaces, default-on and
  soft-failing, so an operator can diagnose a failure after the fact instead of
  reading silence.

## Impact

- New modules: `log_events.py`, `metrics.py`, `access_log.py`,
  `mutation_journal.py`, `obs_cli.py`.
- Rewritten: `logging_config.py`, `privacy_log.py`.
- Touched conversion sites: `command_surface.py`, `server_rest.py`,
  `server_hosted.py`, `server.py` (`CallTraceMiddleware`), `writer_lease.py`
  (`coordination_status`, mutation-journal seam), `server_assets.py`
  (`/metrics.json`), `runtime_readiness.py`, `__main__.py`,
  `media_worker_child.py`.
- Deployment scripts: `scripts/restart.ps1`, `scripts/restart.sh`.
- No new runtime dependency. No change to the vault `Knowledge Base/log.md`
  format or its read path. No change to `tests/test_latency_gate.py` or
  `tests/golden/`.
