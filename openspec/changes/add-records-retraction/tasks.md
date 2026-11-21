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

- [ ] 3.1 Rehearse migration 19 on a byte copy of a production store. Record integrity, foreign keys, history verification and per-collection current counts before and after.
- [x] 3.2 Run the Records benchmark before and after this change.
- [ ] 3.3 After the release is installed, with a backup taken before the upgrade: retract the one stored row with a `null` key part, and verify from stored state that the collection holds one row fewer and that no current row has a `null` key part.
- [ ] 3.4 Archive this change with `openspec archive add-records-retraction` after 3.3. Run `openspec validate --all --strict` before and after.
