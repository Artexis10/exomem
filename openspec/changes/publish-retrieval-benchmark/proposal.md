## Why

Neither comparable project publishes retrieval-quality numbers today: a LongMemEval-style
harness with zero published results is not evidence, and a competitor with no eval harness at
all is not evidence either. exomem already has a working golden-set eval harness
(`scripts/eval_retrieval.py`, `tests/golden/queries.yaml`, `src/exomem/eval_metrics.py`) and a
production-scale vault to run it against. A published, reproducible benchmark artifact —
methodology plus aggregate metrics — is a low-cost credibility differentiator versus doc-chat/RAG
apps and other MCP note servers: it turns "we think retrieval is good" into a number a reader can
re-derive.

This also rides along cheaply with the timing/observability work already landed in
`improve-find-latency-token-cost`: `find()` already accepts an optional `FindTimings` recorder and
the harness already drives `find()` directly, so wall-clock latency percentiles per mode are a
small addition to a harness that already runs the full golden set.

Corrected against code reality (the harness, read in full, wins over any earlier sketch):

- `scripts/eval_retrieval.py` runs `find()` against the **real vault** (it force-enables
  embeddings by popping `EXOMEM_DISABLE_EMBEDDINGS` before importing `exomem`), scoped
  `scope="kb-only"` so it never triggers the wider vault auto-widen scan. It writes nothing to the
  vault.
- It already computes exactly four metrics per query, via `src/exomem/eval_metrics.py`: NDCG@5,
  NDCG@10, MRR, and recall@10 (mean-aggregated in `_evaluate()`). There is no fifth metric hiding
  anywhere; the published table is these four, not an invented set.
- It already has a `--markdown` flag, but today that only prints a single-config table (baseline
  run, or one row per `--sweep` grid point) — there is no multi-mode (keyword/hybrid/hybrid+rerank)
  aggregate table, no latency numbers, and no corpus stats anywhere in the script.
- The golden set (`tests/golden/queries.yaml`) is a **seed set of 9 queries**, hand-authored against
  the bundled fixture KB (`tests/fixtures/`), explicitly designed to grow over time via
  `scripts/derive_relevance_pairs.py` mining real (query -> cited path) usage for human
  confirmation. Any published benchmark must say so plainly — this is a small, single-vault,
  self-graded golden set, not a third-party benchmark.
- `mode` on `find()` is one of `"keyword" | "hybrid" | "vector"`; reranking is an orthogonal
  boolean (`rerank=True/False`) rather than a fourth mode. "hybrid+rerank" in this proposal means
  `mode="hybrid", rerank=True`.

## What Changes

- `scripts/eval_retrieval.py` gains an additive `--report markdown` mode: runs the existing golden
  set once per retrieval mode (keyword, hybrid, hybrid+rerank), reusing the harness's existing
  `_evaluate()` aggregation for NDCG@5/NDCG@10/MRR/recall@10 per mode, plus median/p90 `find()`
  wall-clock latency per mode sampled over repeated golden-set runs, plus rounded corpus counts
  (markdown files, notes, media) computed by a pure, model-free filesystem walk. The emitted
  artifact is aggregate-only: no query text, no vault-relative paths, no excerpts, no per-query
  rows. Existing `--sweep`/baseline `--markdown` behavior is unchanged.
- A new pure module, `src/exomem/eval_report.py` (no torch, no live-vault access), holds the
  corpus-counting and markdown-rendering logic as plain-data functions — mirroring
  `eval_metrics.py`'s existing "pure, torch-free, unit-testable" precedent — so the reporting
  logic is testable on fixture data without a real vault or models.
- New `docs/benchmarks.md` (skeleton in this change; real numbers filled at implementation time
  by an actual desk-side run against the private vault): methodology (golden-set construction,
  vault scale as rounded counts, hardware line, model/version line), the results table, reproduction
  instructions for a third party (their own vault + golden queries, or the bundled
  `tests/fixtures` sample vault as a deterministic smoke), and an explicit limitations section (n
  of queries, single-vault, self-graded golden set).
- `README.md` gains a short "Measured retrieval quality" note near the existing comparison table,
  linking to `docs/benchmarks.md`.
- Tests: fixture-data unit tests for `eval_report.py`'s corpus counting (against
  `tests/fixtures/`, deterministic, no models) and markdown rendering (synthetic aggregate
  inputs), plus a privacy-guard test asserting the rendered report contains none of the golden
  set's query strings or target paths — mirroring this repo's existing leak-guard posture
  (`tests/test_scaffold_no_leak.py`).
- CI is **not** a gate on retrieval-quality numbers: no real vault or downloaded models are
  available in CI, so `--report markdown` against a live vault stays a desk-side command. The new
  fixture-data unit tests are ordinary, fast, model-free tests and run in the existing lean pytest
  job like any other test.

## Capabilities

### Modified Capabilities

- `find-recall-efficiency`: adds a reproducible benchmark-report mode for the existing retrieval
  eval harness and an aggregate-only publication contract for that report. No change to `find()`
  behavior, ranking, timing diagnostics, caching, or freshness requirements already specified for
  this capability.

### New Capabilities

- None.

## Impact

- Code: `scripts/eval_retrieval.py` (new `--report markdown` mode, latency sampling loop), new
  `src/exomem/eval_report.py` (pure corpus-counting + markdown-rendering module).
- Docs: new `docs/benchmarks.md`; `README.md` gains a short linking note.
- Tests: new fixture-data unit tests for `eval_report.py` (corpus counting, report rendering) and a
  privacy-guard test for the rendered report.
- No MCP/REST/CLI command-registry change, no schema change, and no change to `find()`'s
  production ranking/return behavior — this change only adds dev/eval tooling and documentation
  around the existing harness.
- Dependencies: none expected. No optional model becomes mandatory; `--report markdown` still
  requires the same live-vector environment the harness already requires today.
