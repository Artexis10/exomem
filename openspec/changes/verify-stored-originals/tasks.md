## 1. Fixity table and binding walk (D2, D3)

- [x] 1.1 Add the fixity table, keyed by vault path and payload SHA-256, in the vault's state folder. Fill it from `media_store.bound_originals`, the member manifests that `archive_members.read_manifest` reads, and the fenced text originals that `adoption_plan.read_import_index` finds. Run the binding walk at start and then at its interval. Write the pure identity and ordering logic test-first. Verify with tests:
  - a deleted table is rebuilt from the bindings, with every row `unverified`;
  - with the lexical index absent, the binding walk finds every bound original and pool blob;
  - a row retires when its binding names another hash.

## 2. Thread, pass and budget (D1, D4)

- [x] 2.1 Start the fixity thread from `server_runtime.py`, as `_start_graph_drain` is started. Verify: with `EXOMEM_DISABLE_MEDIA_EXTRACTION` set, the thread runs passes. With `EXOMEM_DISABLE_ORIGINAL_FIXITY=1`, no thread starts and status reports `off`. A failing start logs and the server still serves, through the shared `_start_component` that starts the other background parts.
- [x] 2.2 Implement the pass: the presence check, the same-pass hash of a changed identity, and the budgeted re-hash. Verify with tests:
  - a pass hashes no more bytes than its budget;
  - an evicted original causes no store request;
  - a changed modification time is hashed in the same pass.

## 3. Restore (D5, D6, D7)

- [x] 3.1 Implement the candidate copies, the re-check under the vault mutation guard, and the move of changed bytes to Trash. Verify with the highest-level check: one pass on a temporary vault with injected damage.
  - With a matching copy, the original is restored, the changed bytes are in Trash, and attention lists the finding.
  - With no matching copy, the file is unchanged, the row is `corrupt_no_copy`, and the binding keeps its hash.
  - With a deleted original, the row is `missing` and nothing is restored.
- [x] 3.2 Verify the edges with tests, each one failing first:
  - an outside rewrite gives the cause `edited`;
  - a binding rebound during a pass moves nothing;
  - changed bytes of a raw-protected original stay owner-only in Trash;
  - changed fenced text of a Source page leaves the page unchanged and records `corrupt_no_copy`.

## 4. Signals (D9)

- [x] 4.1 Add the `original_fixity` attention category and the `originals` block in `resource_status.py`. Verify: before the first pass the block reports `unknown` and no count. A stopped pass reports partial counts. A `corrupt_no_copy` row gives one default attention finding.
- [x] 4.2 Add the content-free count to `runtime_readiness.py`, and the cellctl alert beside the recall-lag alert. Verify in `infra/cellctl/tests/test_cell_health_alerts.py`:
  - a count above zero fires the alert;
  - `unknown` or an unreadable answer neither fires nor resolves it;
  - the readiness answer stays ready and holds no path.
- [x] 4.3 Add the restic restore of one original to `docs/runbooks/cloud-operator-restore.md`. Verify: the step names the restic command, the target path and the hash check against the binding.

## 5. Measurement before release (D4)

- [x] 5.1 Measure one full pass and one table rebuild on WSL and on a Cloud cell. Record the original count, the bytes read, the duration and the read rate, with the run file of each. Verify: design.md cites the numbers, and the defaults for the interval, cycle, read cap and walk interval follow from them.
- [x] 5.2 Rehearse the runbook step on a disposable rehearsal cell. Verify: the step restores one original whose bytes hash to its binding. Evidence: run 42690123576 restored a damaged original, and the next pass and readiness counted no unresolved original. The rehearsal rendered the scratch restore as rehearsal step 11 does, because the operator scratch command does not yet support a new cell; step 5 of the runbook says so.

## 6. Delivery

- [x] 6.1 Give each closed state and cause value a one-line reason in code. Verify: the C6 gate passes, and the review names C4 as absent.
- [ ] 6.2 After the release, sync the spec and archive this change with `openspec archive`. Verify: `openspec validate --all --strict` passes before and after.
