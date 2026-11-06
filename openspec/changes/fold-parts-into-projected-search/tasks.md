# Tasks

## 1. Part text in the governed catalog

- [ ] 1.1 Build each catalog row with its Source's part text on the full-text variant only; validate part numbers, size and level, and bind parts in the row digest.
- [ ] 1.2 Store parts in an immutable table of projection store version 3, and read a version 2 store as one without parts.
- [ ] 1.3 Republish a Source on a part write, a move, a recovery and a new extraction; count parts against capacity and log the parts left out.
- [ ] 1.4 Raise the projector version, and reuse each stored vector whose projection text is unchanged during the refresh.

## 2. Projected search

- [ ] 2.1 Index part text as documents of its variant, and fold each item to its best document before the lane cut.
- [ ] 2.2 Carry the matched part through fusion, the lexical guard and the reranker, and emit `matched_part` and `passage` on the hit.
- [ ] 2.3 Prove scope, scope change, move, re-extraction, trash and recovery, refresh and capacity end to end on an enrolled vault.

## 3. Delivery

- [ ] 3.1 Review the exact change independently.
- [ ] 3.2 Merge, sync and archive with `openspec archive`.
