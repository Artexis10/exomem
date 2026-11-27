# Subject records latency

This benchmark measures the work that the `subject_records` block adds to one `recall_context` request, for one (anchor, collection) pair. It is not a gate, and the pull-request tier does not run it.

`latency.py` builds a temporary vault and store with invented data:

- two person pages;
- one Records ledger that declares `subject`, with `size` rows citing the first person and one row in ten citing the second;
- amounts in cents, so every sum is rounded to the decimal places of its values;
- one row in a hundred with no amount, and another with no date.

It times `subject_records.summaries` 30 times in two cases:

- `served`: the turn touches the ledger's claims, so the entry is built.
- `gated`: the turn does not touch them. Every request that names a cited person without asking about the ledger pays this cost.

It runs on typed rows (a summary collection) and on JSON rows (a Markdown-items collection).

    python benchmarks/subject_records/latency.py --out results.json --sizes 5000,100000 --layouts typed,json

The target is the Records load target for a filtered query, applied per pair: p95 at most 300 ms at 5,000 rows and at most 1 s at 100,000.

`results.json` is the recorded run at commit `53b9cb42d` (Python 3.14.6, SQLite 3.53.1, 20 CPUs, 30 samples per case). Statements count every statement of the last `summaries` call in a case: the check of which collections declare `subject`, the admission, the visibility probe, and the two statements over the cited rows.

| Rows citing the anchor | Rows | `served` p95 | `gated` p95 | Statements per call, `served` / `gated` | Statements over the cited rows |
|---|---|---|---|---|---|
| 5,000 | typed | 24.72 ms | 2.36 ms | 39 / 14 | 2 |
| 5,000 | JSON | 19.37 ms | 2.09 ms | 33 / 11 | 2 |
| 100,000 | typed | 317.89 ms | 2.34 ms | 39 / 14 | 2 |
| 90,000 | JSON | 305.62 ms | 2.29 ms | 33 / 11 | 2 |

A Markdown-items collection holds at most 100,000 rows, so the JSON run at the larger size has 90,000 rows citing the anchor. Before this change no block existed, so the "before" cost of this work is zero. In every run, the sums and unknown counts match the generator's own arithmetic, done in exact decimals.

Run it with nothing else loading the machine. One earlier run beside other heavy jobs, on a host short of memory, passed the read session's 2 s wall-clock bound in its 90,000-row JSON case. Such a case now reads as an `unavailable` block, and its `states` show `block:unavailable`.
