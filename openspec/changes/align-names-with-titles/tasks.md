## 1. Detection

- [x] 1.1 Move the planned-path function into `relocate`, called by relocation and detection, and add the `name_drift` module: the registry scope, the batch resolver and the review finding. Test: the backlog loop renames every drifted page to the path written in the test, and leaves Sources, recaps, an untitled page, a case-only name and a numbered copy as they are.
- [x] 1.2 Add the `name_drift` audit check and the default attention category, anchored on the top index. Test: one item for the vault, carrying the relocate call and its own ref; its fingerprint changes with the drifted set; its ref holds through its own renames; none at zero drift.

## 2. Surfaces

- [x] 2.1 Add `inspect_name_drift`, with the egress, authorization and public-surface entries. Test: it lists drifted pages with ref, title, current and planned name, marks pages with no usable title, and pages by cursor.
- [x] 2.2 Return the `rename` suggestion from a retitling write, with its terminal projector. Test: a retitle returns the call; an unrelated edit returns none.
- [x] 2.3 Teach the scaffold the retitle rule and the batch rule, keeping it generic. Test: `tests/test_scaffold_no_leak.py` passes.

## 3. Wiring after `curation.relocate`

- [x] 3.1 Resolve `review_ref` of this item to the next batch inside `curation.relocate`, and halve a batch too large to seal. Test: the backlog loop renames batch by batch until the item closes, and one compensation restores the last batch byte for byte; an oversized batch seals smaller and names the deferred pages.
- [x] 3.2 Refresh the item when a curation run that renamed pages ends. Test: the backlog loop closes the item with no manual refresh.
- [x] 3.3 Serve the item in the default review listing from its stored due-state row, with the time of its scan. Test: the listing returns the item, its call and the row's time with the scan disabled.
- [x] 3.5 Leave out of an item's batch each page whose move or referrer rewrite the contract refuses, and remember it until it or a referrer changes. Test: the backlog loop closes with the fixture's legacy notes reported as refused, and a repaired page returns; a refused link rewrite leaves its page out until the referrer changes.
- [x] 3.6 Keep navigation pages out of every relocation, and offer no item in a vault without a top index. Test: a relocation that names a typed index keeps its name; a vault without a top index lists the drift and offers no call.
- [x] 3.7 Size an item's batch from its pages and their links to what one sealed plan holds before preparing it, and keep halving as the fallback. Test: an oversized batch seals smaller with each sealed step prepared once; the measurement in design.md decision 9 seals 105 and 65 of 200 pages in one call without halving.
- [ ] 3.4 After release, rename one live batch through the item's call with the operator's yes, and record the counts.
