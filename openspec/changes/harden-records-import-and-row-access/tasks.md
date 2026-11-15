## 1. Benchmark (first, then before and after each delivery)

- [ ] 1.1 Add a Records import scenario to the existing latency harness (`scripts/measure-records-append-latency.py`) or a sibling that shares its isolation setup. It covers:
  - a generated source of 58 lists and about 4,800 nested rows, with a 734-row `null`-position tail;
  - a contender option that runs CPU-bound threads in the process;
  - recorded per-batch wall time, CPU time, statement count, and boundary wait and hold.
- [ ] 1.2 Record the baseline on `main`: the clean store, then 1 and 2 contenders, 3 repeats each. Keep the run files.

## 2. Validation and honest counts (delivery 1)

- [ ] 2.1 Make `required` mean non-null and keep `null` out of natural keys, in the one validation and identity function shared by append, update, bulk upsert and import. Test:
  - the 734-row `null` tail through import;
  - a `null` optional key part through bulk upsert.
- [ ] 2.2 Run full-source preview through the job's own row preparation, with the `would_*` counts, coordinates, collapse samples and `coverage`. Test the malformed tail placed after the valid rows.
- [ ] 2.3 Split `duplicates` into `collapsed_in_source` and `restated_existing`, and fold the job totals from the batch receipts. Test totals against receipts after a multi-batch job.

## 3. Row access (delivery 1)

- [ ] 3.1 Bind a row id with one key-and-version read, cached per collection generation, behind the caller's read decision.
  - Test edit by id on a store collection of 1,000 rows or more, at the production response cap.
  - Test the withheld-row twin.
- [ ] 3.2 With the owner of `add-collection-query-engine`, complete its Q1 tasks 1.7 to 1.9 and route `query_records` on store collections through `query_engine.sqlite.execute_legacy`. Test a filtered and sorted query over 5,000 rows at the production 200 ms profile (opting out of the suite-wide relaxation), plus the 154-row case beside a CPU-bound thread.
- [ ] 3.3 Count the `rendered` copy once against the response cap. Take the ownership proof once per request.
- [ ] 3.4 Add `record_memory(action="retract")` with its guards, its audit effect and its history. Add the migration that admits the `retract` effect. Test retraction, history and re-insert by the same natural key.

## 4. Import holds and status (delivery 2)

- [ ] 4.1 Replace per-row store reads in a batch with set-based reads for existing rows, twins, versions and event effects. Read per-collection layout and epoch once per batch. Show the batch's statement count bounded by tables, not rows, in the benchmark.
- [ ] 4.2 Close a batch at the hold budget as well as at 500 rows or 4 MiB. Test an unrelated interactive write during an import beside two CPU-bound threads: it commits with no `MUTATION_BUSY`, and no hold exceeds the budget.
- [ ] 4.3 Serve import status from a read snapshot off the store-thread queue, with phase, timings, wait and hold, throughput, ETA and resumable or cancellable. Test status while a batch runs.

## 5. Closure

- [ ] 5.1 Re-run the benchmark after each delivery. Record the per-case difference against the baseline from the run files.
- [ ] 5.2 After the release is installed on the personal service:
  1. Retract the one stored row with a `null` key part.
  2. Verify the collection holds 4,061 rows and no `null` key part, from stored state.
  3. Edit the dogfood album by id.
  4. Run filtered queries on the 154-row and 4,062-row collections.
  5. Update the incidents with the before and after evidence.
- [ ] 5.3 Archive this change with `openspec archive harden-records-import-and-row-access` after 5.2. Run `openspec validate --all --strict` before and after.
