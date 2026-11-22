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
- [x] 2.5 Serve a position whole when it fits the block's bound, and open the block with its framing, which names the units as the person's own recorded positions. Section 5 moved the block from the end of the response to the front.

## 3. Proof

- [x] 3.1 Test projection on a seeded vault: a short lead links to a long page whose position unit sits in its last fifth. Test that a hidden linked page contributes nothing, and that the block leaves the turn allowance intact on a real service packet.
- [x] 3.2 Prove recall latency before and after on the recall latency instrument. Evidence, on a production-sized vault copy: the positions stage adds a p50 of 8 ms on turns with no position in reach and 18 ms on turns that serve one, about 1% of recall time. The warm paired wall-time difference is +0.03 s (95% CI −0.005 to 0.10), which is no change.
- [x] 3.3 Run the real-model dev case that exposed the gap, then one blind held-out decision run with analogue and control cases, judged blind by a model of the agent's family, applied identically to both arms. Evidence: 9 unseen cases (6 positive, 3 controls), two arms with 2 repeats each, judged blind against per-case references. Position carried +0.67 (95% CI 0.33 to 0.92); counting either reply +0.42 (0.08 to 0.75); final-turn correctness +0.39 (0.17 to 0.61); no automatic agreement −0.06 (−0.17 to 0), within its bound; no invented facts did not clear its bound (−0.28 to 0), with two isolated slips against none. The design's "Measured" section holds the detail.

## 4. Delivery

- [ ] 4.1 Run the full corpus, independent review, CI, merge and the ordinary release.
- [x] 4.2 After release, declare the vault's existing position categories through a reviewed registry save that the owner confirms. Verify on the installed service that a turn on such a subject serves the block. Evidence: verified on the installed service 2026-11-20, positions state ready on the incident turn.
- [ ] 4.3 Have an independent lane reproduce the corrected behaviour before either incident is closed. The first reproduction failed on the first answer, which led with a default explanation; section 5 revises the framing and the placement, and this task reruns after it ships.
- [ ] 4.4 Synchronize and archive after the nonoptional tasks have code, test and merge evidence. Run `openspec validate --all --strict` before and after.

## 5. First answer states the position

- [x] 5.1 Replace the framing passage so that it asks the agent to open its first answer with the position, attributed and dated, and to state it before any yes or no. Lead the maps response and the hook's rendering with the block, and keep `budget.serialized_chars` correct. Evidence: `tests/test_recorded_positions.py` pins the first field of the response, the served words and the hook's order; the dev replays are in the design's "Read first, framing first" section.
- [x] 5.2 Bound a page's units in total for a caller served only an excerpt of it, drop units on a page whose text bound is 0, count and never name the units that caller cannot read by reference, and decide the page bounds again at terminal egress. Evidence: five tests in `tests/test_recorded_positions.py`. Four fail against the earlier code. The fifth, the count-only recheck, fails against the block without the counted-page recheck. It does not fail on the base, which turned that unit into a reference that terminal egress already decided.
- [ ] 5.3 Run the held-out real-model round for the first answer: fresh cases with a leading yes/no question that presupposes a default cause, one control with no recorded position, and one with a position that the evidence contradicts, judged blind with the same reference view for every arm. Add the reproduced incident conversations to the real-model regression set. Rerun it when the framing passage, the model or the code that reads the answer changes.
- [x] 5.4 Report the block `unavailable` or `partial`, with the cause as its reason, when the request deadline skips or stops task discovery or discovery is unavailable, so an unknown never reads as no position. The installed reproduction of 4.3 found a recall that spent its budget before task discovery and served `empty`.
