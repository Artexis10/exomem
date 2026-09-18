## Why

Two latency regressions on the personal service went unnoticed for days because
nothing reads the call ledger back. The idle reaper evicted the recall RAM
caches every minute for weeks (every recall paid a cold matrix load and page
re-parse), and every deep recall that packed a media transcript re-ran the
semantic segmenter, which encodes hundreds of texts per request (80 to 110 s
inside `recall.pack`). Both were visible in `ledger.jsonl` the whole time: the
rows carry `total_ms` and per-stage `spans`, and the dominant span named the
cause in each case. The only consumers of that file today are `exomem logs`,
`exomem trace` and a hand-written script, so a regression is found when a
person gets annoyed enough to go and look.

The CI latency gate cannot stand in for this. It runs a model-free 2,000-note
synthetic vault on a fresh process; neither regression exists there, because
both depend on live state (a long-lived process, a vault with transcripts).

## What Changes

- A latency watch fed by the same site that writes the ledger row: a bounded,
  in-memory, content-free ring of recent recall calls (tool, client, deep or
  not, `total_ms`, top spans) with the trailing-window p50/p90 per tool and
  client, compared against ceilings held as PROVISIONAL constants in one
  module. Calls in the first minutes after process start do not count toward
  the verdict, so the known cold window after a promotion is not reported as a
  regression.
- `bootstrap` carries a `latency` block only while the calling client's
  trailing p90 breaches its ceiling: tool, sample count, p50, p90, the ceiling,
  and the spans that dominate the calls over it. A healthy service returns a
  bootstrap response byte-identical in shape to today's.
- `exomem doctor` gains a `latency` check that reads the ledger on disk for
  the trailing window (independent of process lifetime), reports the same
  figures per tool and client, warns above the ceiling, and names the dominant
  spans among the slow calls so the finding is a diagnosis, not a number.
- The service logs one structured `latency_ceiling_exceeded` event when a
  (tool, client) first crosses its ceiling, rate-limited to once per hour, so
  the regression is on the record even when nobody calls bootstrap or doctor.
- The CI latency gate is unchanged.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `call-ledger`: the ledger's write site also feeds a rolling latency watch;
  the watch is content-free and never slows or breaks the call.
- `agent-bootstrap-contract`: bootstrap surfaces a recall latency regression to
  the client that is experiencing it, and nothing otherwise.
- `install-readiness`: doctor gains a read-only `latency` check over the
  ledger.
