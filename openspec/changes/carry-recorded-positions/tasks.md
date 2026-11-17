# Tasks

## 1. Trait

- [x] 1.1 Add `position` to the closed kind-trait set and let category definitions in the pack and the vault registry declare `traits`, validated against the closed set, with a named finding for an unknown trait.
- [x] 1.2 Declare `position` on the shipped `preference` category.
- [x] 1.3 Expose the categories and kinds that carry a trait through the registry, so the projection reads them and never names a category.

## 2. Projection and framing

- [x] 2.1 After task leads and their links are served, run one batched semantic-unit query over the served leads and their listed link targets, filtered to position categories and kinds. Rank by relevance to the turn, then by recency.
- [x] 2.2 Serve the `positions` block in maps responses with its state, its framing passage and its items (text, authored date, page title, unit reference). Record its references in the served-context row.
- [x] 2.3 Add the vault budget `positions_max_chars`, pinned to the engineering bounds the hook reads. Overflow becomes references.
- [x] 2.4 Teach the scaffold skill to read the block and to capture a person's position under a category that carries the trait. Refresh the skill contract stamps.
- [x] 2.5 Serve a position whole when it fits the block's bound, end the maps response with the block, open it with its framing, and attribute each unit as its text does.

## 3. Proof

- [x] 3.1 Test projection on a seeded vault: a short lead links to a long page whose position unit sits in its last fifth. Test that a hidden linked page contributes nothing, and that the block leaves the turn allowance intact on a real service packet.
- [ ] 3.2 Prove recall latency before and after on the recall latency instrument.
- [ ] 3.3 Run the real-model dev case that exposed the gap, then one blind held-out decision run with analogue and control cases, judged by a model from another family.

## 4. Delivery

- [ ] 4.1 Run the full corpus, independent review, CI, merge and the ordinary release.
- [ ] 4.2 After release, declare the vault's existing position categories through a reviewed registry save that the owner confirms. Verify on the installed service that a turn on such a subject serves the block.
- [ ] 4.3 Have an independent lane reproduce the corrected behaviour before either incident is closed.
- [ ] 4.4 Synchronize and archive after the nonoptional tasks have code, test and merge evidence. Run `openspec validate --all --strict` before and after.
