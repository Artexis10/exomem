## Structured event shape

Every observability log call goes through one helper,
`log_events.log_event(logger, level, event, *, fields=None, content=None,
exc_info=None)`, which attaches `event`, `fields`, and `content` to the
`LogRecord` via `extra=`. `fields` is always content-free (tool names, codes,
durations, counts, scope kinds). `content` is the only place a message excerpt
or other user-derived text may live, and only for events an `EVENT_CATALOG`
entry explicitly allows. `log_event()` never raises: an event with an
undeclared content key drops that key rather than failing the caller, because
a logging bug must never break a tool call. Two formatters read the same
three attributes — `JsonLinesFormatter` for the on-disk JSONL file,
`KeyValueFormatter` for the existing `key=value` console/trace style — so nothing
downstream re-parses free-text messages.

## Why redaction classifies by record, not by regex

`privacy_log.py` today either passes a record through untouched or blanks it
completely, keyed only on whether the message is the `exomem.calls`
`event=hosted_call ...` trace line. That is a coarse but safe fail-closed
default. This change adds one more branch: a record produced by `log_event()`
for an event present in `EVENT_CATALOG` is "structured" — its `content` dict is
known to be the only content-bearing part, so the hosted factory can drop
`content` (and `exc_info`, always, hosted or not) while keeping `event` and
`fields` intact. Any record that is not structured this way — the large
existing body of `log.info("...")` calls across the codebase — keeps today's
full-blanking behavior unchanged. This is deliberately conservative: adopting
`log_event()` at a call site is what earns it field-level redaction; nothing
is reclassified by guessing at message shape.

## The anyio context-copy trap (O3)

The MCP tool wrapper (`command_surface.py`) runs synchronously inside FastMCP's
threadpool; `CallTraceMiddleware.on_call_tool` runs in the async request task.
A `ContextVar` set inside the wrapper does **not** propagate back to the
middleware — anyio's `to_thread.run_sync` copies the context into the worker
thread but any mutation made there is invisible to the caller once the thread
call returns. The failure signal therefore cannot be a ContextVar. Instead the
wrapper records into a bounded, lock-protected module-level dict,
`_TOOL_FAILURES[request_id] = {code, ...}`, right before it returns the failure
envelope. `CallTraceMiddleware` unconditionally pops `request_id` out of that
dict after `call_next` returns (success or not) and logs `tool_failure
code=...` instead of `tool_success` when an entry was present. "Unconditional"
matters twice: a call that never touched the dict pops `None` and logs
`tool_success` as before, and a call whose middleware layer never runs (there
isn't one, currently, but future direct-call paths might not) cannot leak an
entry forever — a bounded TTL sweep on the dict guarantees eventual cleanup
even if a pop is ever missed.

The envelope returned to the client is unaffected by any of this: the wrapper
builds it exactly as it does today and O3 only adds logging/metrics calls
around the existing `return cli_ops.envelope(False, error=...)` sites.

## Metrics: soft-fail and snapshot placement

`metrics.py` is one process-wide registry (counters + fixed-bucket histograms)
protected by a single `threading.Lock`. Every public method
(`inc`/`observe`/`snapshot`) catches and swallows its own errors — a metrics
call is never allowed to raise into the caller, matching the O1 logging
invariant. The registry is snapshotted to the writer-lease `state_dir` (the
same directory the idempotency store and mutation-lock sidecar already use)
rather than the log directory, because it is process-restart state, not a log:
a snapshotter thread started from `server_runtime.py` writes it atomically
(temp file + `os.replace`) every `EXOMEM_METRICS_SNAPSHOT_SECONDS` (default 60;
`0` disables the thread entirely), and the registry restores from that file at
process start so counters survive a restart instead of resetting to zero. No
network endpoint is added yet — `/metrics.json` is O6, a later commit on this
same registry, kept deliberately separate so the registry itself lands and is
tested before anything serves it.

## Per-process log files

Windows cannot share a single `RotatingFileHandler` across processes (the
rename-on-rotate step fails when another process holds the file open), and
Exomem already runs as more than one process kind from one checkout: the
long-lived server, one-shot CLI invocations, and the media worker child. Each
gets its own file (`exomem.log`, `exomem-cli.log`, `exomem-media.log`) under
the same `resolve_log_dir()` directory, so nothing contends for the same
handle and each stream stays independently parseable and rotatable.

## Restart scripts stop deleting the log

`scripts/restart.ps1`/`restart.sh` truncated (Windows) or deleted
(cross-platform intent, though the shipped behavior is delete) `exomem.log` on
every restart specifically so the post-restart tail showed only the new
session. That already-useful intent is kept, but the previous session's log is
now archived to `logs/archive/` instead of discarded, with the newest 10
archives retained and older ones pruned. The NSSM `service.out.log`/
`service.err.log` online-rotation pile (`AppRotateOnline 1`) accumulates
timestamped files with no built-in cap; the same restart pass prunes it to the
newest 20. The plain tail-of-`exomem.log` fallback stays for the case where the
file does not exist yet (a fresh install, or the process hasn't written its
startup banner).
