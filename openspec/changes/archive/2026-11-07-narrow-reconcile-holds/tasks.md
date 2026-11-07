## 1. Measure

- [x] 1.1 Profile each phase of a cold and a warm reconcile on a disposable copy of a real-sized vault.
  - Evidence: a CLI reconcile on a 13,665-page copy, models disabled, on a contended machine, so the absolute numbers are high. Source: the `mutation_lock_released` events and phase checkpoints of each run.
  - Warm: total 4,163 s; one whole-run hold, `command maintain_memory`, of 2,898.7 s. Per phase, in seconds: start 1,491 (mostly before the hold), purging 3.7, units 328, lifecycle 18.9, indexes 2.7, embeddings 0, lexical 806.8, graph 0.1, orphans 16.8, references 48.2, derived receipts 0, drift 49.4, rebaseline 6.1, receipts 0.4, projections 1,390.7.
  - Cold: total 2,791 s; one whole-run hold of 2,249.1 s. The largest phases: projections 780, lexical 738, drift 101, orphans 73, references 37.
- [x] 1.2 From the profile, group the phases into holds under design D1, or stop and report.
  - Evidence: the phases that must hold the boundary take about 90 s of the 2,899 s warm hold, so D1 passes. Design D1 lists the grouping. The units phase took 328 s warm and 0.16 s cold. The profile does not split its audit, which holds nothing, from its repair, which holds the boundary; task 3.3 shows the units hold.

## 2. Implement

- [x] 2.1 Add a reconcile narrowing row in `writer_lease`, honouring `EXOMEM_WIDE_MUTATION_BOUNDARY`.
  - Evidence: `narrow_reconcile` in `LeaseManager.invoke` covers the `reconcile` command and `maintain_memory` mode `reconcile`.
- [x] 2.2 Remove reconcile from `WHOLE_RUN_MODES` and keep the graph join outside every hold.
  - Evidence: `WHOLE_RUN_MODES`, `narrows_boundary` and the dispatch retry are deleted. `tests/test_graph_rebuild_availability.py::test_manager_reconcile_releases_canonical_boundary_before_graph_rebuild` passes.
- [x] 2.3 Give each group from 1.2 its own hold, with a bounded busy wait and stop checks.
  - Evidence: `reconcile._phase_hold` wraps each phase's writes in `maintenance_jobs.chunk_hold`. The receipts phase holds the boundary too, because it closes intents and tombstones from the vault as it reads it.
- [x] 2.4 Re-read each phase's inputs inside its hold under design D2.
  - Evidence: design D2 lists each phase's re-read and each run-long value.
- [x] 2.5 Apply only the purge entries that the plan and the fresh read share, and report the rest.
  - Evidence: `semantic_isolation_changed` in the report and the job receipt; the repair is marked `changed_since_plan`, so the isolation cursors stay put.
- [x] 2.6 Apply only the CLIP and frame orphans that a planned write's plan and its fresh read share, and report the rest.
  - Evidence: `reconcile._planned_orphans` and `orphans_changed` in the report and the job receipt. A run without a plan applies its fresh read.
- [x] 2.7 Remove the unit rows of a parent outside the corpus when its bytes did not move since the unit audit.
  - Evidence: the unit audit records those parents' bytes (`_read_hashes`), and the units hold compares them again.
- [x] 2.9 Leave the unit rows of a parent outside the corpus that a write indexed for its present bytes.
  - Evidence: the unit audit reports the source hashes that an orphan's rows carry (`actual_source_hashes`). `tests/test_semantic_unit_reconcile.py::test_a_page_written_after_the_corpus_read_keeps_the_unit_rows_its_write_indexed` passes, and fails on the code before this task.
- [x] 2.8 Serialize reconcile write runs with a vault-scoped run lock that no ordinary writer takes, under design D5.
  - Evidence: `reconcile._run_lock`; `tests/test_reconcile.py::test_a_second_reconcile_is_refused_while_one_runs` passes, and fails on the code before this task.
- [x] 2.10 Refuse a second reconcile write run at once, under design D5.
  - Evidence: `_run_lock` takes the run lock without blocking. `tests/test_reconcile.py::test_a_second_reconcile_is_refused_while_one_runs` passes.
- [x] 2.11 Rebase the lexical lineage after the rebaseline hold, under design D1.
  - Evidence: `lexstore.rebase_current_catalog_lineage` runs after the hold, as warm-up and the watcher seed call it.
- [x] 2.12 Check that the units repair leaves a corpus page edited after the unit audit, under design D2.
  - Evidence: `semantic_index.current_parent_index_state` already discards a supplied parent state whose hash differs from the committed bytes, so a stale repair cannot write old units over an edit, on `main` or here. A test for the units skip passed with the skip removed and caught only report fields, so the change keeps no such test. The orphan case, where a skip is the only protection, is covered by `test_a_page_written_after_the_corpus_read_keeps_the_unit_rows_its_write_indexed`.

## 3. Verify

- [x] 3.1 Prove that a foreground write commits between two holds of a CLI reconcile and of a job.
  - Evidence: `tests/test_reconcile.py::test_an_ordinary_write_commits_between_two_holds_of_a_cli_reconcile` and `tests/test_maintenance_jobs.py::test_an_ordinary_write_commits_between_two_holds_of_a_reconcile_job` pass. On `main`, the write gets `MUTATION_BUSY`.
- [x] 3.2 Prove that a purge entry that moved is reported and left unchanged.
  - Evidence: `tests/test_records_recall_reconcile.py::test_a_record_restored_after_the_plan_is_reported_and_keeps_its_rows` passes. On `main`, the purge removes the restored record as missing.
- [x] 3.3 Repeat the profile from 1.1 and record the longest hold before and after.
  - Evidence: [after-profile](https://github.com/Artexis10/exomem/pull/2497#issuecomment-6703825324) on the same copy and harness. The longest mutation-boundary hold fell from 2,249.1 s to 5.8 s cold and from 2,898.7 s to 26.9 s warm. Total run times do not compare, because the machine load differed.
- [x] 3.4 Run Ruff, the privacy gate, `openspec validate --all --strict` and the full suite.
  - Evidence: PR #2497 CI on 970f8ac21 passed 48 checks, including all core test shards, with 15 skipped and none failed.
- [x] 3.5 Obtain an independent review, then merge.
  - Evidence: two independent review rounds approved PR #2497 with no blocking findings. It merged as 0d9c02b23 and shipped in v0.163.0.
