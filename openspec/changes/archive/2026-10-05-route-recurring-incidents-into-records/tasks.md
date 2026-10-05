## 1. Claim hygiene

- [x] 1.1 Red: prose claims ("not because after first use") capture a page sharing only those words; `failure`/`failures` and `regression`/`regressions` do not meet (`tests/test_incident_records_routing.py`, part 1).
- [x] 1.2 Compare terms through the shared fold owned by `vocabulary_fold.fold_term`.
- [x] 1.3 Consolidate function words as `structure_promotion.FUNCTION_WORDS`; `_STOPWORDS` is that set plus glue.
- [x] 1.4 `collection_claims.normalize_terms` drops function words and folds, on claims and observations alike, filtering folded tokens as raw ones so it is idempotent (`test_normalize_terms_is_idempotent`); update existing expectations to folded forms.

## 2. Structured claims

- [x] 2.1 Red: `claims.match` parses, validates (named refusals) and is described; a predicate route is strong with no word overlap; every key must hold; ties stay silent; routing sees type, category and project.
- [x] 2.2 Parse `claims.match` into `CollectionManifest.claim_match` with no schema-version bump; extend the JSON schema and `describe` contract.
- [x] 2.3 `RoutingTarget.match`, predicate-first `route(..., facets=...)`, and `is_routing_target` accepting declared membership.
- [x] 2.4 Write path: `_records_routing_facets` feed predicates only, never coverage terms; a page contradicting a declared key is excluded from coverage routing; declared winners rank by predicates held, then coverage; Evidence writes pass their sidecar's facets; facets stored on the observation component and re-applied at serve (`test_a_contradicting_project_never_routes_by_coverage`, `test_facet_values_are_predicates_not_coverage_terms`, `test_declared_winners_rank_by_predicates_held_then_coverage`, `test_evidence_writes_route_by_their_facets`).
- [x] 2.5 Audit path: `_observation_page_signal` mirrors the write path; predicate evidence joins the finding and its signal version.
- [x] 2.6 Document `match` generically in the scaffold `planning-records.md`; refresh the skill contract stamp and plugin mirror; `tests/test_scaffold_no_leak.py` passes.

## 3. Backfill on create or claims change

- [x] 3.1 Red: creating a collection yields one grouped item with count and bounded refs; stable fingerprint as pages grow, new one on claims change; not re-raised after dismissal; writes leave it untouched; reflected pages leave it.
- [x] 3.2 `audit.backfill_component` and the bounded backfill collection in `_check_unreflected_observations` (recompute only): a two-cheap-word or predicate pre-filter, a per-collection parse budget, a cursor keyed by the declared-claims signal that resumes later pages, `truncated` beside the count, strong or predicate routes only, asked once at every prominence (`test_backfill_budget_is_per_collection_and_a_cursor_reaches_later_pages`, `test_backfill_prefilter_needs_two_cheap_claim_terms`, `test_only_strong_or_predicate_routes_join_the_backfill`, `test_a_dismissed_backfill_stays_dismissed_after_ordinary_appends`, `test_backfill_is_asked_once_at_every_prominence`).
- [x] 3.3 Serve-time recomposition, coverage inclusion, and record-write settlement skipping the grouped kind in `due_state`.

## 4. Act, don't advise

- [x] 4.1 Red: maximal files with a ready payload; balanced asks once then holds; light/off hold; a recurrence appends an occurrence; moderate routes stay plain.
- [x] 4.2 `records_disposition` with item mapping, recurrence detection over the claims projection excluding items that already cite the note, a complete `record_memory` update payload with hash guards for an occurrence, a disposition on edit only while the page's entry is open, budget checks before anything is marked asked, and the file-locked asked-once ledger (`test_editing_a_filed_note_at_maximal_neither_files_nor_recurs`, `test_editing_a_filed_note_at_balanced_does_not_ask_again`, `test_an_over_budget_disposition_drops_only_itself`, `test_proposed_item_shapes_sources_and_leaves_page_status_alone`).
- [x] 4.3 Red then fix: the compact terminal dropped any advisory with keys beyond the original five; project the bounded disposition fields.
- [x] 4.4 End to end: a real failure-note write returns `file` and its payload succeeds through `record_memory` exactly as returned; a second note returns `append_occurrence`, whose payload also runs exactly as returned and adds the second note to the item's sources (`test_a_failure_note_is_filed_through_the_returned_payload`).
- [x] 4.5 Bootstrap points at the disposition command-free within the compact byte ceiling (main 62,857 bytes, this change 62,864; the ask-once backfill wording kept the integrated branch at 62,643 bytes, unchanged).

## 5. Collection candidate noise budget

- [x] 5.1 Red: 300 generic-word units yield no strong candidates; one real dated domain yields exactly one candidate; constants are provisional; population survives recomposition; serving is capped.
- [x] 5.2 Function-word exclusion, `common_terms` distinctiveness ceiling, `select` merging identical supports, stored common terms for serve recomposition.
- [x] 5.3 Per-response cap of served `collection_candidate` rows after audience filtering and triage.

## 6. Delivery

- [x] 6.1 Record the before/after on the synthetic fixture in design.md and the PR body.
- [x] 6.2 Merge, then synchronize these deltas into the canonical specs and archive the change (orchestrator).
  Merged in #1424 (84f95bc91); archived in this delivery with `openspec archive`.
