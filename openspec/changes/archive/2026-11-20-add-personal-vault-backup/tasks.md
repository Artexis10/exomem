# Tasks

Each box carries its evidence: the commit or test that proves it. Test names are in `tests/test_vault_backup.py` unless a path is given.

## 1. State declaration

- [x] 1.1 Add the closed `rebuildable` attribute to the state registry, set it false for the three `external-canonical` families and the four owner-decision families named in the design, and fail registry load for a canonical family that is rebuildable. Prove that the registry exposes exactly those families as not rebuildable, and that a rebuildable canonical family is refused.
  Evidence: `tests/test_state_root_placement.py::test_the_vault_backup_stages_exactly_the_families_declared_not_rebuildable`, and `tests/test_reserved_admin_paths.py` for the prefilter field.

## 2. Configuration and restic wrapper

- [x] 2.1 Add `vault_backup` with config loading from `EXOMEM_VAULT_BACKUP_CONFIG`, secret handling in the subprocess environment only, and the bounded `restic` wrapper. Prove that an entry inside the vault is invalid, that secrets are absent from the argument list and present in the environment, and that a log line scrubs configured values.
  Evidence: `test_an_unusable_entry_is_reported_with_the_field_and_never_run` (a secret path or a local repository inside the vault is refused), `test_a_failing_restic_leaks_no_secret_into_the_log_or_the_record` (a stand-in restic reads the repository and the credential from its environment, and neither value reaches the log or the record), `test_stopping_the_service_ends_a_running_backup_and_leaves_no_false_failure` (the wrapper's stop and timeout bound).

## 3. Snapshot, retention and staging

- [x] 3.1 Stage non-rebuildable state with SQLite's online backup API and run `restic backup` and `restic forget --prune`. Prove with a real restic binary that a database written during staging restores and passes its integrity check, that no write-ahead-log file is staged, and that a failed backup forgets nothing.
  Evidence: `test_state_that_cannot_be_rebuilt_is_staged_consistently_and_rebuildable_state_is_left_out`, `test_retention_forgets_older_snapshots_after_a_complete_backup`, `test_a_partial_backup_keeps_history_and_names_the_state_file_that_failed`.
- [x] 3.2 Support `include_originals` false. Prove that the snapshot holds no original's bytes and that status warns with the unreplicated count as `unknown`.
  Evidence: `test_originals_left_out_by_the_owner_are_counted_and_the_sidecar_stays`.

## 4. Verification

- [x] 4.1 Add the rotating `restic check` and the sampled restore against the admission hashes. Prove with a real restic binary that a healthy repository passes, and that a corrupted pack surfaces as a finding with a path or code.
  Evidence: `test_the_whole_workflow_backs_up_verifies_and_recovers_a_deleted_note` (healthy), `test_a_damaged_repository_pack_is_a_finding_in_the_check_and_the_sampled_restore` (damaged).

## 5. Owner operations and status

- [x] 5.1 Add `exomem backup status`, `run` and `restore`, with the restore refusals. Prove that a deleted note restores byte-identical from an earlier snapshot into a new directory, and that a target inside the vault refuses.
  Evidence: `test_the_owner_cli_backs_up_reports_and_restores_into_a_separate_directory`, `test_a_restore_target_that_could_overwrite_the_vault_or_files_is_refused`, `test_a_restore_path_that_leaves_the_vault_is_refused`.
- [x] 5.2 Add the `vault_backup` block to `exomem status` and the service start and stop hook. Prove "not configured", "restic not found", unknown before the first run, and a lost record reading as unknown.
  Evidence: `test_nothing_runs_until_the_owner_names_a_config`, `test_status_reads_restic_not_found_when_the_executable_is_missing`, `test_the_service_thread_backs_up_on_its_own_and_never_creates_the_repository`, and the unknown and never reads in `test_the_whole_workflow_backs_up_verifies_and_recovers_a_deleted_note`.

## 6. Acceptance

- [x] 6.0 Review corrections (pull request review): a shared repository keeps each vault's and machine's snapshots apart; a sample that drew nothing and a successful retention are stamped honestly; a lost record reads `unknown` for findings and the last error; secrets are scrubbed before the log line is cut; leaving originals out lists paths instead of one exclude per original; a timed-out restore is never run again into the same target; an unknown key and a config file inside the vault are refused and the status shows the effective keep rules; SQLite staging copies in one step, honours a stop and leaves no sidecar file; an external registry family must declare `rebuildable`.
  Evidence: `test_retention_never_prunes_another_vaults_snapshots_in_a_shared_repository`, `test_retention_never_prunes_another_machines_snapshots_of_the_same_vault_path`, `test_a_backup_without_originals_still_keeps_exactly_the_snapshots_the_rule_names`, `test_a_backup_without_originals_keeps_whole_clean_folders_and_leaves_only_the_originals_out`, `test_an_empty_sample_never_reads_as_a_passed_restore`, `test_a_successful_retention_records_when_it_last_succeeded`, `test_a_lost_record_reads_unknown_for_findings_and_the_last_error`, `test_a_secret_that_straddles_the_cut_of_a_logged_error_does_not_leak_its_tail`, `test_a_restore_is_repeated_only_when_a_lock_stopped_it_before_it_wrote`, `test_an_unusable_entry_is_reported_with_the_field_and_never_run`, `test_the_status_shows_the_keep_rules_that_retention_applies`, `test_a_config_file_inside_the_vault_is_refused`, `test_staging_a_write_ahead_log_database_leaves_no_sidecar_files_beside_it`, `test_a_stop_ends_staging_instead_of_finishing_every_file`, and `tests/test_state_root_placement.py::test_an_external_family_that_does_not_say_whether_it_is_rebuildable_is_refused`.

- [x] 6.1 Run the end-to-end workflow on a disposable vault and a disposable local repository with a real restic binary: configure, back up, forget, check, sampled restore, delete a note, back up again, restore the note from the first snapshot into a target directory, and read each step in the status. Record the log.
  Evidence: `test_the_whole_workflow_backs_up_verifies_and_recovers_a_deleted_note`; the full module passed with restic 0.19.1 (`39 passed` after the review corrections).
- [x] 6.2 Add the pinned restic install to the CI job that runs these tests, and make a missing restic fail in CI and skip with a clear reason locally.
  Evidence: `scripts/ci-install-restic.sh` reads the pin and checksum from the Dockerfile arguments, the "Install restic" step in `.github/workflows/ci.yml`, and the `restic_binary` fixture, which fails when `EXOMEM_REQUIRE_RESTIC` is set (the `core-tests` job sets it) and skips with the install hint otherwise. A run without restic skipped 8 tests with that reason, and the same run with `EXOMEM_REQUIRE_RESTIC` set failed on the first.
