## 1. Projection

- [ ] 1.1 Keep the binding projection for every vault with a valid allowance (`_tracks_bindings`), and run `recover` before the offload scan whenever the vault tracks bindings. Rebuild only in the serving process, never in the media worker process.
- [ ] 1.2 Add the side table keyed by companion path, with its two kinds of row: a confirmed displaced reference, which counts, and a page that names a stored version but binds no original, which makes the total unknown. Fill it in the rebuild. In each delta, replace the rows of each page that the delta writes or deletes. Write the generation that it matches with each delta and rebuild.
- [ ] 1.3 Add the version-key index and the `(sha256, size)` index, and the owner-total read: the checkpoint, the side table's generation, the recorded failure, the total over distinct versions in the selected store, the conflict flag and up to 8 reuse versions outside Trash and not displaced. Read it in one ledger transaction.
- [ ] 1.4 Record a failed rebuild in the media ledger, with its generation and up to five blockers, in place of `_failed_census`. Make the rebuild go on past a note that fails on its own and collect it, and still publish nothing.
- [ ] 1.5 In file mode, withdraw the projection at each service start, before the first offload scan, on a vault with an allowance. Withdraw it when a restore that replaces pages outside governed writes ends.
- [ ] 1.6 In store mode, publish a rebuild at the frontier of the snapshot whose pages it read, with the pages and the frontier from one snapshot.

## 2. Offload and status

- [ ] 2.1 Read the owner total from the projection outside the boundary for admission, the offload scan, the refusal check of `enqueue_offload`, the refusal status, upload admission, the import window status and `take_census`. Remove the census walk from each hold.
- [ ] 2.2 Under the protection and publication holds, check that the projection is current, and read the total again when the checkpoint moved since admission read it.
- [ ] 2.3 In store mode, run the custody follow before each hold. Under each hold, replay an audit suffix of at most 256 rows on a complete projection at the current policy, and raise `MEDIA_STORE_BUSY` for a longer suffix or a broken trail.
- [ ] 2.4 Return the job with `MEDIA_STORE_BUSY` when the projection is not current and no failure is recorded at its generation. Refuse with `MEDIA_ALLOWANCE_UNKNOWN` and the recorded blockers when one is. Report the total as being counted in status. Admit an upload when the total is not current.
- [ ] 2.5 Remove `OffloadBatch`, `ReferenceUsage.charge`, the `pointer_replacements` marker and the writes of `media_owner_census`. Keep the table for a downgrade.
- [ ] 2.6 Add one file under `user-notes/` for the code pull request: saves no longer wait while media is moved to the store.

## 3. Proof

- [ ] 3.1 A governed save commits during offload admission, publication and the scan, with no `MUTATION_BUSY`, while a timed hook parks the owner-total read, as `test_reference_restored_during_final_census_cannot_publish_over_quota` parks the census. Confirm that the test fails on the code before this change.
- [ ] 3.2 Count the pages read while `current_thread_holds_boundary()` is true during one `offload()` call, at 100 and at 1,000 bound originals. Both counts are equal and include no note read for the allowance.
- [ ] 3.3 The projection's total and reuse slice equal `media_allowance.bound_references` on one vault with: live and Trash companions, a confirmed displaced companion with another version at the same path, a shared version, another store, a `stored_object` without a binding and a conflicting version. The unbound page and the conflict each make both totals unknown.
- [ ] 3.4 On a generated ledger of 100,000 bindings, the owner-total read takes at most 50 ms at p95, the rebuild's publication hold at most 0.5 s at p99, and a replay of 256 audit rows under a hold at most 0.5 s at p99. Record the figures in the design.
- [ ] 3.5 Time the projection's calls per write on a vault with only an allowance: at most 10 ms at p95 of added work. Record the save p95 of `scripts/semantic_write_latency.py` before and after on the same machine.
- [ ] 3.6 In store mode, a save that lands between the protection hold and the publication hold is replayed under the publication hold, and the offload publishes with no `MEDIA_STORE_BUSY`.
- [ ] 3.7 In file mode, a reference added by hand while the service is stopped makes the projection not current after the next start, and the rebuilt total counts it. An outside edit while the service runs withdraws the projection before the watcher's debounce.
- [ ] 3.8 A projection that is not current returns the job with its attempt count unchanged. A failed rebuild refuses with `MEDIA_ALLOWANCE_UNKNOWN` and names its notes, also after a restart.
- [ ] 3.9 A restore of a vault whose projection was current leaves the projection not current until a rebuild publishes.
- [ ] 3.10 After a revert to the code before this change and an upgrade again, the allowance reads the projection as not current until a rebuild, in file mode and in store mode.
- [ ] 3.11 Replace the tests that pin the batch census with tests of the projection's behaviour: in `tests/test_media_store.py`, `test_an_allowance_refusal_recorded_during_the_offload_scan_takes_no_census_under_its_hold`, `test_a_batch_of_offloads_reads_the_owner_census_once`, `test_other_writes_land_between_two_members_of_a_batch`, `test_a_batch_charges_each_version_it_publishes_to_its_census`, `test_a_census_that_could_not_count_replaces_an_older_known_figure_in_the_status` and `test_a_status_with_a_demand_counts_afresh_once_the_recorded_census_is_older_than_the_demand_lease`; the census uses in `tests/test_media_storage_record.py`, `tests/test_pooled_restricted_upload.py` and `tests/test_media_replicas.py`.

## 4. Closure

- [ ] 4.1 Add a pointer to this change, with the code pull request, to `add-cloud-media-store` design D10 and tasks 4.5.1 and 5.4.9.
- [ ] 4.2 After the release is live, the mutation lock's `mutation_lock_released` events for media operations on the personal service show no hold above 0.5 s for the allowance.
- [ ] 4.3 Archive this change with `openspec archive count-media-allowance-from-bindings` after `add-cloud-media-store` is archived, and run the strict gate before and after.
