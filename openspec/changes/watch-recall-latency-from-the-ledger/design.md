## Context

`ledger.jsonl` already records what a regression guard needs: one row per MCP
call with `tool`, `client_name`, `total_ms`, per-stage `spans` and the
`budget` block, all content-free by construction. What is missing is a reader
that turns those rows into a verdict and puts the verdict where the person who
pays for the latency will see it. The two regressions that motivate this change
were both attributable from the spans alone (`embeddings.matrix_load`
`reason=cold` on every recall; `embeddings.encode` with ~900 texts inside
`recall.pack`), so the guard reports dominant spans, not only percentiles.

## Decisions

### D1. The watch is fed at the ledger write site, not by reading the file

`command_surface` calls `call_ledger.record_call` once per MCP call with the
tool name, the client, the wall time and the spans in hand, and it still holds
the real arguments, so `deep` is classified there as a boolean before the
ledger reduces it to a length and a hash. `latency_watch.observe(...)` is called
beside `record_call`, under the same "never breaks or slows the call" rule the
ledger has: every failure is swallowed and counted. The ring is bounded (2,048
entries, oldest evicted) and holds no query text, path, excerpt or argument
value: tool, client name, deep flag, `total_ms`, timestamp, and the top five
span names with their milliseconds.

Reading the file on the request path was rejected: bootstrap is the first call
of every session and a multi-megabyte JSONL parse would add more latency than
the guard is meant to catch.

### D2. Ceilings are PROVISIONAL constants in one module

`latency_watch.py` holds `RECALL_P90_CEILING_MS = 1000` (for `ask_memory`
without `deep`, `read_memory`, `find`), `DEEP_RECALL_P90_CEILING_MS = 5000`
(for `ask_memory` with `deep`; the pack stage alone is 3.5 to 4.5 s today, and
this ceiling is lowered when that changes), `MIN_SAMPLES = 20`,
`WINDOW_SECONDS = 86400`, `STARTUP_GRACE_SECONDS = 600` and
`REPORT_INTERVAL_SECONDS = 3600`. No environment override: a ceiling that can
be raised from the environment is a ceiling nobody trusts. The values are named
in the spec as provisional so a later change can revise them without a design
argument.

### D3. Bootstrap reports only a breach, and only to the client breaching

The `latency` block appears on the bootstrap response only when the calling
client's trailing p90 for a tool is over its ceiling with at least
`MIN_SAMPLES` samples after the startup grace. The block is
`{tool, deep, samples, p50_ms, p90_ms, ceiling_ms, dominant_spans:[{name, ms,
calls}]}` per breaching tool. A healthy service returns today's response
shape, which keeps the surface fingerprint and every existing bootstrap test
unchanged, the same rule the `budget` block on recall responses follows. The
block is computed from the ring on demand (a few thousand integers) and never
touches the file.

### D4. Doctor reads the file, so it sees across restarts

`exomem doctor` `latency` reads `ledger.jsonl` and, when the window reaches
past it, the newest archive generations, for the trailing `WINDOW_SECONDS`,
parsing rows with the tolerance the observability check already uses. It
reports per (tool, client): samples, p50, p90, ceiling, and for calls over the
ceiling the five spans with the most total milliseconds. Status is `warn` above
a ceiling, `pass` otherwise, and `pass` with a note when fewer than
`MIN_SAMPLES` rows exist. Doctor does not apply the startup grace: it is the
diagnosis surface and a cold window after a promotion is itself worth seeing;
the dominant spans (`recall.due_state.build`, `embeddings.matrix_load`
`reason=cold`) say what it was.

### D5. One log event per breach per hour

When the ring's verdict for a (tool, client, deep) first turns to breach, the
watch logs `event=latency_ceiling_exceeded` with the same content-free fields
as the bootstrap block, at most once per `REPORT_INTERVAL_SECONDS` per key.
It is emitted from the observe call's own thread, after the row is recorded,
inside the same failure guard.

## Alternatives considered

- Gate in CI only: cannot see live state; both motivating regressions were
  invisible to the synthetic gate.
- A metrics histogram alert: `exomem_tool_duration_ms` exists, but it is
  per-tool only (no client, no deep flag, no spans) and is a fixed-bucket
  histogram, so it cannot name the dominant stage.
- Refusing or degrading calls over the ceiling: rejected; the request budget
  already bounds a single call, and a guard that changes results to report a
  regression costs more than the regression.
