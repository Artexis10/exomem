## 1. Capture with declared passages

- [ ] 1.1 Accept `passages` on `capture_source` through the public tool options; verify each bound and the refusal for `passages` with `content`, each before any write.
- [ ] 1.2 Render the body from the passage texts, store the keys in a block of the body before the Capture section and the passage count in frontmatter, and include keys and texts in the capture input digest; verify reuse on an identical retry, the identity conflict on a changed key, and that frontmatter does not grow with the passages.
- [ ] 1.3 Verify that a passage result carries the declared keys in file mode and in store mode.

## 2. Chunks of declared passages

- [ ] 2.1 Chunk a Source that has a passage count by its declared passages, with one function that also gives each chunk its passage ordinal and span; verify a long passage splits with adjacent spans and stays one passage.
- [ ] 2.2 Keep the embedding index's stored structure and the rows of other Sources unchanged; verify that no reindex runs and that `tests/test_audit_contradiction_order.py` passes unchanged.
- [ ] 2.3 Leave the key block out of the search body; verify that keys give no search signal: equal indexed text, vectors and scores for two Sources that differ only in their keys, in both storage modes.

## 3. Passage result level

- [ ] 3.1 Add `result_level: "passage"` to `search_memory` in its own module, on a path that embeds once and selects chunks without the collapse to one chunk for each file; verify two passages of one Source both rank.
- [ ] 3.2 Return reference, source identity, key, ordinal, passage hash, span, score, the recall space and the withheld count, with no passage text; verify the passage hash equals the hash of the declared text.
- [ ] 3.3 Bind each result to the current page; verify that a chunk with stale stored text is withheld and counted.
- [ ] 3.4 Apply eligibility before ranking; verify that a tag filter excludes a better match and that the results still fill from eligible Sources.
- [ ] 3.5 Refuse a ranking option combined with the passage level, and a `limit` above 200; verify each error names what to change.
- [ ] 3.6 Borrow a proved publication without building, walking or waiting; derive declaration membership once per published state. Verify the public request reads at most `limit` distinct selected Sources, including filters, relations, terminal filtering and withheld candidates in both storage modes.
- [ ] 3.7 Refuse a passage request at once when model admission is full; verify the busy outcome and its retry hint.
- [ ] 3.8 Require whole-namespace content authority before candidate work; verify refusal before encoding or Source reads, including an internal allow-all admission predicate.
- [ ] 3.9 Verify cold and stale publications return unavailable without Source reads or encoding; prove concurrent borrowing of a current generation, full checkpoint changes, store revision changes without projections, and normal publication afterward.

## 4. Latency

- [x] 4.1 Build the declared reference and qualify disk scoring with one caller and with four; record all attempts and runtime receipts. Design and compare a bounded resident mode only if disk qualification fails.
- [x] 4.2 Choose the scoring mode from the measurement, report the mode in the reply, and add the reference and the bars to `request_latency_contract.json`; verify the contract check fails on a missed bar.
- [x] 4.3 Implement D8's bounded optional resident path after critique; verify numerical admission across held generations, capture/purge/rollover, honest disk fallback and unchanged ordinary APIs.
- [x] 4.4 Compare repaired disk and resident modes on the same frozen reference, instrument and conditions, with pressure/peak-memory observations and a case-level interval.

## 5. Delivery

- [x] 5.1 Document declared passages and the passage result level in `docs/source-capture.md`.
- [ ] 5.2 Run `openspec validate --all --strict`, deliver through an independent review, synchronize the deltas into the canonical specs and archive this change in the same delivery.
