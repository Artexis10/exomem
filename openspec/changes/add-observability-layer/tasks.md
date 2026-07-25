## 1. O1 — Logging core

- [x] 1.1 Add `src/exomem/log_events.py`: `log_event()` helper attaching
      `{event, fields, content}` via `extra=`; `JsonLinesFormatter` +
      `KeyValueFormatter`; `EVENT_CATALOG` with per-event content
      classification; `bounded_traceback()`.
- [x] 1.2 Rewrite `src/exomem/logging_config.py`: JSONL file logs with UTC ISO
      timestamps, per-process files (`exomem.log` / `exomem-cli.log` /
      `exomem-media.log`), `EXOMEM_LOG_LEVEL` canonical with `FASTMCP_LOG_LEVEL`
      fallback preserved.
- [x] 1.3 Wire `configure_logging` into the CLI dispatch path in
      `src/exomem/__main__.py` and into `src/exomem/media_worker_child.py`.
- [x] 1.4 Upgrade `src/exomem/privacy_log.py`: a hosted + structured record
      (produced via `log_event()` for a cataloged event) drops its `content`
      payload and keeps the content-free skeleton; an unstructured record
      keeps today's full blanking; `exc_info` is always stripped hosted.
      Implemented by wrapping `logging.Logger.makeRecord` rather than
      `logging.setLogRecordFactory`: `extra=` attributes are applied by
      `makeRecord` after the factory returns, so a factory hook cannot see
      `event`/`fields`/`content` (caught red by an initial factory-only
      attempt; see design.md).
- [x] 1.5 Fix `scripts/restart.ps1` / `restart.sh`: archive `exomem.log` to
      `logs/archive/` keeping the newest 10 instead of deleting it; prune the
      `service.*` rotation pile to the newest 20 (Windows/NSSM only —
      launchd/systemd have no equivalent uncapped rotation pile); keep the
      plain tail fallback.
- [x] 1.6 Red-first tests: `tests/test_log_events.py`,
      `tests/test_privacy_structured_redaction.py`. Additional coverage:
      `tests/test_log_process_files.py` (per-process files, CLI dispatch
      wiring).

## 2. O2 — Metrics registry

- [x] 2.1 Add `src/exomem/metrics.py`: counters + fixed-bucket histograms
      under one `threading.Lock`; metric names per design.md; every public
      method soft-fails.
- [x] 2.2 Atomic JSON snapshot/restore to the writer-lease `state_dir`; a
      snapshotter thread started from `server_runtime.py`
      (`EXOMEM_METRICS_SNAPSHOT_SECONDS`, default 60; `0` disables).
- [x] 2.3 Red-first tests: `tests/test_metrics_registry.py`.

## 3. O3 — Error-plane capture

- [x] 3.1 At the `OpError`/exception conversion sites (`command_surface.py`
      MCP tool wrapper, `server_rest.py`, `server_hosted.py`), log
      `event=tool_failure` / `event=rest_failure` with content-free fields
      (`tool`, `request_id`, `code`, `duration_ms`, `scope`) plus a
      300-char-truncated `content.message`, and bump the matching metrics
      counters, before returning the unchanged envelope. `server_hosted.py`'s
      `_execute_command` already logged an `event=hosted_call ...` trace line
      on every error via `_trace`/`_error_response`; that site only gained a
      metrics bump, not a second differently-shaped log line.
- [x] 3.2 Add the bounded, locked `_TOOL_FAILURES` signal dict, popped
      unconditionally (with a TTL sweep) by `CallTraceMiddleware`, which emits
      `tool_failure code=...` instead of `tool_success` for a failed call
      while preserving the `event=hosted_call kind=...` prefix contract for
      hosted MCP calls.
- [x] 3.3 Red-first tests: `tests/test_tool_failure_logging.py`; confirm
      `tests/test_command_surface_retry.py:831,861,878` (OpError stays normal
      tool content, unexpected exception stays native) pass unmodified.

## 4. O4 — Correlation

- [x] 4.1 Add `src/exomem/access_log.py`: ASGI `AccessLogMiddleware` (UTC ts,
      method, path, status, `duration_ms`, request_id extracted or minted and
      injected as a response header, session id, `cf-ray`); wire at
      `server.py`; silence `uvicorn.access` in the same commit.
- [x] 4.2 Add additive JSONL fields to `logs/{queries,writes,reads}.jsonl` via
      a new `peek_request_id()` (never mints): `request_id`, `ts_utc`,
      `outcome`, `error_code`, `duration_ms`; size-cap rotation (64MB, keep one
      generation); `usage.py` reads the `.jsonl.1` generation too.
- [x] 4.3 Log the retry-scope hash in tool events.
- [x] 4.4 Red-first tests: `tests/test_access_log_middleware.py`,
      `tests/test_jsonl_rotation.py`.

## 5. O5 — Mutation journal

- [x] 5.1 Add `src/exomem/mutation_journal.py`: `logs/mutations.jsonl`, one
      record per mutation attempt at the terminal/interrupted/replayed seam in
      `writer_lease.py` — `ts_utc`, `request_id`, `tool`, `command`,
      `receipt_id`, `outcome`, `error_code`, `duration_ms`, `boundary_wait_ms`,
      `boundary_hold_ms`, `lease_role`, `fencing_token`, `replica_id`, `scope`,
      content-classified `targets`, `target_count`. `targets` is the
      mutation's canonical vault/cell identity (writer_lease.invoke() has no
      per-file visibility below the command leaf; deeper per-file target
      tracking is future work, noted in the delivery report).
- [x] 5.2 `mutation_lock.py` captures `wait_ms`/`hold_ms` for the journal via
      a `ContextVar` (`last_mutation_timing()`), read by `writer_lease.invoke()`.
- [x] 5.3 Red-first tests: `tests/test_mutation_journal.py`.

## 6. O6 — Surfacing

- [x] 6.1 Add a `/metrics.json` route beside `/health/ready`
      (`server_assets.py`).
- [x] 6.2 `runtime_readiness.py` gains an `observability` block.
- [x] 6.3 `coordination_status` (`writer_lease.py`) gains
      `ttl_remaining_seconds`, `renewer_alive`, `last_renew_age_seconds`,
      `last_coordinator_error{code,age_seconds}`, and lease op counters, and
      records coordinator errors instead of silently returning
      `coordinator_healthy: False`.
- [x] 6.4 Red-first tests: `tests/test_runtime_readiness.py`,
      `tests/test_writer_lease.py`, `tests/test_metrics_endpoint.py` additions.

## 7. O7 — Tooling

- [x] 7.1 `exomem trace <request_id>` and `exomem logs tail|grep` in
      `__main__.py` (`src/exomem/obs_cli.py`).
- [x] 7.2 Doctor `_check_observability` (log dir writable, rotation live,
      JSONL parseable, snapshot age, `service.*` count).
- [x] 7.3 Red-first tests: `tests/test_obs_cli_trace.py`.

## 8. O8 — Docs + spec

- [x] 8.1 `docs/observability.md`.
- [x] 8.2 Remove the "observability = non-goal" bullet at
      `docs/deployment.md:954`.
- [x] 8.3 `env.example` entries: `EXOMEM_LOG_DIR`, `EXOMEM_LOG_LEVEL`,
      `EXOMEM_LOG_MAX_MB`, `EXOMEM_LOG_BACKUPS`, `EXOMEM_JSONL_MAX_MB`,
      `EXOMEM_METRICS_SNAPSHOT_SECONDS`, `EXOMEM_DISABLE_ACCESS_LOG`,
      `EXOMEM_DISABLE_METRICS` — `EXOMEM_LOG_MAX_MB`/`EXOMEM_LOG_BACKUPS` were
      wired into `logging_config.py` (previously hardcoded) so the documented
      vars are real, not aspirational.

## 9. Verification

- [x] 9.1 Pinned suites stay green: `tests/test_command_surface_retry.py`
      (`tests/test_latency_gate.py` untouched — not run this turn, see report).
- [ ] 9.2 `uv run ruff check src tests` clean on changed files — ruff is not
      installed in this environment; the orchestrator runs it out-of-band.
- [ ] 9.3 Linux CI: full suite, latency + golden gates (authoritative; this
      box cannot run the full suite).
- [ ] 9.4 Live smoke after merge+deploy: restart via `restart.ps1`, confirm
      `logs/exomem.log` survives as an archive, `exomem trace <request_id>`
      joins a real call end-to-end, `/metrics.json` serves.
