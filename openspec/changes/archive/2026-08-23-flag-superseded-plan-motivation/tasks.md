## 1. Read-only batch resolution (red first)

- [x] 1.1 Test that `memory_refs.paths_for_ids_read_only` returns every path holding an identity, so a duplicated identity is a tuple of two rather than an `AMBIGUOUS_REFERENCE` whose message counts pages.
- [x] 1.2 Test that an absent identity is an empty tuple and a malformed identity is dropped without raising.
- [x] 1.3 Test that the whole batch costs at most one corpus scan, and no scan at all when the sidecar is current.
- [x] 1.4 Test that the resolver creates no sidecar where none exists and leaves an existing sidecar's bytes unchanged.
- [x] 1.5 Test that an `IN` clause wider than SQLite's variable limit is chunked, and that a vault whose sidecar cannot be opened still resolves from Markdown.
- [x] 1.6 Add `paths_for_ids_read_only` beside `refs_for_paths`: read-only sidecar connection, chunked `IN`, one whole-batch `_scan_pages` fallback, every path per identity, raising nothing.

## 2. Selection and projection (red first)

- [x] 2.1 Test that an active committed item carrying only `motivation` is selected, and that widening relaxes none of lifecycle, status, or commitment.
- [x] 2.2 Test that `motivation_refs` keeps authored order, is bounded at 16, drops non-string and unbounded values, and keeps a malformed-but-bounded string so an invalid reference stays collapsible.
- [x] 2.3 Widen `selects_item` to evidence bindings **or** motivation references, and add `motivation_refs`.
- [x] 2.4 Test that a legacy free-text `motivation` is neither read as references nor refuses the collection, that an undeclared field refuses nothing, and — since no reviewed output distinguishes the two candidate gates — pin `motivation_is_governed` directly on the array, string, and absent forms.
- [x] 2.5 Gate the projection in `_planning_page` on `planning.motivation_is_governed(manifest)`, not on field declaration.

## 3. Counts (red first)

- [x] 3.1 Test that `divergence(entries)` with one positional argument still returns exactly the seven shipped keys.
- [x] 3.2 Test that the four motivation counts are plain integers, never booleans, and hold `refs == resolved + unresolved` and `superseded <= resolved`.
- [x] 3.3 Test that an item citing no knowledge still carries all four counts, reading zero.
- [x] 3.4 Extend `divergence` with an optional second parameter defaulting to absent, and assemble all eleven counts in one pass.

## 4. Supersession review (red first)

- [x] 4.1 Test that a plan citing a page marked `status: superseded` reports the reference, its resolution, and its supersession.
- [x] 4.2 Test that a plan citing a live page reports resolved and not superseded, and that a hand-edited `superseded_by` with an unchanged status is not supersession.
- [x] 4.3 Test that one identity cited by several plans is resolved once and reported identically on each.
- [x] 4.4 Test that no path, title, or successor of a resolved target appears anywhere in the response.
- [x] 4.5 Add `_supersession`: one batch call, release filtering after the uniqueness decision, `find_corpus.CACHE` for status, and absence for every failure.
- [x] 4.6 Assemble the per-item `motivation` entries with the `memory` field name, and add them to the response.

## 5. Bounding (red first)

- [x] 5.1 Test that the motivation budget is spent on retained items only, after ordering and truncation.
- [x] 5.2 Test that an over-budget reference reports `motivation_budget_exhausted`, sets motivation truncation, and still counts as unresolved.
- [x] 5.3 Test that the motivation budget is separate from the execution budget and leaves `budget_exhausted` untouched.
- [x] 5.4 Add the second budget, its counter-derived verdict, and the `motivation_consulted` and `motivation_truncated` response fields.

## 6. Write guards

- [x] 6.1 Assert the reference sidecar's bytes explicitly across two reviews, since the canonical byte census skips registered internal state.
- [x] 6.2 Assert that a review creates no sidecar where none existed.
- [x] 6.3 Grep this module's source for `rebuild_all`, `ReferenceIndex(`, `refresh_paths`, `upsert_after_write`, and `resolve_identifier(`.

## 7. Disclosure — written last, adversarially

- [x] 7.1 Structural: an absent reference, a duplicated identity, a governance-blocked page, and an access-tier-excluded page produce identical entries once each authored reference is set aside, one identical divergence block, and one tally.
- [x] 7.2 Non-vacuity: a released target does resolve, so the four identical entries are not identical because nothing works.
- [x] 7.3 A withheld twin never promotes a duplicated identity into a unique hit.
- [x] 7.4 Equality: two vaults differing only by whether a blocked page exists produce equal responses once `generated_at` is dropped, with a control that flips only the ceiling.
- [x] 7.5 The same equality under the excluded access tier, with its own control.
- [x] 7.6 The budget verdict does not move with hidden state.
- [x] 7.7 A directly-edited malformed stored reference refuses the collection through the existing bounded counter, disclosing nothing.

## 8. Regression and gates

- [x] 8.1 The whole shipped `tests/test_plan_progress_review.py` stays green — it is the regression gate for the `ref`-key, boolean-count, and byte-identical traps.
- [x] 8.2 `tests/test_planning_motivation.py` and the planning, records, structured-collection, and memory-ref suites stay green.
- [x] 8.3 `git diff --exit-code tests/fixtures/mcp_tool_schemas.json src/exomem/tool_surface_contract.json` is clean: no command wiring, no tool-surface movement.
- [x] 8.4 `openspec validate flag-superseded-plan-motivation --strict` and `openspec validate --specs --strict` pass.
- [x] 8.5 `uvx ruff check` is clean on every changed file.

## 9. Closure

- [x] 9.1 Once this change is merged and therefore demonstrably shipped, sync its `planning` delta into `openspec/specs/` and archive it with `openspec archive` in the same delivery, re-running `openspec validate --all --strict` before and after. Left open deliberately: archiving before the merge would claim a shipped state that does not exist yet, and the archive-discipline check treats a fully-ticked active change as debt.
