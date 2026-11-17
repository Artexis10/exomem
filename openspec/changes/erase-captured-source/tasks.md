## 1. Custody and dependency proof

- [x] 1.1 Add owner-registered canonical Source manifests and producer APIs; keep only attempt residue and progress cursors in private runtime state. Evidence: `source_custody.py` manifests and fences; erase progress rows in `source_eraser._Ledger`; `tests/test_source_custody.py`.
- [x] 1.2 Bind attempt artifacts before surviving bytes and new Source references before full Source staging; distinguish reused observations. Evidence: `test_capture_publishes_canonical_custody_before_a_lost_terminal`, `test_restart_retains_allocated_target_without_reexecuting_lost_capture`. Residue producers move to `erase-captured-source-in-store`.
- [x] 1.3 Reserve and resolve the dependency latch at semantic, origin-binding, history and collection publication seams, including skipped back-references. Evidence: `test_restricted_semantic_commit_tracks_a_skipped_back_reference`, `test_exact_success_is_sticky_without_clearing_a_different_reservation`. Bounded reconciliation and no-effect clearing move to `erase-captured-source-in-store`.
- [x] 1.5 Persist Source-file images and exact copy reservations; qualify indexed lookup with producer checkpoints, fresh births and uncertainty horizons. Evidence: `generated_fragment_store.py`; `tests/test_generated_fragment_store.py`.
- [x] 1.6 Issue a producer birth and complete coverage at fresh enrollment only, when the payload, result and native hooks all record the capture. Evidence: `add.py` enrollment; `test_erase_retires_the_page_copies_and_derived_rows_until_complete` reads a `ready` lookup.

Task 1.4 (legacy coverage reconciliation through the watcher) moves to `erase-captured-source-in-store`.

## 2. Incarnation-scoped cleanup

- [x] 2.1 Register erasure and authenticated policy qualification with default-off host configuration, namespace authority and exact incarnation preconditions. Evidence: `sources.erase` in the service tier; `test_disabled_policy_refuses_before_any_lookup`, `test_repeat_returns_the_same_target_and_a_conflicting_rebind_is_refused`, `test_stale_hash_is_refused_and_leaves_the_source`.
- [x] 2.2 Persist bounded target-qualified progress and implement owned file, original, staging, result-payload and private recovery cleanup in file mode. Evidence: `source_eraser.py`, `NativeCustody.retire_installed`, `NativeCopyCustody.erase`; `test_a_process_killed_at_each_phase_is_finished_by_a_later_retry`, `test_copies_no_owner_can_close_do_not_starve_the_pages_own_copy`.
- [x] 2.4 Require actual derived-owner completion and fence delayed index, projection, rollback and cache publication against replacement incarnations in file mode. Evidence: `test_a_recapture_at_the_same_path_keeps_the_new_incarnation_indexed`, `test_a_find_that_read_before_an_erasure_cutoff_does_not_publish_its_result`. The replica and store parts move to `erase-captured-source-in-store`.
- [x] 2.5 Extend settlement custody reporting and explicit residue cleanup; retain unresolved attempt recovery links through collection. Evidence: `test_settlement_closes_and_reports_owned_allocation_without_canonical_readers`, `test_pre_reference_residue_survives_settlement_and_gc_until_owner_ack`, `test_nested_capture_gc_preserves_outer_residue_and_its_active_admission`.
- [x] 2.6 Refuse generic delete and move of an enrolled Source with `SOURCE_ERASE_REQUIRED`, and keep an enrolled capture's committed path out of the call ledger and the write log. Evidence: the lifecycle refusal tests in `tests/test_source_custody.py`; `test_an_enrolled_capture_leaves_its_path_out_of_the_write_log`.

Task 2.3 (the canonical store purge and provenance integration) moves to `erase-captured-source-in-store`.

## 3. Verification and delivery

- [x] 3.1 Prove dependency refusal, unknown coverage, interrupted cleanup, allocated-but-absent targets, replay retirement and delayed erase against path reuse. Evidence: `tests/test_source_eraser.py`.
- [x] 3.2 Prove new-reader absence and obsolete cache-builder exclusion while overlapping snapshot reads can finish. Evidence: `test_service_erases_a_captured_source_with_passages_until_every_owner_acknowledges`; the erasure cutoff tests in `tests/test_find_hot_cache.py`.
- [x] 3.3 Prove that one progress call costs the same at 2 copies, at 200 copies and with 20,000 unrelated Sources. Evidence: `test_a_progress_call_costs_the_same_however_many_copies_or_unrelated_sources`. The p95 run on the generated reference moves to `erase-captured-source-in-store`.
- [ ] 3.4 Run completion checks, independent stored-data review, privacy and strict OpenSpec validation; synchronize and archive the finished change.
- [ ] 3.5 Deliver the coherent batch through green CI and ordinary release with host erasure policy disabled.
