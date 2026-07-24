## Context

Exomem already has a per-call tracer. `CallTraceMiddleware` (`src/exomem/server.py:77-145`) is
installed on both profiles — hosted (`src/exomem/server.py:241`) and local
(`src/exomem/server.py:255`). For each call it resolves a `request_id`
(`mcp_request_id()`, a UUIDv4 that honors an inbound `x-exomem-request-id` header,
`src/exomem/command_surface.py:287-315`), binds it through the synchronous wrapper with
`mcp_request_context` (`src/exomem/server.py:90`), and emits `event=tool_start` before the call
and `event=tool_success` / `event=tool_error` after, with `tool`, `request_id`, and
`duration_ms` (`src/exomem/server.py:126-145`).

Four facts from the codebase shape the design:

- **Refusals return, they do not raise.** `bind_vault`'s wrapper catches the governance error
  and returns an envelope (`src/exomem/command_surface.py:234-242`): an `OpError` becomes
  `cli_ops.envelope(False, error=error.as_public_dict())` (`:237-238`). The middleware's
  `try` around `call_next` (`src/exomem/server.py:130-137`) therefore sees a normal return and
  logs `tool_success`. The `OpError.code` never leaves the wrapper. Any outcome signal must be
  captured **at the wrapper**, not inferred at the middleware.
- **A privacy-safe caller identity already exists and is thrown away.** `mcp_retry_scope()`
  (`src/exomem/command_surface.py:259-284`) derives `principal:<sha256>` from a verified
  token subject/issuer (`:273`), `bearer:<sha256>` from an Authorization header (`:280`), or
  `session:<id>` (`:282`). It is computed for idempotency scoping and discarded for reads.
- **The process-wide redactor cannot be relied on.** `privacy_log.install_hosted_log_redaction`
  (`src/exomem/privacy_log.py:38-66`) is gated on `EXOMEM_HOSTED_CELL`
  (`content_private_logging_enabled`, `:16-26`) — off for local installs — and it *exempts*
  `exomem.calls` `event=hosted_call` lines from redaction (`:51-57`). The ledger must be
  redaction-safe **by construction**, independent of that flag.
- **A durable append precedent exists, and an unbounded-growth anti-pattern exists.**
  `query_log._append` (`src/exomem/query_log.py:65-71`) appends one JSON object per line in
  append mode, swallowing every exception. Its three sinks — `queries.jsonl`, `writes.jsonl`,
  `reads.jsonl` (`:36-38`) — have **no rotation** and are already multi-MB. `log.md`, by
  contrast, rotates: size-triggered, keep newest N, tail moved byte-exact into a
  content-addressed archive (`src/exomem/vault.py:4422-4508`).

## Goals / Non-Goals

**Goals**

- One durable, structured row per MCP tool call, distinguishing `ok`, `refused`, and `error`.
- Record enough to reconstruct *what happened* (identity, timing, argument shape, target,
  outcome, refusal code) and **nothing that leaks note content**.
- Survive the incident: write on a read-only vault replica, outside the writer-lease boundary.
- Make eviction, gaps, and tampering **detectable**, not silent.
- Stay well under 1 ms per row with no fsync on the hot path.

**Non-Goals**

- Not metrics/OTel/spans; not aggregation; not sampling. One flat row per call.
- Not a behavior change to any tool, envelope, latency contract, or existing log line.
- Not a fix for the writer fail-back gap; not a reasoning/scoring surface (pure substrate).

## Decisions

### 1. Two-seam capture with a request-scoped outcome

The identity, timing, and argument shape are known at the middleware; the outcome and refusal
code are known only at the wrapper. Bridge them through the request scope the middleware
**already binds**.

- The `bind_vault` wrapper's outcome branch (`src/exomem/command_surface.py:234-242`) records,
  onto a request-scoped `ContextVar`, either `outcome="refused"` with the `OpError.code`
  (from `error.as_public_dict()`) for a returned governance envelope, or leaves the default
  `outcome="ok"` when the leaf returns normally.
- `CallTraceMiddleware.on_call_tool` reads that ContextVar back after `call_next` returns and
  emits **one** ledger row at its return point (`src/exomem/server.py:131-137`) with the final
  `outcome`; on an uncaught exception in its `except` branch (`:138-145`) it emits one row with
  `outcome="error"` and the exception type as `error_code`.
- Exactly one row is emitted per call, at the single point where both timing and outcome are
  known. The row is **not** emitted at `tool_start`; the existing `tool_start` log line is
  untouched and remains prose-only.

The guard pre-check (`guards.guard_text_content`, `src/exomem/server.py:91-117`) raises before
`call_next`, so a guarded-content rejection surfaces in the middleware `except` branch and is
recorded as `outcome="error"`. Whether to promote guard rejections to `outcome="refused"` with
a synthetic code is deferred (see Open Questions).

### 2. The JSONL row schema

One JSON object per line. Field order below is documentation only; the canonical form used for
hashing sorts keys (Decision 8).

| field | type | source / meaning |
|---|---|---|
| `schema_version` | int const | schema id; starts at `1` |
| `sequence` | int | strictly monotonic, never reset, spans rotation |
| `prev_hash` | hex string | previous row's `row_hash`; genesis = 64 zeros |
| `row_hash` | hex string | sha256 of this row's canonical JSON excluding `row_hash` |
| `timestamp` | string | ISO-8601 UTC wall clock (informational; `sequence` orders) |
| `request_id` | string | `mcp_request_id()` UUIDv4 (`command_surface.py:287-315`) |
| `tool` | string | tool name (`_extract_tool_name`, `server.py:86`) |
| `caller_principal_hash` | string \| null | `mcp_retry_scope()` (`command_surface.py:259-284`) |
| `arg_names` | string[] | sorted argument names present |
| `args` | object | per name → `{ "len": int, "sha256": hex }` (value bytes, never value) |
| `target_paths` | string[] | vault-relative path(s) the call targets, structural |
| `outcome` | enum | `"ok"` \| `"refused"` \| `"error"` |
| `error_code` | string \| null | `OpError.code` when refused; exception type when error |
| `duration_ms` | number | `round((perf_counter()-t0)*1000, 2)`, as the tracer already computes |
| `truncated` | bool | true when a bounded field was truncated to keep the row atomic |

### 3. Where the file lives — outside the vault, outside the lease

The ledger writes to a **host-local state directory**, not the vault and not the
writer-lease-guarded mutation path. Default `<state_dir>/calls/ledger.jsonl`, where
`state_dir` mirrors the writer-lease default `~/.cache/exomem`
(`src/exomem/writer_lease.py:275`), overridable via `EXOMEM_CALL_LEDGER_DIR`. This location is
writable even when the vault is a read-only replica and even when the writer lease is held by
another host — which is the whole point: a ledger that cannot write during a read-only incident
cannot explain that incident. Archives live under `<state_dir>/calls/_archive/`. Files are
created mode `0600` where the platform supports it.

### 4. Append discipline and the single-writer chain

**All MCP tool calls are dispatched in the main server process** — `on_call_tool` runs there —
so the ledger has a single *logical* writer for call rows even though the media worker spawns
child processes. This is load-bearing and is asserted by a verification task (Decision 8,
Open Questions). Given that:

- The sequence counter, the `prev_hash` chain, and rotation are maintained by one in-process
  appender under a cheap in-process lock (async/thread), never a cross-process lock file. A
  lock file would add a failure mode on a read-only mount for no benefit here.
- Each row is serialized to a single `bytes` buffer terminated by `\n` and written with one
  `os.write` to an `O_APPEND` file descriptor. `O_APPEND` gives an atomic position advance, and
  a single write of a bounded, newline-terminated buffer keeps each row intact even if a child
  process has inherited the descriptor. This is why `O_APPEND` JSONL is chosen over
  `RotatingFileHandler`, whose rename-on-rotate is not multi-process-safe on Windows when a
  child holds the handle.
- Rows are bounded; if `arg_names`/`args`/`target_paths` would push a row past the atomic-write
  bound, those lists are truncated (with `truncated=true`) rather than splitting a row across
  two writes.

### 5. Redaction rule — shape and hashes, never values

- For each argument: record its name, the byte length of its serialized value, and
  `sha256(serialized value)`. Never the value.
- `target_paths` are recorded as vault-relative structural paths (the note a write addresses),
  not their content. Whether to hash the path itself is an Open Question.
- `caller_principal_hash` is the already-hashed `mcp_retry_scope()` output — a raw token or
  subject is never reachable here by construction.
- The ledger does **not** depend on `privacy_log`'s process-wide redactor, which is
  `EXOMEM_HOSTED_CELL`-gated (`src/exomem/privacy_log.py:16-26`) and off locally. Redaction is a
  property of what the row builder puts in the row, enforced by the CI leak test (Decision 9).

### 6. Rotation — content-addressed, sequence-continuous

Mirror `log.md` (`src/exomem/vault.py:4451-4508`), not a logging handler:

- Before an append, if the live file exceeds the size threshold (default modeled on
  `LOG_ROTATE_BYTES_DEFAULT = 2_000_000`, `src/exomem/vault.py:4424`, overridable), the oldest
  rows beyond the newest N (modeled on `LOG_ROTATE_KEEP_ENTRIES = 200`, `:4425`) are moved
  **byte-exact** into a content-addressed `ledger-<hash>.jsonl` archive.
- `sequence` **does not reset**, and the chain **spans the boundary**: the archived segment's
  last `row_hash` equals the live file's first retained row's `prev_hash`. A per-archive header
  records the archived `[first_sequence, last_sequence]` and the boundary hashes so a verifier
  walks archive → live continuously.
- Rotation runs in the same in-process appender under the same lock, so there is no rename race
  with a child that inherited the descriptor.

### 7. Latency budget — buffered append, no fsync

Production reads are 10–13 ms; CI gates cap total at `CEIL_TOTAL_MS = 5000`
(`tests/test_latency_gate.py`) and the semantic-write median at `VALIDATE_MEDIAN_MS = 500`
(`scripts/semantic_write_latency.py`). A ledger row costs a handful of sha256 hashes over small
argument blobs plus one `os.write` — microseconds, well under 1 ms. **No fsync on the hot
path.** A hard crash may lose the last buffered rows; that loss is *detectable* as a `sequence`
gap, and the ledger is diagnostic, not a durability-critical store, so buffered append is the
correct trade.

### 8. Per-row hash chain, not per-document

Each row carries `prev_hash` = the previous row's `row_hash`, and
`row_hash = sha256(canonical_json(row without row_hash))`, where canonical JSON sorts keys and
uses compact separators over UTF-8. The genesis row's `prev_hash` is 64 zeros. This is **O(1)
per append**.

This adopts the good parts of the `fable-delegate` skill's `audit.jsonl` prior art —
append-only, a monotonic sequence that makes eviction detectable, a per-entry hash, a
`schema_version` constant, restrictive `0600` mode, redaction discipline, and a JSON Schema
validated in CI — but **differs deliberately**: `audit.jsonl` re-hashes an accumulating
document, which is fine for a low-frequency state machine and O(n) and wrong for a
high-frequency event stream, so Exomem chains each row on the previous row's hash instead. And
because there is a single in-process writer, there is no lock file.

## Risks / Trade-offs

- **No fsync → crash loses the buffered tail.** Mitigated: the loss shows up as a `sequence`
  gap, so it is detectable rather than silent, and the ledger is diagnostic.
- **Single-writer assumption.** If a future refactor dispatches MCP calls across processes, the
  in-process chain would fork. Mitigated by a verification task asserting single-process
  dispatch and by reserving a `writer_id` field for a per-writer-stream fallback.
- **`target_paths` can be sensitive.** A filename can itself hint at content. Recorded because
  it is load-bearing for forensics; bounded, host-local, `0600`. Hashing it is an Open Question.
- **Argument-hash equality oracle.** `sha256(value)` is stable, so identical arguments hash
  identically — an equality oracle across calls. Acceptable for shape-matching; a per-file
  random salt would defeat it (Open Question).
- **Archive accumulation.** Rotation bounds the *live* file, but archives accumulate like
  `log.md`'s. An archive-pruning/retention policy is out of scope here and flagged for later.

## Alternatives Considered / Rejected

- **A SQLite ledger table** (like `.idempotency.sqlite`). Richer queries, but a DB write on the
  hot path risks lock contention, colocating it with the vault breaks read-only-replica writes,
  and retention becomes `VACUUM`. JSONL is grep-able mid-incident and matches the `query_log`
  precedent. Rejected; a downstream loader can import the JSONL into SQLite offline.
- **Synchronous fsync per row.** Durability, but it violates the sub-1 ms budget and the
  no-fsync constraint, and the chain already makes loss detectable. Rejected.
- **Reuse the `exomem.calls` process log as the ledger.** That *is* the status quo: evictable,
  unstructured, refusals-as-successes, and coupled to the hosted redactor. Rejected — it is the
  problem being fixed.
- **A whole-document hash chain (`audit.jsonl` style).** O(n) per append; unusable at
  read/write frequency. Rejected in favor of per-row chaining.
- **`RotatingFileHandler`.** Not multi-process-safe on Windows; its rename-on-rotate fails when
  a child holds the handle. Rejected in favor of in-process content-addressed rotation.
- **A single global lock file for cross-process append.** Unnecessary under the single-writer
  model and an extra failure mode on a read-only mount. Rejected.

## Open Questions

- Hash `target_paths`, or record vault-relative paths verbatim? (forensic value vs filename
  leak)
- Add a per-file random salt to argument hashes to defeat the equality oracle?
- Confirm default-on is right, and decide an archive-pruning/retention policy (out of scope now).
- Guard-content rejections: keep as `outcome="error"`, or promote to `outcome="refused"` with a
  synthetic code?
- One ledger file for reads and writes, or split them, given reads dominate call volume?
