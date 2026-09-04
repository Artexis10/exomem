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
