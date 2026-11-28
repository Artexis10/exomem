# Tasks

## 0. Before building inheritance

- [ ] 0.1 On a read-only copy of the owner's graph, measure how far each candidate class spreads under transitive inheritance: dependents per classified page, and classes per entity page. Record the numbers in the design. Reopen D1's transitive rule if the spread makes classes meaningless.
- [x] 0.2 Agree the class-policy read exception with the store owner and reconcile the active canonical store read contract.

## 1. Registry and fields

- [ ] 1.1 Add the closed relation behaviour `inherits_sensitivity` and assign it in the core pack to `derived_from`, `evidenced_by`, `cites`, `refines`, `duplicates` and `supersedes`.
- [ ] 1.2 Hold `inherited_sensitivity` as a protocol-owned key in `page_fields.py`. Declare `sensitivity_mask` in the `page-fields` pack as a list pointer field with writer `classify`.
- [ ] 1.3 Extend classify to clear a floor and to set or clear a mask, each with a reason and a revert, owner principal only. A mask names only a class the page holds now.
- [ ] 1.4 Guard removals of own classes at the page and row write boundary when a class policy is active.

## 2. Carry-over at authored writes

- [ ] 2.1 Add carry-over to every authored write. Reuse compiled source closure. Bind only authorized resolutions to the new page or row version.
- [ ] 2.2 Accept `sources` on `add_record` and `edit_record`, within each tool's schema byte ceiling.
- [ ] 2.3 Add the one-line `sources` prompt to read results that release a page with effective classes.
- [ ] 2.4 Teach agents, in the scaffold references and the capture skill, to name in `sources` or `links` every page whose content a write uses.

## 3. Live inheritance

- [ ] 3.1 Add `page_sensitivity` at the next free owner version. Stage affected sets from canonical bindings and publish their deltas with the intent.
- [ ] 3.2 Give episode `about` a graph edge with the behaviour if it has none.
- [ ] 3.3 Index `item_sources` by source ref for current row versions; join row classes at decision time.
- [ ] 3.4 Key each entry of `admission_memo`, membership `_MEMO`, `_SNAPSHOT_MEMO`, `_PATH_MEMO`, the store-mode membership map and the collection view decisions on its page's and scope parents' generations.
- [ ] 3.5 Check current class scopes and exact representation availability before projected ranking or reduction.
- [ ] 3.6 Refuse a class on a page outside the graph with `SENSITIVITY_PAGE_UNTRACKED`; list already-classified pages outside the graph in the release check.
- [ ] 3.7 Report unresolved legacy derivation claims and withhold them from class-policy reads until owner verification.

## 4. Disclosure

- [ ] 4.1 Make membership subjects carry effective classes, `UNRESOLVED` when not current. List every fact released without a path (counts, rollups, aggregates, snippets, titles, link names, suggestions) and decide each for its pages. Decide binaries by their Evidence page.
- [ ] 4.2 Implement indexed pending reads and atomic publication. Prove input generations and durable dependency coverage through supersession and restart.
- [ ] 4.3 Add the `sensitivity` selector: no other selector in the same scope, refused in `exclude`, never removing the residual scope, refused on a vault without the store.
- [ ] 4.4 Casefold path selectors and exclusions.
- [ ] 4.5 Test every registered read operation, MCP resource and prompt with the synthetic canaries. Include a fenced owner before derivation.

## 5. Proof and delivery

- [ ] 5.1 Compare the real hold and restricted first read at the load target. Include unaffected reads, saturated preparation and a stalled intent.
- [ ] 5.2 Extend the twin-world rig with the direct, cited, episode, uncited copy and unresolved-link cases. Run the blind Opus re-attack before merge.
- [ ] 5.3 Run the full suite, the strict OpenSpec gate and the privacy gate. Obtain two independent reviews, merge, and verify the ordinary release.
