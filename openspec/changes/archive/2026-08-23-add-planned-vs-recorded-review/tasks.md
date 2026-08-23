## 1. Pure-logic unit tests (red first)

- [x] 1.1 Test the item selection predicate: active + committed + non-empty `progress_evidence` is selected; candidate/planned/blocked status, non-committed commitment, archived lifecycle, and absent/empty/malformed evidence are not.
- [x] 1.2 Test evidence-descriptor normalization: exactly `{collection, role, view}` is accepted in authored order; a non-mapping, wrong key set, unknown role, or non-string view is skipped without raising.
- [x] 1.3 Test the divergence count block over plain dicts: `evidence_bindings`, `resolved_bindings`, `unresolved_bindings`, `progress_bindings`, `completion_bindings`, `progress_observations`, `completion_observations` are all non-negative integers and sum consistently.
- [x] 1.4 Test that the divergence block contains no float and no score-shaped key, including when a completion binding matches zero records.
- [x] 1.5 Test the deterministic item ordering by `(collection_id, plan_id)` and the preserved authored evidence order.

## 2. Review module over shipped primitives

- [x] 2.1 Add `src/exomem/plan_progress.py` with the pure selection, normalization, divergence, and ordering helpers the unit tests bind to.
- [x] 2.2 Discover authorized Planning collections and select items through `planning.query` with exact status and commitment filters and active lifecycle; skip a collection that refuses and count it without disclosure.
- [x] 2.3 Execute each binding through `record_governance.resolve_collection`, `record_governance.query_collection`, and `record_governance.project_query_result`; extract only matched, returned, truncated, and the collection/snapshot identifiers. The view's declared aggregate is NOT extracted — it can carry a full record row, record identities, record values, or a float mean.
- [x] 2.4 Map every refusal to exactly one bounded reason (`collection_unavailable`, `profile_mismatch`, `view_unavailable`, `query_unavailable`, `result_withheld`, `budget_exhausted`), giving missing and withheld collections identical treatment.
- [x] 2.5 Deduplicate `(collection, view)` executions per call, enforce the item cap and the distinct-execution budget, and report `truncated` and `bindings_truncated` explicitly.
- [x] 2.6 Assemble the response envelope: mode, generated_at, derived, read_only, collections_scanned, collections_unavailable, items_matched, items, counters, and the unavailable-reason tally.

## 3. Integration tests over a real two-profile vault

- [x] 3.1 Build a vault with a Planning collection declaring `progress_evidence` and a Records collection declaring saved views, then assert a committed active item reports the exact matched counts of its bound views.
- [x] 3.2 Assert non-selected items (candidate, uncommitted, archived, evidence-free) never appear.
- [x] 3.3 Assert a governance-withheld evidence collection and an absent evidence collection both report `collection_unavailable` with no disclosure.
- [x] 3.4 Assert an unknown saved view reports `view_unavailable` while the rest of the review is returned intact.
- [x] 3.5 Assert a Planning collection named as evidence reports `profile_mismatch`.
- [x] 3.6 Assert a repeated `(collection, view)` executes once and yields identical numbers for every item that binds it.
- [x] 3.7 Assert the execution budget marks overflow bindings `budget_exhausted` and sets `bindings_truncated`.

## 4. Non-goal enforcement tests

- [x] 4.1 Assert the vault is byte-identical before and after a review (hash every file), proving no plan, record, manifest, audit head, activity event, or review state is written.
- [x] 4.1a Assert that under a configured external audience the only new paths are the governance kernel's own disclosure receipts, and that nothing existing changes.
- [x] 4.2 Assert authored `health` is echoed unchanged and that no other health value, verdict, or suggestion appears anywhere in the response.
- [x] 4.3 Assert the whole response contains no float and no key or string matching a score/percent/ratio/rank/severity shape, with the traversal reaching scalars nested in lists.
- [x] 4.4 Assert no Records row, body, or item identity leaks into the response.
- [x] 4.4a Bind aggregate-declaring views (`latest:`, `distinct:`, `group:`, `avg:`, `count`) in the fixture so 4.3 and 4.4 are not blind, and assert the aggregate is withheld entirely while the matched count survives.
- [x] 4.6 Assert the returned item SEQUENCE is identity-ordered over a trio whose pinned plan IDs contradict both title order and insertion order, with non-monotonic observation counts, so a divergence sort, a reversed sort, and a no-op ordering all move an item.
- [x] 4.9 Assert the item cap retains by identity rather than by arrival, so ordering provably happens before truncation and the cap never becomes a covert ranking.
- [x] 4.7 Pin the item limit, execution budget, evidence cap, and the `_bounded` clamp by value.
- [x] 4.8 Assert `collections_unavailable` for an absent selector, a wrong-profile selector, and a discovered Planning collection whose query refuses; assert `query_unavailable` for an unexpected refusal.
- [x] 4.5 Assert the review module imports no mutating entry point (add/update/triage/append/create/delete/write).

## 5. Command wiring and surface parity

- [x] 5.1 Add the `plan-progress` branch to `commands.op_review_memory`, routing `collection` through the existing `path` argument and `limit` through the existing cap, with no signature change.
- [x] 5.2 Extend the runtime `INVALID_MODE` message to name `plan-progress`.
- [x] 5.3 Assert `review_memory(mode="plan-progress")` returns the mode envelope through the leaf, round-trips over the REST facade, is registered on all three surfaces, and leaves the pinned MCP tool surface unchanged.
- [x] 5.4 Assert `review_memory(mode="attention")` behaviourally surfaces no plan reference, divergence, or Planning-anchored item, and that no attention category, review ref, or fingerprint is produced. Seed a real `unprocessed_source` finding first and assert the queue is non-empty, so the exclusion is observed against actual items rather than an empty queue.
- [x] 5.5 Assert a Planning manifest that does not declare `progress_evidence` is scanned and returns zero items rather than refusing.

## 6. Gates

- [x] 6.1 Run the new test module plus the adjacent planning, records, structured-collections, attention, and relation-queue suites.
- [x] 6.2 Run `uvx ruff check` over every changed file.
- [x] 6.3 Run `openspec validate add-planned-vs-recorded-review --strict`.

## 7. Deferred to the pinned-surface refresh (not in this change)

- [x] 7.1 Document `plan-progress` in the `review_memory` `mode` docstring, regenerate `tests/fixtures/mcp_tool_schemas.json` and `src/exomem/tool_surface_contract.json` via `scripts/dump-tool-schemas.py`, and refresh every registered external connector. This moves the pinned tool surface and belongs to that release-blocking fan-out, not here.
