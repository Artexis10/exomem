# Measured retrieval quality

exomem ships a golden-set eval harness, so retrieval quality is a number you can
re-derive, not a claim. This page documents the methodology and the measured
results. It is deliberately transparent about what the numbers are and are not:
a small, single-vault, self-graded benchmark — strictly more evidence than "no
published results" or "no harness at all", but not an independent third-party
benchmark.

> The results table below is filled by a desk-side run against a real vault (it
> needs the downloaded embedding + reranker models, which CI does not have). The
> `TODO(real-vault-run)` cells are placeholders, not measurements — see
> [Reproduction](#reproduction) to generate your own.

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
- **exomem version / models.** exomem `TODO(real-vault-run)`, embedding model
  `BAAI/bge-base-en-v1.5`, reranker `BAAI/bge-reranker-base`.
- **Hardware.** `TODO(real-vault-run)` — CPU / GPU / RAM the published numbers
  were measured on.

## Results

<!-- Replace every TODO(real-vault-run) cell with a desk-side run's output. -->

- Corpus scale: `TODO(real-vault-run)` markdown files, `TODO(real-vault-run)` KB
  notes, `TODO(real-vault-run)` media artifacts (rounded down to the nearest 10).
- Golden set: 9 queries.

| Mode | NDCG@5 | NDCG@10 | MRR | recall@10 | latency median (ms) | latency p90 (ms) |
|---|---|---|---|---|---|---|
| keyword | TODO(real-vault-run) | TODO(real-vault-run) | TODO(real-vault-run) | TODO(real-vault-run) | TODO(real-vault-run) | TODO(real-vault-run) |
| hybrid | TODO(real-vault-run) | TODO(real-vault-run) | TODO(real-vault-run) | TODO(real-vault-run) | TODO(real-vault-run) | TODO(real-vault-run) |
| hybrid+rerank | TODO(real-vault-run) | TODO(real-vault-run) | TODO(real-vault-run) | TODO(real-vault-run) | TODO(real-vault-run) | TODO(real-vault-run) |

## Reproduction

The report reproduces in the same shape against any vault + golden set that
follow the harness contract. It needs live vectors (the embedding/reranker
models), so it is a desk-side command, not a CI step.

Against your own vault and golden queries:

```
EXOMEM_VAULT_PATH=/path/to/your/Obsidian \
  python scripts/eval_retrieval.py --report markdown --golden tests/golden/queries.yaml
```

As a deterministic, smoke-scale run against the bundled sample vault (no private
vault required — same report shape, tiny corpus):

```
EXOMEM_VAULT_PATH=tests/fixtures \
  python scripts/eval_retrieval.py --report markdown
```

Set `EXOMEM_BENCH_HARDWARE` to record the host in the report's methodology line.

## Limitations

- **Small.** n = 9 golden queries. Individual metric moves are noisy at this size.
- **Single vault.** Measured against one private vault; results do not
  generalize to arbitrary corpora.
- **Self-graded.** Relevance labels are chosen by the vault owner, not by
  independent annotators — this is a self-measurement, not a third-party
  benchmark.
- **Hardware-dependent latency.** The median/p90 numbers reflect one host's
  CPU/GPU. A third party's latency will differ; the published p90 is not a
  portable guarantee.
