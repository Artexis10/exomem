# Task 0.1 — Recall Baseline On The Live Cell Before The Change

Content-free by construction: no query text, no page paths, no vault content.
Query *shapes* are named; the strings themselves are not recorded.

## What this artifact is

The "before" half of Task 5.6's before/after comparison. It is measured on the
served cell running **0.69.0**, which is the last release without any of this
change's work. Once the cell is upgraded past that release the before-series
cannot be retaken, so this file is written now and the upgrade is ordered after
it.

**It is partial, and the partiality is the point of this section.** Task 0.1
specifies thirty samples per series at a one-minute load average at or below
2.0. What is recorded below is single-sample-per-shape probing taken while the
box sat between load 3 and 12 with other work running. It establishes the
*shape* of the cost and the size of the walk stages; it does not establish
p50/p95 to the precision the contract's ceilings are stated at.

The full-form series is taken with `scripts/recall_latency_gate.py` in
report-only mode (no `--check`) against the still-0.69.0 cell on a quiescent
box, and appended here under "Full series" before the cell is upgraded.

## Method

Direct reads through the served REST facade's `ask_memory` route with
`include_timings=true`, taken from the operator's own cell. Stage timings are
read from the response's timing diagnostics, not from the query log — the live
`queries.jsonl` rows carry no stage timings on this release.

## Measured, 2026-09-03, cell at 0.69.0

Whole-request medians by shape, warm unless stated:

| shape | scope | cache state | total |
|---|---|---|---|
| hybrid | kb | cold | 1037 ms |
| hybrid | kb | warm | 561 ms |
| hybrid | vault | warm | 881 ms |

Warm stage costs inside the hybrid shape:

| stage | cost |
|---|---|
| fusion + rerank | 226 ms |
| CLIP lane | 37 ms |

Rerank cost is a function of candidate count, not of corpus size:
15 candidates ≈ 110 ms, 30 candidates = 217 ms.

## The two walk stages this change removes

`filter_eligibility` and `outside_kb` are the stages Task 0.1 requires be
present in the filtered series. They are present, and their cost is not a fixed
overhead — it is a function of how much of the read-side cache survived the last
write:

| date | cell state | `filter_eligibility` | `outside_kb` | request total |
|---|---|---|---|---|
| 2026-08-31 | caches warm and current | ~40 ms combined | — | p50 1.4 s |
| 2026-09-01 | partially degraded | — | — | p50 719 ms |
| 2026-09-03 | post-restart, caches lost | 18.1 s / 7.9 s | 7.6 s / 8.3 s | 29.5 s / 17.7 s |

The 2026-09-03 row is two consecutive reads on a cell whose catalogue and corpus
caches had been discarded, so each query paid a full tree walk. Retrieval
*compute* in those same two reads was about 2 s each; the rest is the walk.

This is the honest statement of the problem: the walk stages are cheap on a warm
cell and unbounded on a cold one, and a governed write is what moves the cell
from the first state to the second.

## What the baseline already says about the 300 ms ceiling

Removing the walks is necessary and not sufficient. On a warm cell the hybrid
shape already spends 226 ms in fusion and rerank and 37 ms in the CLIP lane
before any walk is counted, against a 300 ms p50 ceiling. Two successor
candidates are named by this measurement and are deliberately **not** in this
change's scope:

* skip the CLIP lane for queries with no visual intent — it runs on every text
  recall, 37 ms warm and about 140 ms cold;
* rerank cost scales with candidate count, so the candidate budget is the knob.

## Full series

Not yet taken. Blocked on a quiescent box (one-minute load average at or below
2.0); the gate refuses rather than reporting above it, which is the intended
behaviour. **This must be taken before the live cell is upgraded to a release
carrying this change.**

## Subsecond reproduction, 2026-10-09 (synthetic 6,500 pages, two CPUs)

This section is the "before" for tasks section 6. It is content-free: no query
text and no page paths. It was measured on a shared 20-CPU laptop with the
one-minute load average between 14 and 26, so it is **not** a quiescent
measurement. Read every number as an upper bound for a quiet cell. The
owner-path runs patched the authority check, and the driver altered the query
text after the first pass; the setup below says how. The gate from task 6.1
re-measures on the same instrument before and after each slice, with neither
of those changes.

### Setup

- Corpus: 6,500 generated pages from the repository's public prose (OpenSpec
  specs and archived changes, docs), 2.4-2.7 KB per page on average, 5 to 25
  wikilinks per page, typed frontmatter with tags, projects and dates. The
  second corpus adds semantic units to 60% of pages (3,871 pages, 9,695 units).
  Chunks: 41,878 and 45,825.
- Encoder: the served bge-m3 ONNX int8 artifact, loaded in process. Corpus
  vectors are seeded unit vectors written through the product's own index build
  and stamped with the encoder's identity; every query is encoded by the real
  encoder.
- Principal: **patched owner.** The driver replaced
  `raw_protection.has_unrestricted_access` with a function that returns true,
  so the in-process principal got no RAW admission predicate. The product's
  owner-authority path was not used. R1, R2, R3 and M1 are patched owner-path
  numbers.
- Request: in-process `ask_memory`, `mode="hybrid"`, `limit=15`, `scope="kb"`,
  `detail="compact"`, `rerank=false`, `include_timings=true`.
- Switched off: CLIP (`EXOMEM_DISABLE_CLIP=1`), the reranker
  (`EXOMEM_DISABLE_RANKING=1`, hard-off) and startup warm-up
  (`EXOMEM_DISABLE_WARMUP=1`). No CLIP stage was measured.
- Warm state: managed readiness with the catalogue proof, `warm_all`, the
  reference sidecar built, recall freshness live in both scopes, then 12 warm-up
  requests.
- Series: 38 varied queries of 1 to 9 words. The process is pinned to two
  CPUs.
- **Nonce token.** To defeat the result cache, the driver appended a
  two-character token (`a1` on the second page-level pass, `m0` and `m1` on the
  `mixed` passes). It changes what the lexical lanes match: BM25 adds it to its
  OR-match, and the keyword lane, where it is under the three-character trigram
  floor, verifies it with `instr` on every trigram-matched row. Half of the R3
  page-level samples (38 of 76) and every `mixed` sample carry it, so their
  lexical results are not comparable with the first pass.
- Measured: the elapsed time of the `ask_memory` call, and the server's
  `total_ms`. Stage figures are per-request p50 and p95 (nearest rank) over the
  requests that ran the stage. Not-run stages are listed as not run.

### Run files

The run bundle under `/tmp/exomem-subsecond-runs` is not retained: per-sample
files, profiles, the generated corpus and the driver scripts are gone. Its
content-free summaries are committed under
`verification/subsecond-2026-10-09/`. They hold no query text, no page paths
and no vault text:

- `run-summaries.jsonl`: one line per run and level, with whole-request and
  per-span percentiles, probe counts, load, setup counters and the per-request
  CPU-over-wall median. Its `header.admission` field names the principal as
  the table below does: `patched owner` for R1, R2, R3 and M1. The driver
  wrote `owner`; the label was corrected after the run;
- `stage-tables.md`: the per-request stage aggregates that the tables below
  quote;
- `sqlcheck.txt`: the query plans and isolated replays in the last table.

### Whole request

| Run | Corpus | Encoder | Principal | Level | n | Load | Elapsed p50 / p95 ms | `total_ms` p50 / p95 |
|---|---|---|---|---|---|---|---|---|
| R1 | no units | served | patched owner | page | 38 | 16.8-19.5 | 700 / 866 | 651 / 846 |
| R2 | units | served | patched owner | page | 38 | 16.7-17.5 | 619 / 1,799 | 549 / 1,631 |
| R2 | units | served | patched owner | `mixed` | 38 | 16.2-18.3 | 1,116 / 2,971 | 1,094 / 2,952 |
| R3 | units | served | patched owner | page | 76 | 16.0-17.0 | 363 / 636 | 338 / 578 |
| R3 | units | served | patched owner | `mixed` | 76 | 14.0-16.0 | 871 / 2,460 | 854 / 2,419 |
| M1 | no units | none (model-free) | patched owner | page | 38 | 24.9-25.6 | 429 / 753 | 398 / 731 |
| A1 | no units | none (model-free) | in-process principal, RAW admission on | page | 38 | 18.8-28.3 | 6,837 / 9,992 | 6,809 / 9,970 |

The R2 `mixed` row comes from the R2 `mixed` line of `run-summaries.jsonl`. A1 is
the non-owner path that `fix/admission-candidate-sizing` owns. It is here only
to show the size of that path, not as this change's baseline, and its principal
was the in-process default, not an admitted identity.

Process CPU over wall time, p50 per request: 1.89 in R3 page level and 1.53 in
R3 `mixed`, with the encoder loaded, and 0.42 in M1
(`cpu_over_wall_per_request_p50` in `run-summaries.jsonl`, computed from each
sample's `cpu_ms` and `wall_ms`).

### Stages, R3 page level (the design's reference profile)

| Stage | p50 / p95 ms |
|---|---|
| Request setup (projection, pending visibility, freshness) | 9.4 / 28.1 |
| Query encode | 32.7 / 116.2 |
| Dense search (chunk text hydration 14.7 / 35.2 inside it) | 33.1 / 52.2 |
| BM25 | 29.2 / 55.8 |
| Keyword | 39.4 / 90.6 |
| Parent hints (unspanned, inside `semantic.search`) | 84.7 / 132.3 |
| Graph | 26.3 / 135.4 |
| – `graph.seeds` / `graph.expand` / `graph.resolver` | 6.1 / 29.8, 13.9 / 31.4, 5.1 / 16.3 |
| Temporal (10 of 76 requests) | 43.6 / 124.4 |
| Fusion and multipliers | 24.4 / 65.6 |
| CLIP | not run (`EXOMEM_DISABLE_CLIP=1`) |
| Admission | not run (patched owner) |
| Hit construction, serialization, release gate, due-state block | 29.8 / 78.9 |
| Due-state block alone (outside `total_ms`) | 20.7 / 58.1 |
| `semantic.search` time no stage covers, after parent hints | 5.0 / 9.6 |

Per request at p50 and p95: 9 lexical catalogue connections and 6 readiness
proofs (27.2 / 46.0 ms in proofs), 92 / 150 page hydrations and 126 / 187 page
file reads. In the `mixed` series the unit lanes took 647 / 2,143 ms and page
file reads rose to 442 / 497. That figure includes the query encode: in
`mixed`, the encode runs inside `semantic_units` with no span of its own, and
`vector.embed` reports 0.0 ms at p50 and p95.

### Isolated replays on a copy of the R2 catalogue (`sqlcheck.txt`)

| Query | Shipped form | Alternative |
|---|---|---|
| Parent hints, 300 candidates | 135.2 ms; the plan walks `pages_kb` and scans `json_each` once per KB page | 0.20 ms, `json_each` drives a primary-key lookup |
| Parent hints, 1,000 candidates | 488.1 ms | 0.99 ms |
| Keyword SQL, new connection per query | 3.3 / 8.3 ms p50 / p95 | 2.6 / 6.5 ms on one retained connection |
| BM25 SQL, new connection per query | 6.3 / 10.8 ms | 3.7 / 7.6 ms on one retained connection |

Inside the runs the keyword SQL took 11 to 74 ms per request (profiles), against
3 ms in this replay. An earlier replay on a copy written minutes before, on the
other corpus, measured 73.6 / 479.4 ms with a new connection per query and
1.9 / 4.9 ms on one retained connection. The cause of that spread is not
isolated. Page-cache residency under the laptop's memory pressure is the likely
one, and it is unverified. A retained connection removes the per-request
connection setup in every case.
