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

## 4. Phase 2, only if 3.1 holds any Source back

- [ ] 4.1 Update this design for history-aware read resolution under the Decision 5 constraints, choose the history store, and obtain review before implementation.
- [ ] 4.2 Archive this change with shipped evidence, or withdraw Phase 2 with the 3.1 count as its reason.

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
