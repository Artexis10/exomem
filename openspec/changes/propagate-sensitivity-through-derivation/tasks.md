# Tasks

## 0. Before building inheritance

- [ ] 0.1 On a read-only copy of the owner's graph, measure how far each candidate class spreads under transitive inheritance: dependents per classified page, and classes per entity page. Record the numbers in the design. Reopen D1's transitive rule if the spread makes classes meaningless.

## 1. Registry and fields

- [ ] 1.1 Add the closed relation behaviour `inherits_sensitivity` and assign it in the core pack to `derived_from`, `evidenced_by`, `cites`, `refines`, `duplicates` and `supersedes`.
- [ ] 1.2 Hold `inherited_sensitivity` as a protocol-owned key in `page_fields.py`. Declare `sensitivity_mask` in the `page-fields` pack as a list pointer field with writer `classify`.
- [ ] 1.3 Extend classify to clear a floor and to set or clear a mask, each with a reason and a revert.

## 2. Carry-over at authored writes

- [ ] 2.1 Add the pre-plan carry-over step beside the names step and call it from every authored write surface. Resolve refs under the writer's visibility. The plan function refuses a short floor; classify and mechanical rewrites skip the step.
- [ ] 2.2 Accept `sources` on `add_record` and `edit_record`, within each tool's schema byte ceiling.
- [ ] 2.3 Add the one-line `sources` prompt to read results that release a page with effective classes.
- [ ] 2.4 Teach agents, in the scaffold references and the capture skill, to name in `sources` or `links` every page whose content a write uses.

## 3. Live inheritance

- [ ] 3.1 Add the `page_sensitivity` owner object at owner version 6. Mark dependents pending in the transaction that changes classes, floor, mask or derivation edges; recompute them in derivation slices.
- [ ] 3.2 Give episode `about` a graph edge with the behaviour if it has none.
- [ ] 3.3 Index `item_sources` by source ref for current row versions; join row classes at decision time.
- [ ] 3.4 Key `admission_memo`, membership `_MEMO`, `_SNAPSHOT_MEMO`, `_PATH_MEMO`, the store-mode membership map and the collection view decisions on the `page_sensitivity` revision.
- [ ] 3.5 Carry the generation on projected catalog rows; re-check `sensitivity` scopes against effective classes in projected retrieval.
- [ ] 3.6 Refuse a class on a page outside the graph with `SENSITIVITY_PAGE_UNTRACKED`; list already-classified pages outside the graph in the release check.

## 4. Disclosure

- [ ] 4.1 Make membership subjects carry effective classes, `UNRESOLVED` when not current. List every fact released without a path (counts, rollups, aggregates, snippets, titles, link names, suggestions) and decide each for its pages. Decide binaries by their Evidence page.
- [ ] 4.2 Add the two-revision watermark and the per-snapshot pending set; egress answers missing for pending pages under a `sensitivity` policy for non-owner callers.
- [ ] 4.3 Add the `sensitivity` selector: no other selector in the same scope, refused in `exclude`, never removing the residual scope, refused on a vault without the store.
- [ ] 4.4 Casefold path selectors and exclusions.
- [ ] 4.5 Add the behavioural egress test over every registered read operation with the five synthetic canaries.

## 5. Proof and delivery

- [ ] 5.1 Measure a restricted first read and a hub raise before and after, on generated data at the load target.
- [ ] 5.2 Extend the twin-world canary rig with four synthetic canaries (direct, cited, episode `about`, uncited copy). Run a fresh `security-reviewer` re-attack lane on Opus; it must find no leak and no oracle beyond the existence bits the design lists. Merge only after it passes.
- [ ] 5.3 Run the full suite, the strict OpenSpec gate and the privacy gate. Obtain two independent reviews, merge, and verify the ordinary release.
