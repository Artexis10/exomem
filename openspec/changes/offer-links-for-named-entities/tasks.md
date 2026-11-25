# Tasks

Each task names its check. Run the scoped suites while building and the full corpus before delivery (task 4.1).

## 1. The offer

- [x] 1.1 Build the name index from the write's registry entries, and the matcher of design D2. Check: unit tests for title, alias, longest match, ambiguous key, the page's own Entity, an already-linked Entity, masked regions (frontmatter, fenced and inline code, wikilinks, Markdown links, URLs), a Unicode name, and the pre-write comparison; a fault yields no block.
  Evidence: `tests/test_name_mentions.py` (10 cases: title and alias, longest match, an ambiguous key that takes its words and yields nothing, word boundaries and Unicode, no line spans, masked regions) and `tests/test_unlinked_mentions.py` (the page's own Entity, an Entity already linked anywhere, masked regions through the MCP server, only a raised mention count is offered, and a matcher fault commits the note with no block). Removing the own-page exclusion fails `test_only_a_new_unlinked_mention_of_an_unlinked_entity_is_offered`, and narrowing both fault guards fails `test_a_fault_in_the_matcher_costs_the_offer_and_never_the_write`.
- [x] 1.2 Add the `unlinked_mentions` block on the `undeclared_referents` seam: gates, family, dismissal, batch hold and post-batch carrier, compact projection. Check: the workflow test of design "Proof" through the in-process MCP server, the restricted-writer twin, a quieted family, a dismissed Entity, and one batch that lists an Entity once per page.
  Evidence: `tests/test_unlinked_mentions.py` runs through the in-process MCP server: the offer on `remember` and `edit_note`, three switch-offs (`structural_suggestions` off, light prominence, a quiet family), a quiet family that leaves `undeclared_referents` live, a dismissed Entity, a batch that lists an Entity once for each page it commits, and the restricted-writer twin (the same receipt whether a same-name Entity is withheld or absent). The family is registered in `review_state.registered_families()`.
- [x] 1.3 Build the ready call of design D3. Check: the ready call sent unchanged commits exactly the listed links; a mention whose text repeats on the page gets a unique `old_text`; a page edited in between makes the call refuse and change nothing.
  Evidence: `test_a_note_that_names_entities_in_prose_is_offered_their_links_and_the_ready_call_links_exactly_those` sends the `call` unchanged and finds exactly the listed links and no other change. `test_a_repeated_name_gets_a_unique_anchor_and_a_page_edited_in_between_refuses_the_call` links the first of two equal mentions and shows that the call refuses and changes nothing after another edit.
- [x] 1.4 Teach the block in one scaffold line beside the `undeclared_referents` guidance, and regenerate the generated copies through their producers. Check: bootstrap budget, skill-contract, plugin-sync and scaffold no-leak tests pass with no limit raised.
  Evidence: the scaffold sentence is in `src/exomem/_scaffold/_Schema/references/before-writing.md`. The generated copies come from `scripts/refresh-skill-contract.py`, `exomem package-skills --plugin-root plugins/claude-code` and `scripts/cloud-plugin.py build`, in that order. The bootstrap budget, skill-contract, plugin-sync and scaffold no-leak tests pass with no limit raised.

## 2. Observe relevant due delivery

- [x] 2.1 Record on the `working_set.relevant_due` span whether activation attached a due candidate and the candidate's category, as integer span fields. Check: a ledger row for an activation that attached an ownership question shows it, and one without a candidate shows none. This supplies the server half of `surface-relevant-due-items` task 3.2.
  Evidence: `tests/test_activate_context_surface.py::test_the_ledger_row_shows_whether_activation_attached_a_due_candidate_and_its_category` reads the `working_set.relevant_due` span of the call ledger after an activation that attached a due prediction check. The span fields are `{"attached": 1, "attached_prediction_window": 1}`. After the candidate is withheld, the span has neither key. The test uses a prediction check, because an ownership question needs the ownership fixtures; the field key is derived from the category, so both categories take the same path.

## 3. Measurement

- [ ] 3.1 Before release, an evaluation lane writes capture cases that name existing Entities in prose (including ambiguous and unrelated same-word cases) and runs one development round on real models at one draw: the same cases with and without the block. Report the per-case difference in the share of existing-Entity names linked, with its interval, the false links committed, and the cost. Ship the block only if links rise and false links do not.

## 4. Delivery

- [ ] 4.1 Run the scoped suites, the full corpus, ruff, the OpenSpec strict gate and the privacy gate; obtain one independent review with C4, C6 and scale verdicts; merge and release.
- [ ] 4.2 After install, repeat the read-only census sample of organic notes after at least seven days of use, and report the linked share against the 2026-11-25 baseline (30 of 58).
- [ ] 4.3 Synchronize the delta and archive this change with `openspec archive` when every task above has evidence.
