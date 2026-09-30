# Collection store spike

A throwaway measurement for the OpenSpec change
`openspec/changes/move-structured-collections-to-sqlite/`. It is not product
code, imports nothing from `exomem`, and is not collected by any test tier.

`spike.py` builds the store schema proposed in that change's `design.md` in a
temporary directory, seeds a Records-shaped collection with invented rows, and
times:

- a guarded single append (generation check, row, version, provenance, audit
  effect, hash-chained transaction, all in one `BEGIN IMMEDIATE` transaction);
- an idempotent replay by request id;
- a 500-row bulk upsert in one transaction (half inserts, half updates), and
  its replay (all `unchanged`, nothing written);
- a governed query two ways: per-row authorization before reduction, and the
  uniform-release fast path where one collection-level decision covers every
  row;
- a consistent snapshot through the SQLite backup API, published by atomic
  rename, then `PRAGMA integrity_check`;
- rendering and atomically publishing one Markdown projection file.

```
uv run python benchmarks/collections_sqlite_spike/spike.py --rows 10000 \
  --out benchmarks/collections_sqlite_spike/results-10000.json
```

`results-1000.json` and `results-10000.json` are the committed runs quoted in
the design. They were taken on a 4-core Linux container with ext4 storage,
SQLite 3.45.1, WAL and `synchronous=FULL`. They measure the storage engine
only: the dispatcher, idempotency ledger, governance resolution and receipt
projection are not in these numbers. The design's end-to-end budget accounts
for them separately.
