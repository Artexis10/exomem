# Design - publish retrieval benchmark

## Context

`scripts/eval_retrieval.py` already runs the golden set (`tests/golden/queries.yaml`) against a
resolved vault (`exomem.vault.resolve_vault()`) with live embeddings forced on, computing
NDCG@5/NDCG@10/MRR/recall@10 per query via `src/exomem/eval_metrics.py` and mean-aggregating them
in `_evaluate()`. It already supports `--sweep` (grid search over `RankingConfig` knobs) and a
`--markdown` flag that prints one table for whichever single run just happened. There is no
existing multi-mode aggregate table, no latency measurement, and no corpus-stats output anywhere
in the script.

`find()` (`src/exomem/find.py`) exposes `mode: "keyword" | "hybrid" | "vector"` and an orthogonal
`rerank: bool | None`, plus an optional `timings: FindTimings | None` recorder populated in place
(stage spans via `time.perf_counter()`, exposing `total_ms` and per-stage entries) — added by the
already-implemented `improve-find-latency-token-cost` change. That recorder is designed for a
single `find()` call's stage breakdown, not for aggregating a latency distribution across many
calls.

`eval_metrics.py`'s docstring is explicit about staying "pure ranking-quality metrics... No torch,
no exomem imports" so it's importable in the fast, embeddings-free test suite. That precedent is
the model for how this change keeps its new reporting logic testable.

## Goals / Non-Goals

**Goals:**

- Emit one reproducible markdown artifact per harness run: per-mode (keyword/hybrid/hybrid+rerank)
  aggregate ranking-quality metrics (the four the harness already computes), per-mode latency
  percentiles, and rounded corpus counts.
- Keep the published artifact aggregate-only: no golden query text, no vault-relative paths, no
  excerpts, no per-query rows.
- Keep the new reporting logic unit-testable on fixture data without a real vault, models, or
  torch, matching the `eval_metrics.py` precedent.
- Keep CI's role limited to the ordinary fixture-data unit tests; do not make CI depend on a real
  vault or downloaded models, and do not gate PRs on retrieval-quality numbers.

**Non-Goals:**

- No change to `find()`'s ranking, default behavior, timing diagnostics, caching, or freshness
  contracts — this change only adds a reporting mode on top of the existing harness.
- No adoption of a third-party benchmark harness (e.g. LongMemEval) in this change.
- No per-query or per-path publication, ever, from `--report markdown`.
- No new MCP/REST/CLI command-registry surface; `scripts/eval_retrieval.py` stays a standalone
  dev/eval tool, as it is today.

## Decisions

### The report is a new, additive `--report markdown` mode, not a replacement for `--markdown`

`--report markdown` runs the golden set once per mode in `["keyword", "hybrid", "hybrid+rerank"]`
(the third being `mode="hybrid", rerank=True`), reusing `_evaluate()` per mode for the four
existing metrics, and separately samples latency by repeating the golden set `--repeat N` times
(default small N, e.g. 3) per mode and recording each call's wall-clock time. Existing `--sweep`
and the current single-run `--markdown` table are untouched — a reader who wants the ranking-knob
grid search still gets it exactly as today.

Alternative considered: fold benchmark reporting into `--sweep`. Rejected because `--sweep` is a
ranking-config search tool (rrf_k x compiled_boost x rerank), while the published benchmark is
about mode comparison at the shipped default config — conflating the two would make `--sweep`'s
output ambiguous about which config produced the published numbers.

### Latency is measured by direct wall-clock sampling in the harness, not by threading `FindTimings` through

The report only needs an end-to-end latency distribution (median/p90) per mode, not a per-stage
breakdown. `_evaluate()`/`rank_queries()` wrap each `find_module.find(...)` call with
`time.perf_counter()` before/after (the same lightweight-span pattern `find.py` itself already
uses internally for `FindTimings`), collecting a flat list of per-call latencies per mode across
the `--repeat` runs, then compute median and p90 from that list.

Alternative considered: pass a `FindTimings()` instance into every call and read `.total_ms` off
it. Rejected because it couples the benchmark script to `FindTimings`' internal per-stage key
names for a value (`total_ms`) that a bare `time.perf_counter()` delta already gives directly,
without needing to construct or discard a recorder object per call.

### Corpus counting and report rendering live in a new pure module, not in `eval_retrieval.py` or `eval_metrics.py`

New `src/exomem/eval_report.py`: no torch import, no live-vault network/model access. It exposes:

- `count_corpus_stats(vault_root: Path) -> dict`: a plain filesystem walk that buckets markdown
  files into rounded counts (e.g. rounded down to the nearest 10) for files/notes/media, so the
  function's *output* is privacy-safe by construction rather than relying on every caller to round
  before publishing.
- `render_benchmark_report(*, corpus: dict, per_mode: dict, golden_n: int, meta: dict) -> str`: a
  plain-data-in, markdown-string-out function with no vault or query access at all — it cannot
  leak what it was never given.

`scripts/eval_retrieval.py` calls both from its new `--report markdown` branch; it still owns
driving `find()` and the golden set (unchanged responsibility).

Alternative considered: add these functions to `eval_metrics.py`. Rejected because that module's
scope is ranking-quality math (DCG/NDCG/MRR/recall) reused by both the harness and the auto-tuner;
folding in corpus counting and markdown presentation would blur that module's single concern and
make its "pure metrics" docstring inaccurate. A sibling module keeps both scopes clean and keeps
`eval_metrics.py`'s existing tests and callers untouched.

Alternative considered: test `scripts/eval_retrieval.py` directly (via `importlib.util`, the
pattern `tests/test_scaffold_no_leak.py` already uses for `scripts/genericize-schema.py`).
Rejected because `eval_retrieval.py`'s module top level currently pops
`EXOMEM_DISABLE_EMBEDDINGS` and imports `exomem.find`/`exomem.embeddings` unconditionally, as a
deliberate "this script MUST run with live vectors" guarantee; importing that module from a test
process would mutate the embeddings-disable env var for the rest of the pytest session and is not
worth restructuring the script's carefully-ordered head-of-file setup just to make it importable.
A new pure sibling module sidesteps the problem entirely instead of touching that contract.

### Aggregate-only publication is enforced by giving the pure function no leak-capable inputs, backed by a regression test

`render_benchmark_report()` never receives query strings or paths — its `per_mode` argument is
`{mode: {"ndcg5": float, "ndcg10": float, "mrr": float, "recall10": float, "latency_median_ms":
float, "latency_p90_ms": float}}`, and `corpus` is `{"files": int, "notes": int, "media": int}`
(already rounded). This makes the "no per-query rows, no query text, no paths" contract structural,
not just a code-review convention. A test still greps the rendered output for every golden query
string and every golden target path from `tests/golden/queries.yaml` as a regression backstop —
mirroring `tests/test_scaffold_no_leak.py`'s pattern-denylist posture — in case a future edit
widens `meta` or `per_mode` to include something leak-capable.

### Golden-set self-eval is published as honest-enough, with limitations stated prominently

The golden set is 9 hand-authored queries against a single private vault, self-graded (the author
picks the relevant path(s)), and designed to grow via `scripts/derive_relevance_pairs.py` mining +
human confirmation. `docs/benchmarks.md` states this directly in a Limitations section (n of
queries, single-vault, self-graded relevance) rather than presenting the numbers as if they came
from an independent benchmark. This is still strictly more transparent than either comparison
point in `## Why`: a harness that reports no results, or no harness at all.

Alternative considered: adopt LongMemEval so results are comparable to whichever other project
publishes against it. Rejected — LongMemEval targets long-horizon conversational memory QA and
session summarization, a different task shape from exomem's single-shot retrieval over an owned
markdown vault (typed sources/notes/entities/evidence with provenance and multimodal extraction).
Bolting on an unrelated benchmark harness for comparability would cost real implementation effort
for a comparison that doesn't map cleanly onto what exomem's `find()` actually ranks, when the
existing golden-set harness already measures the retrieval behavior exomem ships today.

Alternative considered: publish per-query rows so a reader can audit individual results. Rejected
per the Aggregate-Only Publication requirement — per-query rows are 1:1 with golden query text
and, once the golden set grows from real mined usage, would risk surfacing what a private vault
actually contains.

### CI runs the new tests but does not gate on retrieval quality

The new `eval_report.py` unit tests (corpus counting against `tests/fixtures/`, rendering against
synthetic aggregate data, and the privacy-guard grep) are ordinary fast, model-free tests and run
in the existing lean `pytest -q` CI job like any other test — no new CI job is added. Running
`--report markdown` against a real vault stays a desk-side command, matching the
`add-docker-distribution` change's precedent of marking steps that need resources CI doesn't have
(there: a local Docker daemon; here: a real vault + downloaded embedding/reranker models) as
explicitly desk-side rather than silently skipped or falsely claimed as covered.

## Risks / Trade-offs

- Latency numbers are host/GPU-dependent and will look different on every machine that reproduces
  them -> `docs/benchmarks.md`'s methodology states the hardware/versions the published numbers
  were measured on, and the Limitations section says explicitly that a third party's numbers will
  differ by hardware, not that the published p90 is a portable guarantee.
- A future edit to `per_mode`/`meta` could widen `render_benchmark_report()`'s inputs to include
  something leak-capable (e.g. an excerpt or a path) -> the privacy-guard regression test greps
  rendered output against every golden query string and target path, so a leak-capable addition
  fails a fast, already-passing test rather than surfacing only in the published doc.
- `docs/benchmarks.md`'s results table cannot be filled with real numbers as part of this OpenSpec
  authoring pass (no vault/model access here) -> `tasks.md` marks that step explicitly as a
  desk-side, real-vault task to complete at implementation time, not silently left inconsistent
  with the skeleton.
- Corpus-count rounding could still be inferred/narrowed across repeated publications over time if
  the vault grows slowly -> out of scope for this change; noted as a residual risk rather than
  solved, since the rounding-only contract is materially better than exact counts and no vault
  content is ever included.

## Migration Plan

No data or schema migration. This is additive dev/eval tooling and documentation; no existing
`find()` caller, MCP tool, REST route, or CLI command changes shape or behavior. Existing usage of
`scripts/eval_retrieval.py` without `--report markdown` is unaffected.

## Open Questions

None for implementation. The real-vault run that fills in `docs/benchmarks.md`'s results table is
explicitly deferred to implementation time (see `tasks.md`), not resolved by this design.
