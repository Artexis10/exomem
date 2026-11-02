## 1. Review fields

- [x] 1.1 Add `check_review_fields` in `relation_review.py`, and drop or refuse review fields at creation commit.
- [x] 1.2 Carry `missing` through `RelationReviewError`, `SemanticWriteError`, the note, link, replace, edit and create_file errors, and one render helper in `commands.py`.
- [x] 1.3 Report `relation_review_fields` from creation validation; drop the fields in `curation`.
- [x] 1.4 Refuse an absent existing-edit hash with `INVALID_RELATION_REVIEW`; add its remediation.

## 2. User summary

- [x] 2.1 Declare `outcome_verb` on `Command` and on the `manage_memory_file` operation rows, and build `user_summary` in `project_terminal`.

## 3. Narration rule

- [x] 3.1 Add `NARRATION_RULE`; carry it in the bootstrap `canonical_loop` and the skill scaffold; document `user_summary` in `mutation-results.md`; regenerate the plugin copies.

## 4. Proof and closure

- [x] 4.1 Run the touched test modules, the bootstrap budget, scaffold-leak and plugin-sync tests, and `openspec validate --all --strict`.
- [x] 4.2 Archive this change with `openspec archive`.
