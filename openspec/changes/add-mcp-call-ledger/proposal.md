## Why

Exomem already traces every MCP tool call, but the trace is **ephemeral, ambiguous, and
mutations-only** at the durable layer. Three verified findings motivate a real ledger:

1. **Trace history is evictable by volume — including the exact volume you need it for.**
   `CallTraceMiddleware` (`src/exomem/server.py:77-145`) emits `event=tool_start` /
   `tool_success` / `tool_error` to the logger `exomem.calls` (`src/exomem/server.py:42`).
   The only durable sink for that logger is the rotating **process log file**. A traceback
   storm or an ordinary burst of calls scrolls older lines out of the retained window before
   anyone can read them — precisely during the incident whose calls you need to reconstruct.
   There is no queryable, structured record; there is a stream of prose log lines.

2. **Refusals are logged as successes.** When a governance rule refuses a call, the tool
   wrapper `bind_vault` **returns an error envelope** rather than raising:
   `cli_ops.envelope(False, error=error.as_public_dict())`
   (`src/exomem/command_surface.py:234-242`). The middleware sees a normal return and emits
   `event=tool_success` (`src/exomem/server.py:133-137`). The refusal — and its
   `OpError.code` (`WRITER_LEASE_REQUIRED`, `SEMANTIC_IDENTITY_DUPLICATE`, `MUTATION_WARMING`)
   — is **never durably recorded**. "The write silently did not happen" cannot be
   reconstructed after the fact.

3. **No durable per-call record exists.** The only durable per-request store is
   `.idempotency-<vault>.sqlite` (`src/exomem/writer_lease.py:868-869`, table `mutations`
   created at `:463`). It is **mutations-only**, **keyed** (a replay overwrites the same row),
   and **TTL-pruned** (`_prune_expired` at `src/exomem/writer_lease.py:586`; TTLs of 600 s
   implicit / 24 h explicit at `:54-55`). It is a replay cache, not a ledger. Reads and
   successful writes leave no structured, queryable, tamper-evident trail at all.

All three share a root cause: **the per-call tracer measures, but nothing durably records what
it measured in a form you can query or trust.**

## What Changes

- Add a `call-ledger` capability: exactly **one durable, structured row per MCP tool call**,
  capturing caller identity, timing, argument *shape* (never values), the outcome, and the
  refusal `OpError.code` when a call was refused.
- Capture across **two seams**, both required. `CallTraceMiddleware.on_call_tool`
  (`src/exomem/server.py:83-145`) supplies identity, timing, and argument shape; the
  `bind_vault` wrapper's outcome branch (`src/exomem/command_surface.py:224-242`) supplies the
  `ok | refused | error` outcome and the `OpError.code`. Without the second seam a refusal is
  indistinguishable from a success — which is the whole defect.
- Write a **redacted, append-only, hash-chained JSONL ledger**, host-local — **outside the
  vault** and **outside the writer-lease mutation boundary** — so it records even on a
  read-only replica, during the exact incidents it exists to explain.
- Give every row a **monotonic `sequence`** and a **`prev_hash` / `row_hash` chain** so a
  dropped, reordered, or edited row is detectable rather than silent.
- Record arguments as **shape + hashes only**: argument names, per-argument byte length and
  sha256, and structural target paths. Caller identity reuses the already-computed hashed
  principal from `mcp_retry_scope()` (`src/exomem/command_surface.py:259-284`) — privacy-safe
  by construction and currently discarded.
- **Rotate on the content-addressed pattern `log.md` already uses**
  (`src/exomem/vault.py:4422-4508`): size-triggered, keep newest N, move the older tail
  byte-exact into a `ledger-<hash>.jsonl` archive — **without resetting `sequence` or breaking
  the chain**. This deliberately does not repeat the unbounded growth of
  `queries.jsonl` / `reads.jsonl` / `writes.jsonl` (`src/exomem/query_log.py:36-38`).
- Add a **CI leak test** asserting no note content reaches the ledger. No such test exists
  today; local installs currently log `ask_memory` query text verbatim because the process-wide
  redactor is gated on `EXOMEM_HOSTED_CELL` (`src/exomem/privacy_log.py:16-26`).

### Non-Goals

- **Not** fixing the writer fail-back / lease-idle-release gap. That is a separate, tracked
  reliability issue; this change only makes the write path *observable*, it does not change it.
- **Not** a metrics / OpenTelemetry / tracing-span system. No aggregation, no exporter, no
  spans, no dashboards, no sampling. One flat durable row per call, and nothing more.
- **Not** a behavior change. No tool's return value, arguments, latency contract, or the
  existing `exomem.calls` log lines change. The ledger is a pure additive sink.
- **Not** a reasoning surface. Per the pure-substrate constraint, the ledger **measures**; it
  never scores, ranks, decays, or interprets a call. It runs no model.

## Capabilities

### New Capabilities

- `call-ledger`: a redacted, append-only, hash-chained, size-bounded per-call record of every
  MCP tool invocation, written outside the vault and outside the writer-lease boundary.

### Modified Capabilities

- None. A read-only audit of 72 active and 27 archived changes found no existing
  observability, telemetry, or audit-logging capability, so this is a purely additive
  capability with no `MODIFIED` or `REMOVED` deltas.

## Impact

- `src/exomem/server.py` — `CallTraceMiddleware.on_call_tool` gains a ledger-row emission at
  the return / except point, reusing the request scope it already binds
  (`mcp_request_context`, `src/exomem/server.py:90`).
- `src/exomem/command_surface.py` — the `bind_vault` outcome branch (`:234-242`) records
  `outcome` + `error_code` onto a request-scoped context the middleware reads back.
- `src/exomem/call_ledger.py` — **new** module: row construction, canonical hashing, the chain,
  the buffered append, and content-addressed rotation.
- `tests/` — a new leak test plus pure-logic unit tests for the schema, chain, and rotation.
- Configuration — new environment knobs for the ledger directory, enablement, and rotation
  size, mirroring `EXOMEM_LOG_DIR` (`src/exomem/query_log.py:41-55`) and the writer-lease
  `state_dir` default `~/.cache/exomem` (`src/exomem/writer_lease.py:275`).

**Default and soft-fail behavior:** the ledger is **enabled by default** (it is the durable
half of an always-installed tracer) with an `EXOMEM_DISABLE_CALL_LEDGER` kill switch mirroring
`EXOMEM_DISABLE_QUERY_LOG`. Every ledger write is **soft-fail**: a failure to append, hash, or
rotate is contained and never raises into the call path, exactly as `query_log._append` swallows
its own errors (`src/exomem/query_log.py:65-71`). The ledger adds no model and performs only
deterministic measurement, so no pure-substrate model justification applies.

Backwards compatible: no existing surface, payload, or log line changes; the ledger is a new
additive sink.
