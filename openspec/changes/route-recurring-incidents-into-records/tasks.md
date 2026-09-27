## 1. Claim hygiene

- [x] 1.1 Red: prose claims ("not because after first use") capture a page sharing only those words; `failure`/`failures` and `dogfood`/`dogfooding` do not meet (`tests/test_incident_records_routing.py`, part 1).
- [x] 1.2 Add `vocabulary_fold.fold_term` to the shared contract (lowercase, `_`/whitespace to `-`, plural and `-ing`/`-ed` folds, length floor, `EXCEPTIONS`, idempotent).
- [x] 1.3 Consolidate function words as `structure_promotion.FUNCTION_WORDS`; `_STOPWORDS` is that set plus glue.
- [x] 1.4 `collection_claims.normalize_terms` drops function words and folds, on claims and observations alike; update existing expectations to folded forms.

## 2. Structured claims

- [x] 2.1 Red: `claims.match` parses, validates (named refusals) and is described; a predicate route is strong with no word overlap; every key must hold; ties stay silent; routing sees type, category and project.
- [x] 2.2 Parse `claims.match` into `CollectionManifest.claim_match` with no schema-version bump; extend the JSON schema and `describe` contract.
- [x] 2.3 `RoutingTarget.match`, predicate-first `route(..., facets=...)`, and `is_routing_target` accepting declared membership.
- [x] 2.4 Write path: `_records_routing_facets`, facet values as routing terms, facets stored on the observation component and re-applied at serve.
- [x] 2.5 Audit path: `_observation_page_signal` mirrors the write path; predicate evidence joins the finding and its signal version.
- [x] 2.6 Document `match` generically in the scaffold `planning-records.md`; refresh the skill contract stamp and plugin mirror; `tests/test_scaffold_no_leak.py` passes.

## 3. Backfill on create or claims change

- [x] 3.1 Red: creating a collection yields one grouped item with count and bounded refs; stable fingerprint as pages grow, new one on claims change; not re-raised after dismissal; writes leave it untouched; reflected pages leave it.
- [x] 3.2 `audit.backfill_component` and the bounded, pre-filtered backfill collection in `_check_unreflected_observations` (recompute only).
- [x] 3.3 Serve-time recomposition, coverage inclusion, and record-write settlement skipping the grouped kind in `due_state`.

## 4. Act, don't advise

- [x] 4.1 Red: maximal files with a ready payload; balanced asks once then holds; light/off hold; a recurrence appends an occurrence; moderate routes stay plain.
- [x] 4.2 `records_disposition` with item mapping, recurrence detection over the claims projection, and the asked-once ledger.
- [x] 4.3 Red then fix: the compact terminal dropped any advisory with keys beyond the original five; project the bounded disposition fields.
- [x] 4.4 End to end: a real failure-note write returns `file`, the returned payload succeeds through `record_memory`, and a second note returns `append_occurrence` for the appended item.
- [x] 4.5 Bootstrap points at the disposition command-free within the compact byte ceiling (main 62,857 bytes, this change 62,864).

## 5. Collection candidate noise budget

- [x] 5.1 Red: 300 generic-word units yield no strong candidates; one real dated domain yields exactly one candidate; constants are provisional; population survives recomposition; serving is capped.
- [x] 5.2 Function-word exclusion, `common_terms` distinctiveness ceiling, `select` merging identical supports, stored common terms for serve recomposition.
- [x] 5.3 Per-response cap of served `collection_candidate` rows after audience filtering and triage.

## 6. Delivery

- [x] 6.1 Record the before/after on the synthetic fixture in design.md and the PR body.
- [ ] 6.2 Merge, then synchronize these deltas into the canonical specs and archive the change (orchestrator).
