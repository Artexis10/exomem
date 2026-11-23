## 1. Detection

- [x] 1.1 Add the `name_drift` module: the rule over `resolve_filename_slug`, the registry scope, the collision forms, the batch resolver and the review finding. Test: the audit lists exactly the pages whose planned name differs from their name, with names taken from the creation path, and lists no Source, dated capture, matching page or collision copy.
- [x] 1.2 Add the `name_drift` audit check and the default attention category. Test: one item for the vault, carrying the relocate call and its own ref; its fingerprint changes with the drifted set; none at zero drift.

## 2. Surfaces

- [x] 2.1 Add `inspect_name_drift`, with the egress, authorization and public-surface entries. Test: it lists drifted pages with ref, title, current and planned name, marks pages with no usable title, and pages by cursor.
- [x] 2.2 Return the `rename` suggestion from a retitling write, with its terminal projector. Test: a retitle returns the call; an unrelated edit returns none.
- [x] 2.3 Teach the scaffold the retitle rule and the batch rule, keeping it generic. Test: `tests/test_scaffold_no_leak.py` passes.

## 3. Wiring after `curation.relocate`

- [x] 3.1 Resolve `review_ref` of this item to `next_batch` inside `curation.relocate`, and halve a batch too large to seal. Test: one batch renames to its planned names and one compensation restores it; an oversized batch seals smaller and names the deferred pages.
- [ ] 3.2 After release, rename one live batch through the item's call with the operator's yes, and record the counts.
