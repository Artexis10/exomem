## 1. Capture with declared passages

- [ ] 1.1 Accept `passages` on `capture_source` through the public tool options; verify each bound and the refusal for `passages` with `content`, each before any write.
- [ ] 1.2 Render the body from the passage texts, store `passage_keys` in the Source page, and include keys and texts in the capture input digest; verify reuse on an identical retry and the identity conflict on a changed key.
- [ ] 1.3 Verify that a passage result carries the declared keys in file mode and in store mode.

## 2. Chunks of declared passages

- [ ] 2.1 Chunk a Source that has `passage_keys` by its declared passages, with one function that also gives each chunk its passage ordinal and span; verify a long passage splits with adjacent spans and stays one passage.
- [ ] 2.2 Keep the embedding index's stored structure and the rows of other Sources unchanged; verify that no reindex runs and that `tests/test_audit_contradiction_order.py` passes unchanged.
- [ ] 2.3 Verify that keys give no search signal: equal indexed text, vectors and scores for two Sources that differ only in their keys.

## 3. Passage result level

- [ ] 3.1 Add `result_level: "passage"` to `search_memory` in its own module, on a path that embeds once and selects chunks without the collapse to one chunk for each file; verify two passages of one Source both rank.
- [ ] 3.2 Return reference, source identity, key, ordinal, passage hash, span, score, the recall space and the withheld count, with no passage text; verify the passage hash equals the hash of the declared text.
- [ ] 3.3 Bind each result to the current page; verify that a chunk with stale stored text is withheld and counted.
- [ ] 3.4 Apply eligibility before ranking; verify that a tag filter excludes a better match and that the results still fill from eligible Sources.
- [ ] 3.5 Refuse a ranking option combined with the passage level, and a `limit` above 200; verify each error names what to change.

## 4. Latency

- [ ] 4.1 Build the declared reference and measure the passage path with one caller and with four, on disk scoring and on a resident matrix; record the run files.
- [ ] 4.2 Choose the scoring mode from the measurement, report the mode in the reply, and add the reference and the bars to `request_latency_contract.json`; verify the contract check fails on a missed bar.

## 5. Delivery

- [ ] 5.1 Document declared passages and the passage result level in `docs/source-capture.md`.
- [ ] 5.2 Run `openspec validate --all --strict`, deliver through an independent review, synchronize the deltas into the canonical specs and archive this change in the same delivery.
