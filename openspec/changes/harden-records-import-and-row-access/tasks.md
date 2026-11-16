## 1. Benchmark (first, then before and after each delivery)

- [ ] 1.1 Add a Records import scenario to the existing latency harness (`scripts/measure-records-append-latency.py`) or a sibling that shares its isolation setup. It covers:
  - a generated source of 58 lists and about 4,800 nested rows, with a 734-row `null`-position tail;
  - a contender option that runs CPU-bound threads in the process;
  - recorded per-batch wall time, CPU time, step count (`execute` calls, `executemany` parameter sets and fetched rows), and boundary wait and hold.
- [ ] 1.2 Record the baseline on `main`: the clean store, then 1 and 2 contenders, 3 repeats each. Keep the run files.

## 2. Validation and honest counts (delivery 1)

- [x] 2.1 Make `required` mean non-null and keep `null` out of natural keys, in the one validation and identity function shared by append, update, bulk upsert and import. Test:
  - the 734-row `null` tail through import;
  - a `null` optional key part through bulk upsert.
- [x] 2.2 Run the source preview through the job's own row preparation, with the `would_*` counts, coordinates, collapse samples and `coverage`, within the preview hold budget. Test the malformed tail placed after the valid rows, and a read stopped by the budget.
- [x] 2.3 Split `duplicates` into `collapsed_in_source` and `restated_existing`. Fold the job totals from the batch receipts, and record the counts of a batch that commits no row in `uncommitted`. Test totals against receipts plus `uncommitted` after a multi-batch job.

## 3. Row access (delivery 1)

- [x] 3.1 Bind a row id with one key-and-version read, cached per collection generation, behind the caller's read decision on each row.
  - Test edit by id on a store collection of 1,000 rows or more, at the production response cap.
  - Test the withheld-row twin.
- [x] 3.2 With the owner of `add-collection-query-engine`, complete its Q1 deadline and fetch work (1.7, and 1.8 apart from its memory probe) and route `query_records` on store collections through `query_engine.sqlite.execute_legacy`, as Decision 5 states. Test a filtered and sorted query over 5,000 rows at the shipped `records` profile, plus the 154-row case beside CPU-bound threads.
  - `tests/test_record_query_selection.py`: routed answers equal the whole read's for filters, sorts, pages, date bounds, default columns, every aggregate and a continuation, on typed and JSON summary collections. A 5,000-row filtered page rebuilds its 10 rows and the 500 leading rows that name its default columns. Default columns over 400 leading rows take a few statements, where the base took one or more per row. A 154-row query answers when every clock reading on its thread advances 0.5 s without CPU, as a wait for the GIL does; it fails on the base commit. A stopped query refuses alike on both routes, converted summary rows keep their item key order, and a 5,000-row whole read outside a query answers under the shipped profile. A withheld row and a restricted caller keep the whole read, and a declined whole read stops decoding at its deadline.
  - `scripts/measure-records-query.py` on invented data, 2026-11-15, with the base commit and this change run in turn at each size, 3 runs each:
    - Without busy threads, the 5,000-row filtered page took 44 to 48 ms in 141 statements, against 158 to 225 ms in 5,086 before.
    - Beside one busy thread, every query at 4,062 and 5,000 rows answered in 0.65 to 1.03 s, where every run refused before. At 154 rows, 9 of 9 runs answered, against 8 of 9 before.
    - Beside two busy threads, every query at 154, 4,062 and 5,000 rows answered in 1.8 to 3.8 s, where every run refused before. A routed query still makes 120 to 142 statements, and its session setup and rendering run outside the deadline, so its wall time can pass the 2 s backstop.
    - At 100,000 rows without busy threads, the filtered page answered in 0.38 to 0.48 s, the date sort in 0.48 to 0.57 s and the grouped aggregate in 0.89 to 1.0 s. Before, the date sort and the aggregate refused, and the filtered page answered in 3.9 s in 2 of 3 runs. Beside one busy thread, the filtered page and the date sort answered in 1.9 to 2.2 s and the aggregate refused; beside two, every 100,000-row query refused on the backstop.
    - The base's busy-thread outcome depends on the host's load: an earlier run under heavier load answered 7 of 9 154-row runs beside one busy thread and 1 of 9 beside two.
- [x] 3.3 Count the `rendered` copy once against the response cap. Take the ownership proof once per request.
  - [x] A query page renders once and shrinks to the rows that fit the cap, then continues from the last kept row.
  - [x] A routed read already hands its routing proof to the store thread and proves nothing itself; a call outside that route proves four times, about 0.3 ms each. Decide with the 3.2 routing.
    No change: every production store request is routed. `writer_lease._collection_store_thread` calls `route_with_proof`, which passes the proof to `server.call(initial=..., read_only=True)`; `_serve` enters `ownership.read_scope(initial=...)`, and every inner proof reuses it. Only tests and in-process binders call outside that route.

## 4. Import holds and status (delivery 2)

- [x] 4.1 Replace per-row store reads in a batch with set-based reads for existing rows, twins, versions and event effects. Read per-collection layout and epoch once per batch. Show the batch's statement count bounded by tables, not rows, in the benchmark.
  - Built for a batch of new rows in a summary collection: 500 rows take 197 `execute` calls, no parameter-set steps and 664 fetched rows (861 steps), from 4,685, 3,000 and 4,660 (12,345 steps) on `main`. Fetched rows still grow by one per row, through the effect check's payload read.
  - Not built: a batch that updates or restates existing rows still writes per row, and a collection with references still maintains its reference index per row.
  - Evidence: the benchmark on the generated 58-list fixture, 3 repeats at 0, 1 and 2 contenders, from the change that delivered this task.
- [ ] 4.2 Close a batch at the hold budget as well as at 500 rows or 4 MiB. Test an unrelated interactive write during an import beside two CPU-bound threads: it commits with no `MUTATION_BUSY`, and no hold exceeds the budget.
- [ ] 4.3 Serve import status from a read snapshot off the store-thread queue, with phase, timings, wait and hold, throughput, ETA and resumable or cancellable. Test status while a batch runs.

## 5. Closure

- [ ] 5.1 Re-run the benchmark after each delivery. Record the per-case difference against the baseline from the run files.
- [ ] 5.2 After the release is installed on the personal service:
  1. Edit one row by id.
  2. Run filtered queries on the 154-row and 4,062-row collections.
  3. Add the before and after evidence to the recorded failure reports.
- [ ] 5.3 Archive this change with `openspec archive harden-records-import-and-row-access` after 5.2. Run `openspec validate --all --strict` before and after.
