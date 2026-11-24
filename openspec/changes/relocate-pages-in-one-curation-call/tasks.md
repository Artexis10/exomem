## 1. Relocation

- [x] 1.1 Add the `relocate` curation action: derive moves, in-place pointer edits, Source project edits and the closing `repoint-links` steps from registry attributes, and seal one plan. Proved by `test_one_relocation_and_one_approval_move_forty_pages_their_sources_and_their_links`. Catches: a rehome that needs a hand-written plan, drops a key another citer needs, or leaves a link to an old path.
- [x] 1.2 Rename only when `to_project` is absent, and register a new key's folder from `folder`. Proved by `test_a_rename_only_relocation_keeps_every_project_and_source` and `test_a_relocation_registers_the_folder_it_names_for_a_new_project`.
- [x] 1.4 Accept `folder` only as one portable folder name, and write it as a quoted scalar. Proved by `test_a_relocation_refuses_a_folder_that_is_not_one_portable_name` and `test_a_new_project_folder_that_yaml_would_read_as_a_number_stays_a_name`.
- [x] 1.5 Count a citer that links a Source by an earlier path, in the same one inbound read. Proved by `test_a_source_cited_by_its_earlier_path_keeps_the_old_project`.
- [x] 1.6 Number a name that another live page's title holds. Proved by `test_a_rename_never_takes_the_title_another_live_page_holds`.
- [x] 1.7 Resolve the named memory refs over one identity census. Proved by `test_pages_named_by_memory_ref_resolve_over_one_identity_census`.
- [x] 1.8 Write a list to a field whose registry `shape` is `list`. Proved by `test_a_list_field_that_gains_a_project_stays_a_list`.
- [x] 1.9 List a Source that its leaf refuses at preparation under `refused`, and go on. Proved by `test_a_source_its_leaf_refuses_is_listed_and_the_relocation_goes_on`.
- [x] 1.10 Prepare a relocation's steps under the request budget, answer `preparing` with `progress` when a call runs short, and go on from the kept steps when the same call is made again. Proved by `test_a_proposal_that_runs_short_goes_on_from_its_prepared_steps_to_the_same_seal` and `test_a_page_that_changes_after_its_step_was_kept_refuses_the_seal`. Catches: a proposal that outlasts the request budget and keeps nothing, a call that prepares kept steps again, and a kept step sealed after its page changed.
- [x] 1.11 Keep a paused preparation only for callers with the same audience, purpose and connector ceiling, and delete an expired one when a read finds it. Proved by `test_a_kept_preparation_goes_on_only_for_a_caller_under_the_same_ceiling`. Catches: an owner client that seals steps prepared under a wider ceiling, so its proposal counts a referrer withheld from it and its apply stops on a write it may not make.
- [x] 1.3 Read the fields that carry project keys from the `page-fields` registry (`carries: project-key`), in the relocation and in the pointer write's key check. Proved by the relocation test, whose Sources differ only in carrying fields.

## 2. Execution

- [x] 2.1 Run several `apply`, `resume` and `apply-compensation` steps per request within the budget, each at its own boundary seams, and answer `executing` with `progress`. Proved by the relocation test (fewer calls than steps) and `test_a_foreground_write_lands_between_two_steps_of_one_call`. Catches: one step per call, and writer-busy refusals during long runs.
- [x] 2.2 Keep crash safety per step. Proved by `test_an_interrupted_relocation_resumes_without_losing_or_repeating_a_step`.
- [x] 2.3 Take the pointer write for one unowned pointer field on any page outside a curated tree, reversible from its sealed delta. Proved by the relocation test's entity step and its byte-identical compensation.
- [x] 2.4 Answer a refusal after committed steps as `stopped` beside the progress, with the stopped step's `committed`: a busy refusal keeps `executing` with `resume`, any other refusal answers `partial` or `blocked`. Proved by `test_a_busy_vault_after_a_committed_step_stops_the_call_beside_its_progress` and `test_another_refusal_after_a_committed_step_stops_the_call_instead_of_raising`. Catches: a call that raises after it committed steps, and a busy vault reported as a failed run.
- [x] 2.5 Run each step's derived fan-out before the next step, except inside a request that already holds the boundary. Proved by the relocation test's one-approval undo, whose source edits bind pages that its link restore rewrote, and by `test_a_revert_drains_its_steps_fan_out_once_its_hold_ends`. Catches: a step that reads the corpus as it was before the step ahead of it, and a revert whose hold grows by every step's fan-out.
- [x] 2.6 Split the link rewrite by referring page into bounded steps that each seal only the moves their pages use, and find each guarded directory's allowed census names once per batch. Proved by `test_a_relocation_with_many_referrers_rewrites_them_in_bounded_disjoint_steps`, `test_a_plan_that_names_one_referrer_in_two_link_rewrites_is_refused` and the measurement in design.md. Catches: a link rewrite whose hold outlasts the request budget, and a split plan that seals every move once per step.

## 3. Size and preview

- [x] 3.1 Check one 1 MiB bound when the store writes a plan, lift the step cap only for a sealed `derived_by`, and seal no preimage that the undo does not read. Proved by `test_a_relocation_too_large_to_load_again_is_refused_before_it_seals`. Catches: a plan that seals and then never loads.
- [x] 3.2 Answer the relocation, `preview` and `propose-compensation` of a derived plan with a grouped summary, and seal the relocation's summary so a later preview shows the same groups. Proved by the relocation test's preview and undo assertions.
- [x] 3.3 Teach the relocation in the shipped skill text, kept generic.

## 4. Release check

- [ ] 4.1 After the personal service runs a release with this change, relocate a set of pages through MCP with one approval, then check the run's status, its links and its undo preview. Catches: a deployment path whose request budget or boundary differs from the test's.
- [ ] 4.2 Archive this change with `openspec archive relocate-pages-in-one-curation-call` after task 4.1, and run `openspec validate --all --strict` before and after.
