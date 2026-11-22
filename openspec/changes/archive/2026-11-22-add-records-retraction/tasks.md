## 1. Schema 19

- [x] 1.1 Add migration 19: rename `items` to `item_rows`, add `retracted_txn`, create the current-rows view `items` and the partial index, and rebuild `audit_effects` with the widened `retract` check. Move the trigger DDL, the raw writers and the blob readers that name `items` to `item_rows`. Test a migration from 18 on a json collection and a typed collection with dynamic tables: `foreign_key_check` is clean, every row reads, history verifies.
- [x] 1.2 Move the reads that must see retracted rows to `item_rows`: history, version identity, typed version reads, history subjects and the row-history lookup. Keep the incremental import row counter correct across retraction and re-admission: it falls back to a count through the partial index when a retraction or revival follows the last log row.

## 2. Retraction

- [x] 2.1 Add `record_memory(action="retract")`, its `operation_entries` row (destructive), the writer operation, its receipt and its `retract` audit effect in the history chain. Bind row ids through the existing binder. Test: a retracted row leaves queries, filtered queries, inspect counts and id binding; history shows the versions and the retraction with its reason; a cold reopen verifies.
- [x] 2.2 Keep derived state consistent: typed current rows, query projections, rollups, reference index, query continuations and cached release state. Test on a typed collection with an index, a rollup and an inverse reference, after retraction and after re-admission.
- [x] 2.3 Re-admit a retracted row through append and through bulk upsert or import, including identical content and the row capacity check. Test both paths.
- [x] 2.4 Refusals: absent and retracted rows refuse identically; stale guards write nothing; a restricted caller gets the collection refusal; file-canonical and per-row-view collections refuse `RETRACT_UNAVAILABLE`.
- [x] 2.5 Regenerate `docs/capabilities.md` and the MCP tool schema fixture. Document retraction and schema 19 in `docs/records.md` and `docs/deployment.md`.

## 3. Closure

- [x] 3.1 Rehearse migration 19 on a byte copy of a production store. Record integrity, foreign keys, history verification and per-collection current counts before and after.
  - Evidence, 2026-11-22: the release owner upgraded a byte copy of the production store, taken as a 0.196.0 backup at schema 18, through the managed path to 0.199.0.
  - Before: `integrity_check` ok, schema 18, commit sequence 27394; the 0.196.0 backup's own validation refuses any `foreign_key_check` row, so the seed had none.
  - After: `integrity_check` ok, schema 19, the same commit sequence and head hash; `foreign_key_check` found 0 violations; current counts were recorded for the 53 collections with current rows.
  - History: all 1,067 `item_versions` rows and 7,066 `audit_effects` rows are value-identical, compared in key order, with none added. All 7,157 current rows kept their ids and content, and none is retracted. The 0.199.0 reader opened the migrated store and backed it up.
  - Limits: no genesis chain verification was recorded on either side of the rehearsal. The history evidence is that `item_versions` and `audit_effects` are unchanged, that migration 19 does not write `txns`, and that the 0.199.0 backup matched the unchanged head to the last transaction. After the live install, inspect's `audit ok` on the retracted collection comes from history verification at schema 19. The per-collection counts ran after the migration only; the before side follows from the row-identity proof. The rehearsal's 45-minute soak verdict failed on 2 of 536 health samples, with 0 failed calls; the record does not keep the samples.
- [x] 3.2 Run the Records benchmark before and after this change.
- [x] 3.3 After the release is installed, with a backup taken before the upgrade: retract the one stored row with a `null` key part, and verify from stored state that the collection holds one row fewer and that no current row has a `null` key part.
  - Evidence, 2026-11-22: 0.199.0 was installed after a schema-18 store backup. The one row with a `null` key part was retracted with a reason.
  - After: the collection's count fell from 4,062 to 4,061; a filter on the `position` key part, the only part that held `null`, matched 0 rows that are missing it or hold `null`; inspect reported 4,061 committed rows with audit ok, and the row's history shows its import write and the retraction with its reason.
  - Limits: the counts come from the service's own store reads; no separate read-only SQL count was taken.
- [x] 3.4 Archive this change with `openspec archive add-records-retraction` after 3.3. Run `openspec validate --all --strict` before and after.
