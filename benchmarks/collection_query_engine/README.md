# Collection query engine baseline
A one-shot baseline of the dark Records store, taken before the typed query engine
changes land, so later ratios (write latency and WAL bytes with 4 declared indexes)
and failure diffs have something to compare against. It is not a gate.

`baseline.py` builds invented rows (fixed seed) in a temp store at N=1,000 and 10,000,
with no secondary index and with 4 declared indexes, and times a guarded append, a
500-row bulk upsert, a legacy sorted page-50, a legacy sum and a typed page-50
(p50/p95 over 30 samples, 5 for bulk). WAL bytes are the `-wal` file growth around one
mutation with autocheckpoint off. RSS is the process high-water mark.

    python benchmarks/collection_query_engine/baseline.py --out baseline.json

`baseline-38798c7b7.json` is the committed run; it also holds `scoped_tests`, the
pass/fail counts of the store and query suites at that revision.
