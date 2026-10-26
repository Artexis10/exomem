Packets P2, P3, P6 and P7 of the paired Substrate change `admit-cloud-imports-against-published-limits`, which holds P1, P4 and P5. Tasks marked **Control-plane review** change cellctl or the controller contract, so the Cloud control-plane owner reviews them.

## 1. cellctl publishes limits and waits for growth (P2) — Control-plane review

- [ ] 1.1 Move `max_cell_gib` to the top level of `StorageConfig`, add the chart value, accept the earlier `local` key, and refuse to start when both differ. Let `plan_growth` read the one cap. Verify: configuration tests for a Hetzner-only fleet, the earlier key alone, and two keys that differ; the existing growth tests still pass.
- [ ] 1.2 Add `ImportLimits` beside `PART_BYTES` and `import_growth_deadline` to `ReconcileConfig`, with `CELLCTL_IMPORT_GROWTH_DEADLINE_MINUTES`. Write `import_limits` only on change, with no generation bump, and compute `max_storage_gib` for both storage classes. Verify: tests for each key's source, a cell above the cap, a local node with no room, and no write when nothing changed.
- [ ] 1.3 Read `required_storage_gib` into the import row. Wait for growth before any hold or prepare Job, tolerate a pod restart, fail at the deadline with `IMPORT_STORAGE_UNAVAILABLE`, and check the local node again before the key with `IMPORT_STORAGE_NO_ROOM`. Verify: `test_imports.py` cases for the wait with no hold, a restart that does not fail, a deadline before `key_ready`, the local no-room failure, and no recipient before capacity.
- [ ] 1.4 Write `staged_files` and `staged_bytes` from the Job's report in the update that records `done`. Verify: a test that the update carries both, and that a failed import writes neither.
- [ ] 1.5 Correct the stale comment that says a `storage_gib` raise bumps no generation. Verify: the comment matches the 0071 trigger and the render digest code.
- [ ] 1.6 Move `substrate-contract.json` to the Substrate ref that holds migration 0079, once P1 is merged. Verify: the deployed-cellctl tests pass against that ref.
- [ ] 1.7 Before each larger local claim request, check node room and retain the last accepted claim request when growth is unsafe. Keep desired `storage_gib` raised. Verify: stale published room, cleanup, later reconciliation and unknown room never request unsafe growth or a smaller claim. Complete this before local import growth ships at local cutover.

## 2. The import Job reads its limits (P3)

- [ ] 2.1 Render `EXOMEM_IMPORT_MAX_MEMBERS` and `EXOMEM_IMPORT_RESERVE_BYTES` into the import Job, read them in `cloud_import_job.py`, and publish the same two values. Verify: a Job test where an environment limit refuses an archive one member over with `ARCHIVE_TOO_LARGE`, a manifest test for the two variables, and a test that absent variables keep the defaults.

## 3. The CLI reads the limits (P6)

- [ ] 3.1 Read `limits` before creation with the fallback for an older server, the unpublished message for `null`, and the three refusals mapped as the spec states. Verify: CLI tests against a synthetic API for a limits object, no `limits` key, `limits: null` and each 422 code.
- [ ] 3.2 End `PARTS_INVALID` and `CIPHERTEXT_SHORT` as abandoned with the cancel wording, and add the staged counts to the final JSON. Verify: CLI tests for both failures and for a final report with and without counts.

## 4. Adopted count support (P7)

- [ ] 4.1 Add `source_root` to compact rows and a scoped `adopted_files` aggregate using distinct original paths across successful outcomes. Verify: repeated runs, overlapping subtrees and incomplete run state produce the specified count or unknown result.
- [ ] 4.2 The Home side is task 5.1 of the Substrate change. Verify: Home shows the adopted count against a cell that runs this version.

## 5. Delivery

- [ ] 5.1 Run the cloud rehearsal on a real Hetzner volume and on a local volume with an import that needs growth. Record the claim's capacity, its conditions, any pod restart and the import's peak volume use against `space_factor × declared bytes`. Verify: the record shows the import reaching `key_ready` and `done`, with its staged counts. This follows P1 in production.
- [ ] 5.2 Deliver P2, P3 and P6 as one pull request, and P7 later. Verify: independent review with the C4 and C6 verdicts, the privacy gate and the required checks pass.
- [ ] 5.3 Roll out in this order, outside the 02:00–05:00 UTC window: 1. Substrate migration 0079 and grants; 2. this controller and image; 3. Substrate admission and Home; 4. the CLI. Undo: leave the columns; return to the previous controller and set `import_limits` to NULL; roll the Substrate deployment back, because grown volumes stay grown; pin the earlier CLI.
- [ ] 5.4 Archive `add-exomem-cloud-vault-import` first. Then synchronize this change into the canonical specs and archive it with `openspec archive`. Verify: `openspec validate --all --strict` passes before and after the archive.
