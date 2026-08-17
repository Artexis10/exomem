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

### 1. One emission point, reading a breadcrumb the wrapper already leaves

The identity, timing, and argument shape are known at the middleware; the outcome and refusal
code are known only at the wrapper.

**This decision changed during implementation.** The proposal called for a request-scoped
`ContextVar` set at the wrapper and read back at the middleware. That cannot work: the
synchronous tool wrapper runs in FastMCP's anyio threadpool, and a ContextVar set there does not
propagate back to the awaiting middleware. Main has since grown the mechanism that does work —
`_record_tool_failure` / `pop_tool_failure` (`src/exomem/command_surface.py:84-116`), a bounded,
locked, per-call-token breadcrumb, keyed by the token `mcp_request_context` mints so two
concurrent calls sharing a client-supplied request id cannot cross. So the outcome signal
already exists and the prose log already distinguishes `tool_failure` from `tool_success`. What
was missing was the **durable structured sink**, not the signal.

The ledger therefore emits from **one** place, `CallTraceMiddleware.on_call_tool`, which pops
the breadcrumb at the single point where both the duration and the outcome are known:

- normal return → `outcome="refused"` with the popped `code` if a breadcrumb is present, else
  `outcome="ok"`;
- uncaught exception → `outcome="error"` with the exception class;
- the **binary-blob content guard**, which raises ahead of `call_next` → `outcome="refused"`
  with the guard's own code, parsed from its `CODE: detail` message;
- **`edit_memory` operation normalization**, which rejects ahead of everything →
  `outcome="refused"` with `INVALID_EDIT`, recording the untranslated arguments the client
  actually sent.

The last two are the reason the emission is not simply "return and except". Both reject before
the leaf, and a row only at the return/except points would leave those calls with no durable
record of having been attempted — the exact "the write silently did not happen" gap this change
exists to close. Recording the normalization rejection required moving that normalization inside
the request scope; it was previously the first statement of the handler, ahead of even the
request id.

Exactly one row per call. The row is **not** emitted at `tool_start`, and the existing
`tool_start` / `tool_success` / `tool_failure` prose lines are untouched.

### 1b. Caller identity: recovered from the handshake, and deliberately not hashed

The MCP initialize handshake carries `clientInfo` (name and version); the server reaches it at
`ctx.session.client_params.clientInfo` and then discards it. `mcp_caller_identity()`
(`src/exomem/command_surface.py`) recovers it along with the transport (headers exist only on
the HTTP transports, so their presence *is* the transport signal) and the session id. Outside an
MCP call every field is `None` — never a raise.

These are recorded **in the clear**, unlike `caller_principal_hash`. A client's product name and
version identify *software*, the same class of value as a `User-Agent`; hashing them would
destroy the only field that answers "which client?" while protecting nothing. The per-principal
identity stays hashed and separate, so the two questions — *which software* and *which
principal* — are answerable independently and at different sensitivities.

### 2. The JSONL row schema

One JSON object per line. Field order below is documentation only; the canonical form used for
hashing sorts keys (Decision 8).

| field | type | source / meaning |
|---|---|---|
| `schema_version` | int const | schema id; starts at `1` |
| `sequence` | int | strictly monotonic, never reset, spans rotation |
| `prev_hash` | hex string | previous row's `row_hash`; genesis = 64 zeros |
| `row_hash` | hex string | sha256 of this row's canonical JSON excluding `row_hash` |
| `ts_utc` | string | ISO-8601 UTC wall clock (informational; `sequence` orders). Named to match the other JSONL journals, so `exomem trace` sorts every source with one key |
| `request_id` | string | `mcp_request_id()` UUIDv4 (`command_surface.py:287-315`) |
| `session_id` | string \| null | MCP session, so one client's calls follow as a sequence |
| `client_name` | string \| null | `clientInfo.name`, in the clear (Decision 1b) |
| `client_version` | string \| null | `clientInfo.version`, in the clear |
| `transport` | string \| null | `"http"` or `"stdio"` |
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

The ledger writes **host-local**, outside the vault and outside the writer-lease-guarded
mutation path. That location is writable even when the vault is a read-only replica and even
when the writer lease is held by another host — which is the whole point: a ledger that cannot
write during a read-only incident cannot explain that incident.

**Changed from the proposal:** it defaults to `logging_config.resolve_log_dir()` —
`logs/ledger.jsonl`, beside `mutations.jsonl` — rather than to `<state_dir>/calls/` under the
writer-lease `~/.cache/exomem`. Both satisfy the constraint, but the log directory is where
`exomem logs` and `exomem trace` already look, and every other journal routes through that same
function precisely so they stay co-located. A second location would have bought nothing and cost
an operator a second place to know about. Overridable via `EXOMEM_CALL_LEDGER_DIR`; archives
live under `<log_dir>/ledger-archive/`. Files are created mode `0600` where the platform
supports it.

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

- Before an append, if the live file exceeds `EXOMEM_CALL_LEDGER_ROTATE_BYTES` (default
  `8_000_000` — larger than `log.md`'s `2_000_000` because call rows are far more frequent than
  vault log entries), the oldest rows beyond the newest `EXOMEM_CALL_LEDGER_KEEP_ROWS`
  (default `2_000`) are moved **byte-exact** into a content-addressed `ledger-<hash>.jsonl`
  archive.
- `sequence` **does not reset**, and the chain **spans the boundary**: the archived segment's
  last `row_hash` is still the live file's first retained row's `prev_hash`, so a verifier walks
  archive → live continuously. No per-archive header is written: the rows already carry
  `sequence`, and reading the first row of a segment places it, so a header would be a second
  copy of the same fact and a second thing to keep true.
- Rotation runs in the same in-process appender under the same lock, so there is no rename race
  with a child that inherited the descriptor.
- Because the archive name is content-addressed, **filename order says nothing about age**.
  Every reader — `trace`, `grep`, `verify` — orders segments by the sequence the rows carry.

### 7. Latency budget — buffered append, no fsync

Production reads are 10–13 ms; CI gates cap total at `CEIL_TOTAL_MS = 5000`
(`tests/test_latency_gate.py`) and the semantic-write median at `VALIDATE_MEDIAN_MS = 500`
(`scripts/semantic_write_latency.py`). A ledger row costs a few sha256 hashes over the argument
blobs plus one open/write/close.

**Measured** (Windows, 2 000 rows, a ~4 KB `content` argument): median **0.35 ms**, p95 0.50 ms,
p99 0.72 ms, max 5.2 ms. That is ~0.05 % of a 650 ms write and ~3 % of a 12 ms read — under the
1 ms budget, though not the "microseconds" this design first assumed; on Windows the per-row
`open`/`close` syscall pair dominates the hashing.

Holding the descriptor open across appends would remove that pair, and is deliberately not done:
rotation's `os.replace` fails on Windows while a handle is open (`WinError 32`) — the same trap
that rules out `RotatingFileHandler` — so a cached fd would have to be invalidated on rotation,
on a path change, and on external deletion. That is three new failure modes bought with 0.3 ms
on a diagnostic sink.

**No fsync on the hot path.** A hard crash may lose the last rows; that loss is *detectable* as
a `sequence` gap, and the ledger is diagnostic, not a durability-critical store, so the
unsynced append is the correct trade.

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

### 9. The operator surface is the existing one, and verification is anchored

A ledger nobody can read is not observability, so the ledger joins the tooling that already
exists rather than adding a surface: a `exomem logs --file ledger` alias, a source that
`exomem trace <request_id>` joins, and `exomem logs verify` for the chain. The CLI's `--file`
`choices` now come from `obs_cli.file_aliases()`, one list, so they cannot drift from what
`resolve_log_file` accepts.

`verify` walks **archive-then-live as one chain**, and does so **anchored**: the first row of
the whole history must be `sequence` 1 hanging off the genesis hash. Without the anchor, a
dropped *oldest* segment verifies clean — nothing precedes the first surviving row to
contradict it, so the chain merely appears to have started later than it did. Verifying a
single file read mid-stream cannot make that claim, so anchoring is a parameter rather than the
default.

## Risks / Trade-offs

- **No fsync → crash loses the buffered tail.** Mitigated: the loss shows up as a `sequence`
  gap, so it is detectable rather than silent, and the ledger is diagnostic.
- **Single-writer assumption.** If a future refactor dispatches MCP calls across processes, the
  in-process chain would fork. Not mitigated by a `writer_id` field — the deployment shape
  excludes the case (one server process, host-local log dir), and a field for a case that cannot
  occur is a claim the code would have to keep true for nothing. The assumption is documented in
  the module, and if it ever breaks the symptom is loud rather than silent: two writers would
  collide on `sequence`, which is precisely what `verify` reports.
- **`target_paths` can be sensitive.** A filename can itself hint at content. Recorded because
  it is load-bearing for forensics; bounded, host-local, `0600`.
- **`client_name` is unauthenticated.** It comes from the client's own handshake, so a client
  could misdeclare it. It is a diagnostic hint, not an authorization input, and nothing reads
  it back into a decision; `caller_principal_hash` remains the authenticated identity.
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

## Resolved Questions

- **Hash `target_paths`, or record them verbatim?** *Verbatim.* "Which page did that call touch?"
  is the first question of any forensic pass, and a hashed path cannot answer it. A path is
  structure, not content, and the file is host-local and `0600`. The residual risk — a filename
  that hints at its content — is stated below rather than traded for an unusable field.
- **Per-file random salt on argument hashes?** *No salt.* The equality oracle it would defeat is
  the feature: identical arguments hashing identically is what answers "is this client retrying
  the same call?", which is exactly the pattern a burst incident needs. A salt would cost that
  and protect a value that is already only a hash.
- **Default-on?** *Yes*, with `EXOMEM_DISABLE_CALL_LEDGER`. The ledger is the durable half of an
  always-installed tracer; a diagnostic you must remember to enable before the incident is not
  one.
- **Guard rejections: `error` or `refused`?** *`refused`*, carrying the guard's own code. A
  content guard is a governance rule, not a bug, and recording it as `error` would put it in the
  same bucket as a genuine crash — which is the same category error the refusals-as-successes
  defect made in the other direction. `_leading_error_code` promotes only a genuine `CODE:`
  prefix; anything unstructured keeps its exception class, so an unexpected exception cannot be
  laundered into a plausible-looking refusal code.
- **One file or split reads from writes?** *One file.* The ordering across reads and writes is
  itself evidence — a refused write followed by a read that returns stale data is a story two
  files cannot tell — and `tool` already partitions the rows for anyone who wants them apart.

## Open Questions

- Archive pruning / retention policy. Rotation bounds the live file; archives still accumulate
  like `log.md`'s. Out of scope here, flagged for later.
