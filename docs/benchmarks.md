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
- **Golden set.** `tests/golden/queries.yaml` — **26** hand-authored
  natural-language queries, each with one or more relevant target pages
  (`expect_any_of`, or `graded` 0–3 for NDCG). Several entries each PIN one
  ranking behavior so a regression points at what it broke: compiled-over-source
  (`prefer_compiled`), supersession demotion (`prefer_active`), a graph-hop-only
  target (reachable only through a wikilink), a temporal-recency query, an
  exact/quoted lookup, and a `scope="kb"` query whose answer lives OUTSIDE
  `Knowledge Base/` (exercising the auto-widen reserve). The set is designed to
  grow: `scripts/derive_relevance_pairs.py` proposes additions mined from real
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

- Corpus scale: 30 markdown files, 20 KB notes, 0 media artifacts (rounded down
  to the nearest 10); 198 chunk vectors.
- Golden set: 26 queries.

| Mode | NDCG@5 | NDCG@10 | MRR | recall@10 | latency median (ms) | latency p90 (ms) |
|---|---|---|---|---|---|---|
| keyword | 0.3430 | 0.3430 | 0.3462 | 0.3269 | 6.4 | 7.2 |
| hybrid | 0.9142 | 0.9270 | 0.9154 | 0.9615 | 6.5 | 7.3 |
| hybrid+rerank | 0.9286 | 0.9397 | 0.9397 | 1.0000 | 6.6 | 7.6 |

The gap is the whole point: lexical-only (`keyword`) recalls the intended page
for only about a third of the queries (recall@10 0.3269, NDCG@10 0.3430), while
`hybrid` finds nearly every target in the top-10 (recall@10 0.9615) and ranks it
at or near the top (NDCG@10 0.9270, MRR 0.9154); the cross-encoder reranker then
tightens the order and lifts recall to 1.0 (NDCG@10 0.9397). `hybrid`'s recall is
below 1.0 by design: two queries deliberately grade a marginal page that
`prefer_compiled` / `prefer_active` demotes out of the top-10 (the
compiled-over-source and supersession pins), so their grade-3 ideal is found but
the marginal is not. Latency here is dominated by fixed per-call overhead — the
corpus is 198 chunk vectors — so treat the absolute ms as a floor, not a
representative production figure; the per-lane curve below shows how cost grows
with corpus size.

## Per-lane latency vs. corpus scale

The aggregate latency in the results table is measured on the ~200-vector fixture
and is dominated by fixed per-call overhead — it says nothing about how cost grows
with a real vault. That blind spot has bitten before: a 10-file fixture benchmark
once reported a whole `find()` at ~5ms and hid a ~14s graph-lane cost on a
~1700-note vault, because an aggregate over a toy corpus cannot show a single lane
blowing up. So this section measures latency PER LANE at realistic scale.

`scripts/latency_curve.py` generates a synthetic, densely-wikilinked vault (25
outbound links per note) at increasing sizes, warms every lane, then times a fixed
query set with `find(include_timings=True)` and reports median / p90 per stage. The
default run is **model-free** (the vector/CLIP/rerank lanes are switched off) so it
needs no GPU or embedding sidecar and reproduces anywhere; it reports the lanes that
both scale with the corpus and need no model — exactly where the graph regression
lived.

Model-free, measured 2026-07-03 on the reference host (AMD Ryzen 7 5800X3D / RTX
5080 / 32 GB / Windows 11), per-lane **median / p90 in ms**:

| Notes | vector | bm25 | keyword | graph | fusion | rerank | total |
|---|---|---|---|---|---|---|---|
| 100 | — | 13.4 / 14.1 | 14.2 / 15.7 | 15.6 / 16.7 | 4.5 / 5.1 | — | 53.0 / 59.0 |
| 500 | — | 63.2 / 75.8 | 73.9 / 82.7 | 63.3 / 70.8 | 19.3 / 32.3 | — | 231.5 / 289.1 |
| 1000 | — | 128.4 / 139.6 | 140.6 / 160.9 | 124.8 / 133.2 | 24.3 / 60.5 | — | 428.5 / 537.9 |
| 2000 | — | 243.2 / 270.1 | 267.7 / 303.8 | 225.9 / 238.6 | 27.5 / 122.6 | — | 805.2 / 1041.4 |
| 5000 | — | 594.5 / 625.8 | 642.2 / 777.9 | 559.5 / 598.8 | 31.8 / 311.9 | — | 1840.1 / 2635.2 |

Reading it:

- Every model-free lane scales roughly linearly with corpus size on this dense
  corpus. `graph`, `bm25`, and `keyword` are all O(N) full-corpus lanes; `fusion`
  stays cheap. A dash means the lane was switched off for the model-free run.
- The **graph** lane is the one that regressed historically. Even warm — with the
  wikilink resolver NOT rebuilt — it costs ~226ms at 2000 notes here, because the
  synthetic corpus is deliberately dense (25 links/note), heavier than a typical
  vault. The regression this guards against — the resolver reverting to a per-query
  full rebuild (read + YAML-parse every note) — pushes the graph lane to ~1.9s at
  2000 notes and ~4.7s at 5000: the class of ~14s event a fixture-scale benchmark
  hid.
- The synthetic dense corpus is a **stress shape, not a representative vault**: a
  real vault has fewer links per note and lighter graph cost. These numbers show
  scaling and relative lane cost, not an absolute production latency.

With a real embedding sidecar and the reranker on (`--embeddings --rerank`), the
`vector` and `rerank` lanes appear too — the per-call query embed and the
cross-encoder over the fused candidates. Measured at smaller sizes (sidecar build
dominates wall-time at large N):

| Notes | vector | bm25 | keyword | graph | fusion | rerank | total |
|---|---|---|---|---|---|---|---|
| 100 | 14.0 / 19.2 | 15.2 / 17.5 | 15.4 / 18.4 | 20.2 / 25.0 | 4.4 / 4.9 | 194.2 / 219.1 | 289.3 / 311.1 |
| 500 | 13.4 / 16.4 | 66.5 / 70.3 | 72.2 / 82.4 | 70.7 / 72.7 | 24.7 / 29.0 | 146.8 / 152.3 | 419.4 / 463.5 |
| 1000 | 14.0 / 18.7 | 137.6 / 176.0 | 145.5 / 185.6 | 135.8 / 145.7 | 39.2 / 60.7 | 147.8 / 157.2 | 660.8 / 766.9 |
| 2000 | 13.6 / 17.1 | 250.8 / 260.3 | 279.3 / 352.6 | 244.3 / 285.8 | 50.8 / 129.9 | 146.0 / 149.9 | 1030.5 / 1291.1 |

The vector query-embed and the reranker's per-candidate cost are roughly
corpus-size-independent (they scale with the query and the fixed candidate count,
not N), so they add a near-constant offset on top of the O(N) lexical/graph lanes.

### Regression gate

`tests/test_latency_gate.py` turns this curve into a CI gate: it generates the same
dense 2000-note vault, warms the lanes, and asserts NO lane exceeds a ceiling
(`graph < 1000ms`, `total < 5000ms`). The ceilings sit in the wide gap between the
warm baseline (graph ~226ms, total ~805ms) and a resolver-rebuild regression (graph
~1.9s), so a 14s-style regression fails CI while ordinary CI-speed variance does
not. It is model-free, so it runs in the lean matrix on every PR and is pinned in
the `retrieval-eval` job next to the golden quality gate.

Reproduce the curve:

```
uv run python scripts/latency_curve.py --sizes 100,500,1000,2000,5000
uv run --extra embeddings python scripts/latency_curve.py --embeddings --rerank --sizes 100,500,1000,2000
```

## Vector backend at scale (10k–50k notes, sqlite-vec vs in-memory scan)

The `add-sqlite-vec-backend` change moved the vector lane's default onto vec0
virtual tables inside the embedding sidecar (`EXOMEM_VEC_BACKEND=auto`), with the
in-memory numpy scan as kill switch and fallback. That swap was gated on measured
evidence, per the find-recall-efficiency spec. This section is that evidence and
the decision record.

Method: same dense synthetic generator as the curve above, with a REAL bge
sidecar per size (`--embeddings`), measured once per backend over the same built
corpus via `--vec-backend numpy,sqlite-vec,binary` and reused across runs via
`--corpus-cache` (embed once, measure many). `binary` is `sqlite-vec` with
`EXOMEM_VEC_QUANT=binary`. Measured 2026-07-03 on the reference host (AMD Ryzen 7
5800X3D / RTX 5080 / 32 GB / Windows 11); 10k tier `--repeat 3`, 50k tier
`--repeat 2`. Vector lane only (ms, median / p90); `overlap@10` is the mean
top-10 overlap of end-to-end `find()` results against the numpy pass; RSS is the
process resident set after the pass (psutil; not captured for the 10k run).

| Notes | chunk vectors | vector numpy | vector vec0-f32 | overlap@10 f32 | vector vec0-binary | overlap@10 binary | RSS numpy / f32 / binary (MB) |
|---|---|---|---|---|---|---|---|
| 10000 | 40,000 | 17.5 / 21.8 | 130.5 / 137.2 | 1.00 | 59.7 / 74.3 | 0.86 | — |
| 50000 | 200,000 | 31.2 / 36.8 | 544.5 / 550.0 | 1.00 | 253.3 / 357.6 | 0.68 | 3491 / 1707 / 1234 |

Reading it honestly:

- **The warm lane race goes to numpy, 7–17×.** BLAS over a RAM-resident
  contiguous matrix runs at memory bandwidth; the vec0 f32 scan re-reads every
  vector byte per query through SQLite's page layer (~614 MB at 200k chunks →
  ~545 ms). Both are brute force — neither is sub-linear — so the gap grows
  with N.
- **What numpy pays for that speed is the point of the swap:** at 200k chunks
  the numpy pass holds ~1.8 GB more resident (the float32 matrix plus every
  chunk's text in the metadata cache), and any out-of-process sidecar change
  costs it a full O(N) reload. The vec0 backend holds essentially nothing
  between queries.
- **vec0-f32 is exact.** overlap@10 = 1.00 at both tiers, end-to-end through
  `find()` — matching the unit-level parity tests. The default-backend swap
  changes no ranking.
- **Binary quantization diverges at scale.** overlap@10 fell from 0.86 (10k) to
  0.68 (50k) on this corpus. It stays strictly opt-in
  (`EXOMEM_VEC_QUANT=binary`, default off); the golden retrieval floors are its
  promotion gate (it clears them on the fixture corpus — both parametrized
  passes of `tests/test_retrieval_golden.py`).
- **The loudest finding is about a different lane entirely:** at 50k notes the
  whole `find()` costs ~25 s under EVERY backend, because bm25 / keyword / graph
  are each ~8 s of O(N) work. The vector lane is under 2.2% of the total in its
  slowest configuration. "Sub-second search at 100k notes" is gated on the
  lexical/graph lanes, not on vector search — that is the next scaling build.

**Decision record.** `auto` defaults to vec0-f32: the lane-latency delta is
invisible end-to-end today, while the residency and reload wins are large and
grow with corpus size. `EXOMEM_VEC_BACKEND=numpy` restores the previous behavior
wholesale. If a future build makes the vector lane the visible bottleneck (e.g.
after the lexical lanes are fixed), the recorded next step is a real ANN index —
hnswlib, or sqlite-vec's ANN once it leaves alpha — behind the same `vecstore`
seam, recall-gated by the same golden floors.

The 100k-note tier (400k chunk vectors) is generated, embedded, and cached; its
measurement pass is pending. Complete it desk-side with:

```
uv run python scripts/latency_curve.py --embeddings --sizes 100000 --max-embeddings-size 100000 --repeat 1 --vec-backend numpy,sqlite-vec,binary --corpus-cache <cache-dir>
```

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

- **Small.** n = 26 golden queries. Individual metric moves are noisy at this size.
- **Tiny corpus.** The quality numbers are measured on the bundled fixture
  vault — ~20 KB notes, 198 chunk vectors. They show the ranking *works* (hybrid
  ≫ keyword, nearly every target recalled), not production-scale quality, and do
  not generalize to larger or different corpora. Point the harness at your own
  vault for a representative read. The per-lane latency curve above uses a
  *separate*, larger synthetic corpus precisely because this fixture is too small
  to expose latency scaling.
- **Self-graded.** Relevance labels are hand-authored (by the maintainer, not
  independent annotators) — a self-measurement, not a third-party benchmark.
- **Hardware-dependent latency.** The median/p90 numbers reflect one host's
  CPU/GPU. A third party's latency will differ; the published p90 is not a
  portable guarantee.
