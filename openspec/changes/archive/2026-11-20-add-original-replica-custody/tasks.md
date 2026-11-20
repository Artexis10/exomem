# Tasks

Each box carries the proof that closes it. Tests live in `tests/test_media_replicas.py` unless named otherwise.

## 1. Configuration and adapter

- [x] 1.1 Extend the media store configuration parser with a list of stores and the three-field descriptor. Prove that an incomplete entry makes only that store unavailable, and that a Cloud entry without a list reads as one `immediate` store with unknown fields.
  Proof: `test_an_incomplete_descriptor_makes_only_that_store_unavailable_and_the_status_names_the_field`, `test_a_vault_that_declares_no_store_list_reads_as_one_immediate_store`.
- [x] 1.2 Promote the filesystem adapter to a supported `filesystem` adapter that never creates its root and requires a matching store-identity marker. Prove that an unmounted NAS mount point refuses uploads and `head` and leaves replica states unchanged.
  Proof: `test_a_root_that_does_not_name_the_store_refuses_the_copy_and_leaves_the_replicas_alone`, `test_a_store_names_itself_only_by_the_owner_s_declaration_and_never_on_the_vault_s_own_device`, `test_declaring_a_store_on_another_device_writes_its_marker_once`, `test_a_root_with_another_stores_marker_id_is_not_served_once_the_owner_recorded_the_id`, `test_a_malformed_connection_field_makes_only_that_store_unavailable`, `test_a_declared_media_edge_store_has_no_connection_of_its_own`.

## 2. Replica list and recoverability

- [x] 2.1 Add the replica list, with key version, to the original binding, and read a legacy single pointer as a list of one. Prove that a binding and a configuration written by this change read correctly on the previous release, and that a second-store replica never changes the original's state.
  Proof: `test_a_binding_with_replicas_still_reads_as_the_pointer_alone_for_a_release_without_the_key`, `test_a_later_edit_of_the_binding_keeps_the_replicas_it_does_not_change`, `test_a_replica_of_an_evicted_original_is_the_stored_ciphertext_and_needs_no_download`. A one-off run parsed a binding that this change wrote with the `originals.py` of `origin/main`: it read the same state, the same pointer store and the same pointer object, and ignored `replicas`.
- [x] 2.2 Record the owner's key-export confirmation per key version. Prove that the status shows an encrypted copy as not recoverable until the owner confirms it.
  Proof: `test_the_owner_s_key_export_is_what_makes_a_replica_recoverable`.

## 3. Copy operation

- [x] 3.1 Add the owner `replicate` operation, with its own queue and worker beside the offload queue, and the shared upload path. Prove bounded batches, reuse of the recorded name after a lost reply, and no foreground busy refusal from the copy on a 10,000-original vault.
  Proof: `test_one_drain_settles_a_bounded_batch_and_an_unavailable_store_pauses_its_queue`, `test_a_retry_after_a_lost_upload_reply_reuses_the_replica_name`, `test_a_busy_vault_boundary_ends_the_round_and_spends_no_attempt`, `test_a_store_that_does_not_answer_is_checked_before_any_work_and_spends_no_attempt`, `test_a_repeated_replicate_keeps_the_progress_of_a_running_request_and_starts_a_new_one_when_it_ends`, `test_the_drain_picks_its_next_original_from_an_index_with_no_sort` (on 500,000 queue rows the pick took 27.7 ms with a temporary sort before the index and 0.004 ms after it), `test_replicate_is_the_owner_s_and_reports_progress_while_status_shows_the_replicas`. On a generated 10,000-original vault, a foreground note write ran every 200 ms while 30 originals replicated: 0 of 32 writes were refused, and their p50 was 1.15 s against 0.75 to 0.86 s without the copy. One sidecar publication holds the vault boundary for a p50 of 0.32 s at 100 originals, 0.4 s at 2,000 and 0.9 s at 10,000, where an offload on the same instrument reads 1.98 s at 2,000. The growth comes from the shared sidecar commit, not from the copy.

## 4. Repair and status

- [x] 4.1 Make fixity try local copies first, then verified `immediate` replicas whose key the install can read, naming each tried replica in the route. Prove a restore from a second replica when the first is damaged.
  Proof: `test_a_second_store_replica_restores_an_original_that_the_first_store_lost`, `test_a_replica_that_the_binding_no_longer_lists_is_not_read_by_repair`, `test_a_replica_entry_that_cannot_name_an_object_is_a_failed_candidate_and_not_an_aborted_pass`, `test_a_restricted_caller_is_not_told_which_store_holds_a_replica_or_under_which_key`.
- [x] 4.2 Add the owner status aggregate and the paged per-original view. Prove one bounded query at 500,000 originals, and unknown values until the background rebuild finishes after the table is lost.
  Proof: `test_the_status_counts_protection_from_the_table_and_reads_unknown_when_the_table_is_gone`, `test_the_status_reads_one_index_and_the_page_returns_originals_by_path`, `test_failure_domains_read_unknown_when_a_store_that_holds_replicas_declares_none`, `test_the_status_keeps_a_replica_known_not_to_survive_apart_from_one_whose_custody_is_unstated`, `test_the_status_reports_the_workers_observation_of_a_store_and_never_reads_the_store`, `test_a_publication_made_during_a_rebuild_survives_it_whatever_the_clock_says`. On 1,000,000 replica rows (500,000 originals, two stores) the aggregate is one covering-index scan, with a median of 97 ms over 7 runs, and a 100-original page takes 3.5 ms. After the table is dropped the status reads `unknown` in 53 ms.

## 5. Acceptance

- [x] 5.1 On a disposable vault with two `filesystem` stores on separate paths, copy the originals, damage each layer in turn, and show restore or honest `corrupt_no_copy` with the run log recorded.
  Proof: `test_a_second_store_replica_restores_an_original_that_the_first_store_lost` runs real `age` on two `filesystem` stores, damages the local copy and the first store, and restores from the second store with the tried copies named in the route. `test_an_original_whose_every_copy_is_damaged_stays_corrupt_and_is_not_overwritten` damages the second store too and reads `corrupt_no_copy` with both copies named as failed. The run log is the pytest output recorded in the delivery report.
