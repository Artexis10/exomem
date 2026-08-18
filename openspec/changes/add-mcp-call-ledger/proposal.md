## Why

Exomem already traces every MCP tool call, but the trace is **ephemeral, anonymous, and
mutations-only** at the durable layer. Four verified findings motivate a real ledger:

0. **Nothing records *which client* is calling.** Not `mutations.jsonl`, not `reads.jsonl`,
   not `queries.jsonl`, not the server log. The MCP initialize handshake carries `clientInfo`
   (name and version) and the server reaches it via `ctx.session.client_params` — then
   discards it. A vault served to several clients at once therefore produces one
   undifferentiated stream, and the first question of any incident — *whose* calls are
   failing? — has no answer in any durable source.

1. **Trace history is evictable by volume — including the exact volume you need it for.**
   `CallTraceMiddleware` (`src/exomem/server.py:77-145`) emits `event=tool_start` /
   `tool_success` / `tool_error` to the logger `exomem.calls` (`src/exomem/server.py:42`).
   The only durable sink for that logger is the rotating **process log file**. A traceback
   storm or an ordinary burst of calls scrolls older lines out of the retained window before
   anyone can read them — precisely during the incident whose calls you need to reconstruct.
   There is no queryable, structured record; there is a stream of prose log lines.

2. **Refusals are indistinguishable from successes by control flow alone.** When a governance
   rule refuses a call, the tool wrapper `bind_vault` **returns an error envelope** rather than
   raising, so the middleware sees a normal return. Since this proposal was written the wrapper
   gained a bounded, per-call-token breadcrumb (`_record_tool_failure` / `pop_tool_failure`) —
   ContextVars do not propagate back out of FastMCP's anyio threadpool, so a breadcrumb is the
   only thing that can bridge the two seams — and the prose log now distinguishes
   `tool_failure` from `tool_success`. What remains missing is the **durable structured sink**:
   the refusal and its `OpError.code` (`WRITER_LEASE_REQUIRED`, `SEMANTIC_IDENTITY_DUPLICATE`,
   `MUTATION_BUSY`) still survive only as an evictable prose line, so "the write silently did
   not happen" still cannot be reconstructed after the fact.

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
  capturing which client called, timing, argument *shape* (never values), the outcome, and the
  refusal `OpError.code` when a call was refused.
- **Recover the caller identity the handshake already supplies.** A new
  `mcp_caller_identity()` reads `clientInfo` name/version, the transport, and the session id
  off the live MCP context, and every row carries them **in the clear**. This is deliberately
  unlike `mcp_retry_scope()`: a client's product name and version identify *software*, the same
  class of value as a `User-Agent`, and hashing them would destroy the only field that answers
  "which client?" while protecting nothing. The per-principal identity stays hashed and
  separate.
- Capture at **one seam**, reading both signals there. `CallTraceMiddleware.on_call_tool`
  supplies caller identity, timing, and argument shape; the wrapper's existing per-call-token
  breadcrumb, popped at that same point, supplies `refused` versus `ok` and the `OpError.code`.
  One row is emitted at each of the middleware's exits — normal return, raised exception, and
  the two pre-checks that reject ahead of the leaf — so a call produces exactly one row and
  never zero.
- Write a **redacted, append-only, hash-chained JSONL ledger**, host-local — **outside the
  vault** and **outside the writer-lease mutation boundary** — so it records even on a
  read-only replica, during the exact incidents it exists to explain.
- Give every row a **monotonic `sequence`** and a **`prev_hash` / `row_hash` chain** so a
  dropped, reordered, or edited row is detectable rather than silent.
- Record arguments as **shape + hashes only**: argument names, per-argument byte length and
  sha256, and structural target paths. Nested structures are hashed whole, so a value buried
  inside an argument object is covered by the same construction as a top-level one. The
  principal identity reuses the already-computed hash from `mcp_retry_scope()` — privacy-safe
  by construction and currently discarded.
- **Expose it through the tooling operators already use**, rather than adding a surface to
  learn: a `exomem logs --file ledger` alias, a source that `exomem trace <request_id>` joins,
  and `exomem logs verify` for the chain. Reads span the archive and the live file as one
  sequence, ordered by the sequence the rows carry — archive filenames are content-addressed
  and say nothing about age.
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

- `src/exomem/call_ledger.py` — **new** module: row construction, canonical hashing, the chain,
  the append, content-addressed rotation, and chain verification.
- `src/exomem/server.py` — `CallTraceMiddleware.on_call_tool` emits one row at each of its
  exits, reusing the request scope it already binds (`mcp_request_context`). The `edit_memory`
  normalization moves *inside* that scope so its rejection is recorded rather than lost.
- `src/exomem/command_surface.py` — new `mcp_caller_identity()`; no change to the `bind_vault`
  outcome branch, whose breadcrumb the middleware already reads.
- `src/exomem/obs_cli.py`, `src/exomem/__main__.py` — the ledger becomes a `logs` file alias
  and a `trace` source, with archive-aware reads and a `logs verify` subcommand.
- `tests/` — a leak test plus unit tests for the schema, identity, chain, rotation, and the
  operator surface.
- Configuration — new environment knobs for the ledger directory, enablement, and rotation
  size and retention, mirroring `EXOMEM_DISABLE_QUERY_LOG`. The ledger defaults to
  `resolve_log_dir()`, deliberately beside `mutations.jsonl` rather than in a second location,
  so `exomem logs` and `exomem trace` find it without anything new to explain.

**Default and soft-fail behavior:** the ledger is **enabled by default** (it is the durable
half of an always-installed tracer) with an `EXOMEM_DISABLE_CALL_LEDGER` kill switch mirroring
`EXOMEM_DISABLE_QUERY_LOG`. Every ledger write is **soft-fail**: a failure to append, hash, or
rotate is contained and never raises into the call path, exactly as `query_log._append` swallows
its own errors (`src/exomem/query_log.py:65-71`). The ledger adds no model and performs only
deterministic measurement, so no pure-substrate model justification applies.

Backwards compatible: no existing surface, payload, or log line changes; the ledger is a new
additive sink.
