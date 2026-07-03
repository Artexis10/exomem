# Measured retrieval quality

exomem ships a golden-set eval harness, so retrieval quality is a number you can
re-derive, not a claim. This page documents the methodology and the measured
results. It is deliberately transparent about what the numbers are and are not:
a small, single-vault, self-graded benchmark — strictly more evidence than "no
published results" or "no harness at all", but not an independent third-party
benchmark.

> The results table below was measured with `scripts/eval_retrieval.py` against
> the **bundled fixture vault** (`tests/fixtures/`) — a deterministic, tiny,
> public corpus, so the exact run reproduces from a clean checkout with no
> private data (it needs the downloaded embedding + reranker models, which the
> lean CI matrix does not have; the dedicated `retrieval-eval` job does). The
> same regression floors are asserted in `tests/test_retrieval_golden.py`. See
> [Reproduction](#reproduction) to re-run it or point it at your own vault.

## Methodology

- **Harness.** `scripts/eval_retrieval.py --report markdown` runs the golden set
  once per retrieval mode and emits an aggregate-only markdown report: per-mode
  ranking-quality metrics, per-mode `find()` latency percentiles, and rounded
  corpus counts. The report contains no query text, no vault-relative paths, no
  excerpts, and no per-query rows — only aggregates.
- **Modes.** Three, at the shipped default ranking config:
  - `keyword` — lexical (BM25) only.
  - `hybrid` — lexical + vector fusion (the default).
  - `hybrid+rerank` — `hybrid` with the cross-encoder reranker on (`rerank=True`;
    reranking is orthogonal to mode, not a fourth mode).
- **Metrics.** The four the harness already computes per query and mean-aggregates
  (`src/exomem/eval_metrics.py`): **NDCG@5**, **NDCG@10**, **MRR**, **recall@10**.
- **Latency.** Wall-clock `find()` time is sampled by repeating the golden set
  `--repeat N` times (default 3) per mode and timing each call with
  `time.perf_counter()`. The report shows the **median** and **p90** (nearest-rank
  percentile) over that flat sample.
- **Golden set.** `tests/golden/queries.yaml` — a seed set of **9** hand-authored
  natural-language queries, each with one or more relevant target pages
  (`expect_any_of`, or `graded` 0–3 for NDCG). It is designed to grow:
  `scripts/derive_relevance_pairs.py` proposes additions mined from real
  (query → cited path) usage, which a human confirms.
- **Vault scale (rounded).** Reported as counts rounded DOWN to the nearest 10
  (privacy floor): markdown files (whole vault), KB notes (the pages `find()`
  indexes), and media artifacts (audio/video/image/pdf).
- **Embedding sidecar required.** The eval reads a prebuilt embedding sidecar (a
  real vault's running server keeps it fresh). The bundled fixture ships without
  one, so the run below builds it first with
  `embeddings.get_embedding_index(vault).rebuild_all()`. Without that step the
  vector lane is empty and `hybrid` collapses to `keyword` — which is exactly the
  silent-degradation state the doctor sidecar probe and the `find` `degraded`
  marker now surface.
- **exomem version / models.** exomem `0.4.1`, embedding model
  `BAAI/bge-base-en-v1.5`, reranker `BAAI/bge-reranker-base`.
- **Hardware.** AMD Ryzen 7 5800X3D (8-core) / NVIDIA RTX 5080 / 32 GB RAM /
  Windows 11.

## Results

Measured 2026-07-03 against the bundled fixture vault (sidecar built first).

- Corpus scale: 10 markdown files, 10 KB notes, 0 media artifacts (rounded down
  to the nearest 10).
- Golden set: 9 queries.

| Mode | NDCG@5 | NDCG@10 | MRR | recall@10 | latency median (ms) | latency p90 (ms) |
|---|---|---|---|---|---|---|
| keyword | 0.2222 | 0.2222 | 0.2222 | 0.2222 | 4.8 | 5.7 |
| hybrid | 0.9590 | 0.9590 | 0.9444 | 1.0000 | 4.7 | 5.4 |
| hybrid+rerank | 0.9971 | 0.9971 | 1.0000 | 1.0000 | 6.2 | 6.8 |

The gap is the whole point: on this golden set lexical-only (`keyword`) surfaces
the intended page for just 2 of 9 queries (NDCG@10 0.2222), while `hybrid` finds
every target in the top-10 (recall@10 1.0) and ranks it first or second (NDCG@10
0.9590, MRR 0.9444); the cross-encoder reranker then all but perfects the order
(NDCG@10 0.9971, MRR 1.0). Latency here is dominated by fixed per-call overhead —
the corpus is 77 chunk vectors — so treat the absolute ms as a floor, not a
representative production figure.

## Reproduction

The report reproduces in the same shape against any vault + golden set that
follow the harness contract. It needs live vectors (the embedding/reranker
models), so the full `--report markdown` is a desk-side command; CI asserts the
lighter regression floors (mean NDCG@10 / MRR / recall@10, plus a per-query
"nothing dropped to recall@10 = 0" guard) in the dedicated `retrieval-eval` job
via `tests/test_retrieval_golden.py`.

Against your own vault and golden queries:

```
EXOMEM_VAULT_PATH=/path/to/your/Obsidian \
  python scripts/eval_retrieval.py --report markdown --golden tests/golden/queries.yaml
```

As a deterministic, smoke-scale run against the bundled fixture vault (no private
vault required — same report shape, tiny corpus). Because the fixture ships
without an embedding sidecar, build one first, in a throwaway copy so the repo
tree stays clean (this is exactly what `tests/test_retrieval_golden.py` does):

```
cp -r tests/fixtures /tmp/exomem-fixture
EXOMEM_VAULT_PATH=/tmp/exomem-fixture python -c \
  "from pathlib import Path; from exomem import embeddings; \
   print('chunks:', embeddings.get_embedding_index(Path('/tmp/exomem-fixture')).rebuild_all())"
EXOMEM_VAULT_PATH=/tmp/exomem-fixture python scripts/eval_retrieval.py --report markdown
```

Skip the build step and every mode reports the same `keyword` numbers — the
vector lane has nothing to read. Set `EXOMEM_BENCH_HARDWARE` to record the host
in the report's methodology line.

## Limitations

- **Small.** n = 9 golden queries. Individual metric moves are noisy at this size.
- **Tiny corpus.** The published numbers are measured on the bundled fixture
  vault — ~10 KB notes, 77 chunk vectors. They show the ranking *works* (hybrid
  ≫ keyword, every target recalled), not production-scale quality, and do not
  generalize to larger or different corpora. Point the harness at your own vault
  for a representative read.
- **Self-graded.** Relevance labels are hand-authored (by the maintainer, not
  independent annotators) — a self-measurement, not a third-party benchmark.
- **Hardware-dependent latency.** The median/p90 numbers reflect one host's
  CPU/GPU. A third party's latency will differ; the published p90 is not a
  portable guarantee.
