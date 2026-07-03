# Tasks — publish retrieval benchmark

## 1. Tests First (pure logic, no torch, no live vault)

- [x] 1.1 `tests/test_eval_report.py::test_count_corpus_stats_against_fixtures`: run
      `eval_report.count_corpus_stats(Path("tests/fixtures"))` and assert it returns rounded
      `files`/`notes`/`media` integer counts (bucketed, e.g. rounded down to the nearest 10) that
      are internally consistent with the known small fixture tree, without asserting on exact
      filenames.
- [x] 1.2 `tests/test_eval_report.py::test_render_benchmark_report_shape`: call
      `eval_report.render_benchmark_report(...)` with synthetic `per_mode` (keyword/hybrid/
      hybrid+rerank, made-up metric and latency numbers), synthetic `corpus` counts, a golden `n`,
      and a `meta` dict; assert the returned markdown contains a row per mode with all four
      existing metrics (NDCG@5, NDCG@10, MRR, recall@10) plus latency median/p90, the corpus
      counts, and a limitations note — using only invented numbers, never real query/path data.
- [x] 1.3 `tests/test_eval_report.py::test_report_has_no_leaked_golden_content` (privacy guard):
      load `tests/golden/queries.yaml`, collect every `query` string and every `expect_any_of`/
      `graded` target path; render a report via 1.2's synthetic inputs plus a second render that
      exercises whatever code path is closest to production use; assert none of the golden
      query strings or target paths appear anywhere in the rendered markdown. Mirrors
      `tests/test_scaffold_no_leak.py`'s pattern-denylist posture.
- [x] 1.4 `tests/test_eval_report.py::test_render_benchmark_report_omits_per_query_rows`: assert
      the rendered markdown has exactly one row per mode (not one row per query) — i.e. row count
      scales with `len(per_mode)`, not with `golden_n`.

## 2. `src/exomem/eval_report.py` (new pure module)

- [x] 2.1 Add module docstring mirroring `eval_metrics.py`'s "no torch, no live-vault access"
      framing; implement `count_corpus_stats(vault_root: Path) -> dict[str, int]` as a plain
      `Path.rglob` walk bucketing by known type folders/extensions (Sources/Articles, Sources/
      Books, Sources/Sessions, Notes/*, media file extensions), rounding every returned count
      before returning it so the function's output is privacy-safe by construction. 1.1 green.
- [x] 2.2 Implement `render_benchmark_report(*, corpus, per_mode, golden_n, meta) -> str`: plain
      dict/int/str inputs only (no `Path`, no vault, no query text parameter at all) producing a
      markdown string — header (title, `meta` fields such as exomem version, model names,
      hardware line), corpus-counts line, one metrics+latency row per mode, golden-set size, and a
      limitations paragraph. 1.2, 1.4 green.
- [x] 2.3 Confirm 1.3 (privacy guard) passes given 2.1/2.2's signatures accept no leak-capable
      input; add a short comment at each function noting the no-path/no-query-text contract so a
      future edit sees the constraint before widening the signature.

## 3. `scripts/eval_retrieval.py`: `--report markdown` mode

- [x] 3.1 Add `--report {markdown}` and `--repeat N` (default e.g. 3) CLI args, additive to the
      existing `--sweep`/`--markdown`/`--rerank`/`--include-rerank` args (no existing flag
      behavior changes).
- [x] 3.2 When `--report markdown` is set: for each mode in `["keyword", "hybrid",
      "hybrid+rerank"]` (the third being `mode="hybrid", rerank=True`), call the existing
      `_evaluate()` once for the four aggregate metrics, then separately time `--repeat` full
      passes over the golden set with `time.perf_counter()` around each `find_module.find(...)`
      call (reusing `rank_queries()`'s per-query loop shape, not `FindTimings`), collecting a flat
      per-mode latency list and computing median/p90 from it.
- [x] 3.3 Call `eval_report.count_corpus_stats(vault_root)` once (same resolved vault root the
      harness already uses) and `eval_report.render_benchmark_report(...)` with the three modes'
      results, corpus counts, `len(golden)`, and a `meta` dict (exomem version, `BAAI/
      bge-base-en-v1.5` / `BAAI/bge-reranker-base` model names, a hardware-line placeholder field
      the runner fills at invocation or leaves generic). Print the rendered markdown to stdout
      (matching the existing `--markdown` precedent of printing rather than writing a file).
- [x] 3.4 Verify existing `--sweep`, baseline `--markdown`, and `--rerank` flags are byte-identical
      in behavior to before this change (no regression in the existing dev workflow).

## 4. Docs skeleton

- [x] 4.1 New `docs/benchmarks.md`: Methodology section (golden-set construction and size,
      `tests/golden/queries.yaml` provenance and growth process via
      `scripts/derive_relevance_pairs.py`, vault scale as rounded counts, hardware line, exomem
      version + model/version line), a Results section with the table structure from
      `render_benchmark_report()`'s shape but **placeholder values** clearly marked
      `TODO(real-vault-run)`, a Reproduction section (run against your own vault + golden queries,
      or `EXOMEM_VAULT_PATH=tests/fixtures` as a deterministic smoke), and a Limitations section
      (n of queries, single-vault, self-graded golden set, hardware-dependent latency).
- [ ] 4.2 **STILL DESK-SIDE — intentionally left unchecked.** Requires the private vault and
      downloaded embedding/reranker models, neither available in this implementation environment
      (torch / sentence_transformers are not installed here; those suite tests skip). The
      `--report markdown` mode is implemented and wired (tasks 3.1–3.4 done), but the real-vault
      measurement pass has NOT been run. To complete: run
      `EXOMEM_VAULT_PATH=/path/to/vault python scripts/eval_retrieval.py --report markdown`
      against the real vault and replace `docs/benchmarks.md`'s `TODO(real-vault-run)` placeholders
      with the measured table. (Use the repo's own invocation; do not run `uv run`.)
- [x] 4.3 `README.md`: add a short "Measured retrieval quality" note near the existing comparison
      table (the `Compared with` table / the `docs/comparison-engraph.md` link), pointing at
      `docs/benchmarks.md`.

## 5. Validation

- [x] 5.1 `.venv/Scripts/python.exe -m pytest -q` green — full suite 1268 passed, 11 skipped
      (pre-existing torch/sentence_transformers/PIL/av/diarizer skips), including the new
      `tests/test_eval_report.py` (4 tests, all model-free and passing under the lean default).
- [x] 5.2 `ruff check` clean on changed files (`src/exomem/eval_report.py`,
      `tests/test_eval_report.py`, `scripts/eval_retrieval.py`) — "All checks passed!".
- [x] 5.3 `npm exec --yes @fission-ai/openspec -- validate publish-retrieval-benchmark --strict`
      passes.
- [x] 5.4 No MCP/REST/CLI command-registry file changed and no `tests/fixtures/
      mcp_tool_schemas.json` drift — this change touches only dev/eval tooling (`eval_report.py`,
      `eval_retrieval.py`), docs, tests, and this change's own tasks.
- [x] 5.5 CI gained no new job — the new tests run inside the existing lean `pytest -q` job; the
      real-vault `--report markdown` run stays desk-side (task 4.2), not a CI step.
