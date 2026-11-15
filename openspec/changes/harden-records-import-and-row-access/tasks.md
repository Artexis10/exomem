## 1. Benchmark (first, then before and after each delivery)

- [ ] 1.1 Add a Records import scenario to the existing latency harness (`scripts/measure-records-append-latency.py`) or a sibling that shares its isolation setup. It covers:
  - a generated source of 58 lists and about 4,800 nested rows, with a 734-row `null`-position tail;
  - a contender option that runs CPU-bound threads in the process;
  - recorded per-batch wall time, CPU time, statement count, and boundary wait and hold.
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
- [ ] 3.2 With the owner of `add-collection-query-engine`, complete its Q1 tasks 1.7 to 1.9 and route `query_records` on store collections through `query_engine.sqlite.execute_legacy`. Test a filtered and sorted query over 5,000 rows at the production 200 ms profile (opting out of the suite-wide relaxation), plus the 154-row case beside a CPU-bound thread.
- [ ] 3.3 Count the `rendered` copy once against the response cap. Take the ownership proof once per request.
  - [x] A query page renders once and shrinks to the rows that fit the cap, then continues from the last kept row.
  - [ ] A routed read already hands its routing proof to the store thread and proves nothing itself; a call outside that route proves four times, about 0.3 ms each. Decide with the 3.2 routing.

## 4. Import holds and status (delivery 2)

- [ ] 4.1 Replace per-row store reads in a batch with set-based reads for existing rows, twins, versions and event effects. Read per-collection layout and epoch once per batch. Show the batch's statement count bounded by tables, not rows, in the benchmark.
- [ ] 4.2 Close a batch at the hold budget as well as at 500 rows or 4 MiB. Test an unrelated interactive write during an import beside two CPU-bound threads: it commits with no `MUTATION_BUSY`, and no hold exceeds the budget.
- [ ] 4.3 Serve import status from a read snapshot off the store-thread queue, with phase, timings, wait and hold, throughput, ETA and resumable or cancellable. Test status while a batch runs.

## 5. Closure

- [ ] 5.1 Re-run the benchmark after each delivery. Record the per-case difference against the baseline from the run files.
- [ ] 5.2 After the release is installed on the personal service:
  1. Edit one row by id.
  2. Run filtered queries on the 154-row and 4,062-row collections.
  3. Add the before and after evidence to the recorded failure reports.
- [ ] 5.3 Archive this change with `openspec archive harden-records-import-and-row-access` after 5.2. Run `openspec validate --all --strict` before and after.
