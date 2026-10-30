## 1. Semantic writer

- [x] 1.1 Commit structural creations as one batch with per-note closure, back-references and contract verdicts (`test_a_chunk_of_notes_commits_with_one_bullet_and_one_log_entry_per_note`).
- [x] 1.2 Refuse a batch that plans one file twice and fall back per note (`test_two_notes_that_plan_one_file_give_the_outcomes_of_creating_them_one_by_one`).
- [x] 1.3 Roll the batch back when installation fails (`test_a_failure_while_the_batch_installs_leaves_no_note_and_each_is_then_made_once`).
- [x] 1.4 Keep each creation's outcome once the batch is written, and never commit it again (`test_a_failure_after_the_batch_is_written_fails_only_its_own_note`).
- [x] 1.5 Judge the batch before the guard, and judge it again against one fresh corpus on a stale stamp (`test_a_commit_between_validation_and_the_batch_judges_the_batch_again`).
- [x] 1.6 Refuse a note that replaces a predecessor (`test_a_note_that_replaces_a_predecessor_is_refused_from_the_batch`).
- [x] 1.7 Report a write that fails after its commit point as every creation's outcome (`test_a_write_that_fails_after_its_commit_point_creates_no_note_again`).

## 2. Vocabulary gate

- [x] 2.1 Validate a tuple of sealed manifests and name each derived write once (`test_a_tuple_of_manifests_names_every_derived_write_once`).
- [x] 2.2 Refuse a claim that two manifests share (`test_two_manifests_claiming_one_path_are_refused`).
- [x] 2.3 Refuse a claimed write whose bytes differ from its claim (`test_a_manifest_in_a_tuple_refuses_a_write_whose_bytes_differ_from_its_claim`).

## 3. Adoption

- [x] 3.1 Add `adopt.create_draft_notes` with one outcome per note and a per-note fallback.
- [x] 3.2 Give a note whose target another writer took `TARGET_OCCUPIED` and commit the others (`test_a_target_taken_by_another_writer_refuses_that_note_and_commits_the_others`).
- [x] 3.3 Call `adopt.create_draft_notes` from `adoption_run`, making notes one at a time until one is made and timed (`test_apply_times_one_note_then_commits_the_rest_of_the_chunk_in_one_batch`, `test_a_quick_refusal_does_not_make_the_next_batch_of_notes_look_free`).
- [x] 3.4 Fall back only when the batch raised before it wrote, and log the refusal code once per refused batch.
