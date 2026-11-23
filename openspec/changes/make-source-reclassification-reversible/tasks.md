# Tasks

Phase 1 is implemented in [PR #1632](https://github.com/Artexis10/exomem/pull/1632).
An independent review approved head `29da92c45` after two correction rounds.
Evidence for 1.1 to 2.1 is in `tests/test_reclassify_source.py` and `tests/test_derived_identifier_egress.py`:

- 1.1: `test_a_legacy_source_is_previewed_moved_twice_and_reverted_through_the_operation` (stem, alias and heading links in an Evidence page) and `test_an_evidence_path_link_refuses_the_move_and_nothing_changes`.
- 1.2: `test_a_reclassified_artifact_page_follows_its_bytes_and_keeps_its_other_fields` and `test_a_legacy_previous_path_is_kept_but_never_reverted_by_guessing`.
- 1.3: `test_a_revert_refuses_a_previous_location_another_page_now_occupies` and the round trip in the end-to-end test.
- 1.4: `test_a_restricted_reclassification_preview_answers_as_if_no_withheld_page_linked_it` and `test_a_restricted_reclassification_and_revert_answer_as_if_no_withheld_page_linked_it`.
- 2.1: `test_a_legacy_source_is_previewed_moved_twice_and_reverted_through_the_operation`, run through the `manage_memory_file` dispatcher.

## 1. Phase 1: safe, reversible reclassification

- [x] 1.1 Report a link rewrite as changed only when bytes change; prove a stem, aliased-stem or heading link in an append-only referrer no longer refuses a reclassification, and a path-form link still does.
- [x] 1.2 Record `reclassified_from` as a list of path, kind and domain entries through parsed-span patches, including companion pointers; prove body bytes equal before commit and a legacy scalar reads as one entry.
- [x] 1.3 Add revert of the latest entry, including a recorded `other`; prove refusal for an unknown classification or an occupied path, and a round trip that restores the original bytes and references.
- [x] 1.4 Report every referrer outcome and refusal in the preview without naming withheld pages; prove a restricted mover's answer matches a vault without the withheld page.

## 2. Phase 1 delivery

- [x] 2.1 Run one end-to-end workflow: preview, reclassify and revert a Source cited by an Evidence page by stem and by a mutable Note.
- [x] 2.2 Obtain exact independent review, pass full CI and installed proof, merge, and verify the ordinary release.

## 3. Separately authorized operation

- [ ] 3.1 Preview the `Sources/Other` drain on the personal vault after installation; record how many Sources the preview holds back for path-form append-only referrers.
- [ ] 3.2 Apply cleared moves in reviewed batches through governed reclassification and check references after each batch.

## 4. Phase 2, delivered by `add-curation-relocation-protocol`

- [x] 4.1 After `add-curation-relocation-protocol` ships, prove that a reclassification writes `moved_from`, that a Source linked by path from an Evidence page moves with only that page's link span changed, and that a revert restores the Evidence page's exact bytes.

  Evidence, delivered by `add-curation-relocation-protocol` (PR #2883, archived 2026-11-17):
  - `tests/test_reclassify_source.py::test_the_correction_records_its_reason_and_previous_path` asserts that the reclassified Source carries a `moved_from` entry with the previous path, kind and domain, and no `reclassified_from`.
  - `tests/test_relocation_repoint.py::test_a_reclassification_repoints_an_evidence_link_and_its_revert_restores_the_bytes` asserts that an Evidence page linking the Source by path differs after the move only in that link's target, and that the revert restores the Evidence page's exact bytes.
  - `tests/test_relocation_repoint.py::test_a_reclassified_source_does_not_say_how_many_pages_it_repointed` asserts that the lineage entry is the same whether the correction repointed two Evidence pages or none, and that the revert restores both pages.
  - The three tests pass on main.
- [ ] 4.2 Archive this change with `openspec archive` after `add-curation-relocation-protocol` is archived, with shipped evidence.

## 5. Source pointer custody

A move of raw bytes beside an unpaired card left the card and its referrers naming a missing path. This group makes a move rewrite prose path mentions, makes pointer drift auditable, and makes it repairable through the same reversible history. Decisions 1 to 3 stand: no append-only byte changes, and the history entry gains one closed key, `pointer`.

- [x] 5.1 Rename the exact vault path in the body of each page that cites a moved file, found through the lexical catalogue; leave Source and Evidence pages as captured and report them; prove the move and the reclassification preview in `tests/test_source_pointer_custody.py` and `tests/test_reclassify_source.py`.
- [x] 5.2 Add the `source_pointer_drift` audit category over `evidence_file`, `data_file` and the nested `artifact_path` pointers, from file metadata only, with a one-candidate proposal and a fix that succeeds; prove it in `tests/test_source_pointer_custody.py` and the restricted-caller twin in `tests/test_derived_identifier_egress.py`.
- [x] 5.3 Add `artifact_path` to reclassification as a pointer repair that records and reverts the previous values, and refuses a missing or mismatching target; prove repair and revert in `tests/test_source_pointer_custody.py`.
- [x] 5.4 Write the unclassified kind, not `other`, in the sidecar of a binary artifact; prove it in `tests/test_preserve.py`.
- [ ] 5.5 After release, run the audit category against the owner's vault and repair each card that names a missing path; record the finding count before and after.
  - Done 2026-11-20 on 0.191.0: four dataset-export cards that cited a missing path were reclassified with `artifact_path`. Each was previewed, applied and checked on disk: kind, domain, classification history and an `evidence_file` that names the real file. The four artifacts' SHA-256 values are unchanged. Their captured prose still cites the old path, as a Source body is immutable.
  - "path mentions were not checked" appeared on 2 of the 4 operations, because the search index was not current.
  - Still open, so this task stays unchecked: on that vault the audit category stops at its 50 s budget with coverage `unknown`, and it takes no scope. The vault-wide finding count before and after is therefore unknown.

## 6. Bounded managed preview and paged approval

- [x] 6.1 Prove current inbound links and published prose candidates on the request deadline. Leave incomplete evidence to the existing recovery workers.
  - Done 2026-11-22: an incomplete prose lookup returns warming without a digest. Watcher and resolver recovery run off the request path, and an overlapping Records read succeeds.
- [x] 6.2 Send proven overflow to the maintenance job runner. Release the complete summary and bounded report pages under the same live owner authority.
  - Done 2026-11-22: a frozen file-mode run on 10,001 Notes and 10,001 Sources, with 10,000 referrers, returned the handle in 96 ms. A concurrent Records read took 102 ms and the job 11.9 s. 10,001 entries arrived over 79 pages, median 48 ms, largest 10,393 JSON bytes. Neither flat folder was enumerated.
- [x] 6.3 Bind the approval digest to the complete report. Refuse stale approval before the first write and verify a fresh approval through the normal apply operation.
  - Done 2026-11-22: the large preview route test refuses a stale digest without a write, then applies a fresh one and moves the Source.
- [ ] 6.4 Exercise a 20,000-page vault with 10,000 referrers to one Source. Verify all report pages and a concurrent Records read. Obtain exact independent review, required CI, and installed proof.
- [x] 6.5 Prove governed paging with a long receipt history and concurrent disclosure appends. Withhold all report facts after historical receipt edits or a pending erasure.
  - Done 2026-11-22: ten rotated receipt parts (524,670 bytes) held a governed erasure while another thread appended disclosure receipts. Every page answered within the request budget, median 47 to 71 ms. A same-size edit to a closed part, and an active-part edit after disclosure, both withheld every report fact.
- [ ] 6.6 Prove bounded terminal release for an oversized governed referrer, including its custody and any bridge dependencies.
  - Partly done 2026-11-22: a simple governed referrer (no container parent, no bridge) is released from the job's held proof with zero reads of the referrer. Ten 2 MiB referrers went from a 15.4 ms to a 1.1 ms median page. A referrer with a container or bridge dependency still takes the full terminal path, so this task stays unchecked.

## Phase 1 released delivery evidence

[PR #1632](https://github.com/Artexis10/exomem/pull/1632) merged as
`762653a0c0030b65898355983664efcb435ca0a9` with ordinary CI green.
The [review](https://github.com/Artexis10/exomem/pull/1632#issuecomment-6070111302)
and [correction recheck](https://github.com/Artexis10/exomem/pull/1632#issuecomment-6072189911)
approved phase 1.
The merge is an ancestor of [v0.115.0](https://github.com/Artexis10/exomem/releases/tag/v0.115.0),
commit `5abed4438ae767af5c89ccd1438c63e35df107d8`.

A fresh isolated CPython 3.13.12 run against the published release passed
`tests/test_reclassify_source.py::test_a_legacy_source_is_previewed_moved_twice_and_reverted_through_the_operation`:
1 passed in 6.00 seconds, with no skips or failures.
The unchanged maintained test came from the exact release tag.
The published PyPI wheel has SHA-256
`b7b9a602d76f056835463edddf8ebc85a0eeb765fd4cc1094f01a1a0f6e6f49d`;
all 573 package members matched the tag and installed distribution.
The dispatcher previewed, moved twice and reverted twice while preserving Evidence links and restoring mutable Note bytes.
The run made no provider calls and removed its private runtime state afterward.
The historical 288-case claim remains unverified.
The production drain and phases 3 and 4 remain open; no production Source move occurred.
