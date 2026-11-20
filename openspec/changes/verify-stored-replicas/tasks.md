# Tasks

Each box carries the proof that closes it. Tests live in `tests/test_media_replicas.py` unless named otherwise.

## 1. Check state and scheduling

- [x] 1.1 Add the check columns, the `quarantined` state and the `(store, checked_at)` index to `media_replicas`, with the heal path for an existing table, and the monthly sample ledger. Make the rebuild keep check results for an unchanged object and version. Prove that a rebuild keeps a quarantine and that a new version starts unchecked.
  Proof: `test_a_rebuild_keeps_the_quarantine_of_a_replica_whose_entry_is_unchanged`, `test_a_new_version_of_a_replica_starts_unchecked_and_unquarantined`, `test_quarantining_an_entry_that_the_table_already_replaced_leaves_the_newer_replica_alone`, `test_a_table_of_an_earlier_release_gains_the_check_columns_and_keeps_its_rows`; each was seen red against its reverted change.
- [x] 1.2 Add the check round to the replica worker: the descriptor picks content, presence or no check; picks are oldest first through the index; rounds are bounded in time and bytes under the read-rate cap. Prove the ladder per descriptor value, that an unavailable store changes no row, and that one pick on 1,000,000 generated rows reads the index with no sort (`EXPLAIN QUERY PLAN` and a timing).
  Proof: `test_a_check_proves_what_the_descriptor_says_the_store_can_prove` (content and presence), `test_a_store_with_no_declared_descriptor_is_never_checked`, `test_a_store_that_does_not_answer_as_itself_changes_no_replica_when_its_check_is_due`, `test_a_check_round_takes_the_replicas_checked_longest_ago_first_and_stops_at_its_share_of_the_cycle`, `test_a_check_pick_reads_the_due_index_in_order_with_no_sort`; each seen red. On 1,000,000 generated rows (500,000 originals in two stores) the pick plan is `SEARCH media_replicas USING INDEX media_replicas_due (store=? AND checked_at<?)` with no temporary b-tree, and a 64-row pick takes 0.04 ms (best of 20, median 0.043 ms).

## 2. Sampled check

- [x] 2.1 Add `sample_bytes_per_month` to the descriptor parser (optional, default 1 GiB, zero is off) and the sampled decrypt check through the existing replica read into a hashing sink. Prove the budget stops samples for the month, a replica the install cannot decrypt is never sampled and gives its reason, and no plaintext file is written.
  Proof: `test_a_sample_that_decrypts_to_the_binding_proves_the_key`, `test_a_sample_writes_no_plaintext_to_disk`, `test_a_store_that_spent_its_monthly_budget_is_not_sampled_until_the_next_month`, `test_a_replica_that_this_install_cannot_decrypt_is_never_sampled_and_is_no_finding` (reason `identity_unreadable`), `test_a_replica_of_another_key_version_is_never_sampled_and_gives_its_reason`, `test_a_sample_that_cannot_decrypt_a_stored_replica_quarantines_it`, `test_the_sample_budget_field_is_optional_and_a_wrong_value_is_shown_not_guessed` (six cases: absent, zero, a figure, negative, text, boolean), `test_a_budget_of_zero_turns_sampling_off_for_that_store`, `test_the_sample_pick_and_the_month_release_read_the_sample_index_with_no_sort`; each seen red against its reverted change (the zero-budget test goes red when a zero is read as the default). On 1,000,000 generated rows (50,000 skipped for size last month) the sample pick plan is `SEARCH media_replicas USING INDEX media_replicas_sample (store=? AND key_version=? AND sample_month=? AND decrypted_at<?)` with no temporary b-tree and takes 0.004 ms (best of 20, median 0.004 ms); a month release of 256 skipped rows reads the same index and is one bounded write of about 100 ms (best of 5, cold cache, four indexes to update), run at most once per store per round and only while skipped rows of an earlier month remain.

## 3. Quarantine, finding and re-copy

- [ ] 3.1 Quarantine on absence, size or digest mismatch and failed sample; never on unavailability. Make eviction quarantine and report a mismatch in the store serving eviction. Prove the drill: one byte flipped in a stored replica becomes a finding at the next check, and eviction of that original reports it.
- [ ] 3.2 Re-copy a quarantined replica into its store from the local original or another usable replica, replacing exactly the quarantined entry by compare-and-swap under the guard. Prove both sources, the `no_verified_source` finding, and that a binding edited meanwhile is not overwritten.

## 4. Fixity

- [ ] 4.1 Drop quarantined replicas from repair candidates, quarantine a replica that fails its hash during repair, and keep a verified copy that could not be put in place. Prove in `tests/test_original_fixity.py` that the retry after a failed Trash move reads the kept copy and makes no replica read.

## 5. Status and delivery

- [ ] 5.1 Extend the status, the per-original view and a paged findings list with the new values. Prove that a presence-only replica never reads as content-verified, that a lost table reads unknown and then never checked, and that the aggregate stays one grouped query.
- [ ] 5.2 Update `docs/` for the owner: what each level proves, the budget field and the findings. Run `openspec validate --all --strict` (1.14.0), the privacy gate and the scoped suites, then archive this change.
