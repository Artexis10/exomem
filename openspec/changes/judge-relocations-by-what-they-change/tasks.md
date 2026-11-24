## 1. Judgement

- [x] 1.1 Judge each page that a move or its undo reaches, moved or dependent, by the non-worsening rule through its own `non_worsening` flag, apart from `grandfathered`. Proved by `test_a_rename_of_older_pages_moves_them_and_refuses_only_the_page_it_gives_a_new_error` and, for a linker withheld from other audiences, `test_the_owner_gets_no_skip_for_a_withheld_linker`. Catches: a page refused for an error key its before state had, a page let through with a new error key, an undo that the forward move's rule cannot pass, and a restricted writer's skip that reaches the owner.
- [x] 1.2 Keep an older page without an exomem_id in the activation baseline through the governed moves that its lineage records, in the edit path, its re-evaluation at commit and the posthoc audit. Proved by `test_an_older_page_keeps_its_baseline_through_its_moves_until_its_bytes_change`, `test_a_copy_that_names_its_standing_original_in_its_lineage_inherits_no_baseline` and `test_a_duplicate_of_a_moved_page_inherits_no_baseline`. Catches: a renamed older page whose next edit must clear all its debt, a baseline that outlives a change to what the page says, and a copy or duplicate that takes the baseline of the page it copies.

## 2. Cost

- [x] 2.1 Patch the before corpus by the pages that a move changes, instead of deriving the whole vault again. Proved by `test_a_move_patches_the_corpus_to_what_a_full_derivation_gives` over a folder move, a rename to the title's stem, a new stem that captures a bare-title link, one of two pages sharing an id, a vacated path filled again, and a new title or stem that a link typed in decomposed Unicode names. Catches: a step that derives every relation again, and a patched corpus that keeps a stale link resolution.

## 3. Release check

- [ ] 3.1 After the personal service runs a release with this change, rename a set of older pages to their titles through MCP with one approval, then check that only pages given a new error are refused. Catches: a deployment whose vault differs from the generated one.
- [ ] 3.2 Archive this change with `openspec archive judge-relocations-by-what-they-change` after task 3.1, and run `openspec validate --all --strict` before and after.
