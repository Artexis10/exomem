## Why

The existing benchmark story has three measured gaps. First, the basic-memory comparison
(2026-07-07/08, KB research notes) measured basic-memory through its one-shot CLI wrapper
(~6.6–7.1 s/query, 15.1-minute first index) — but never as a persistent MCP server, which is
both its intended runtime and the fair fight; its raw SQLite FTS is known to be fast. Second,
memory usage has no benchmark lane: RSS was captured exactly once (psutil, ad-hoc) during the
vec-backend comparison, so memory optimizations have no acceptance instrument. Third, one-shot
CLI startup (~11–12 s, Python import cost) has no before/after gate, so the planned lazy-import
work cannot be verified. `docs/benchmarks.md` publishes retrieval quality and per-lane latency
but cannot answer "is exomem faster and lighter than basic-memory in like-for-like serving?"
with receipts.

## What Changes

- New `scripts/compare_memory_servers.py`: drives exomem (`--transport stdio`) and basic-memory
  0.22.1 (`uvx --from basic-memory`, safe-read env knobs) as persistent MCP servers over the
  same `scripts/synth_vault.py` corpus; measures first-index/cold-start wall time, warm search
  latency distribution (median/p90 over a fixed query set), server RSS after index and after
  the query pass, and write-tool latency. Emits an aggregate-only markdown report with the
  fairness contract documented (embedding dims, tool-surface deltas, host).
- `scripts/latency_curve.py` gains an `--rss` flag recording process RSS (psutil, optional
  dependency, graceful degradation) after warm and after the query pass, per tier and backend.
- New `scripts/startup_benchmark.py`: parses `python -X importtime` for `exomem.__main__`,
  times `exomem --help` and a one-shot model-free product command, and emits a small table.
- New `docs/comparison-basic-memory.md` (precedent: `docs/comparison-engraph.md`) publishing
  methodology + measured results, with a reproduction section; summary row in
  `docs/benchmarks.md`.

## Impact

- Affected specs: `find-recall-efficiency` (comparison harness, RSS lane),
  `install-readiness` (startup benchmark).
- Affected code: `scripts/` additions only, plus docs. No product-runtime changes; lean CI
  unaffected (harnesses are desk-side; no new required dependency — psutil stays optional).
- basic-memory is exercised via `uvx` pinned to the published 0.22.1; no basic-memory code is
  vendored or copied.
