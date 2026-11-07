# Tasks

## 1. Part text in the governed catalog

- [x] 1.1 Build each catalog row with its Source's part text on the full-text variant only; validate part numbers, size and level, and bind parts in the row digest.
- [x] 1.2 Store parts in an immutable table of projection store version 3, and read a version 2 store as one without parts.
- [x] 1.3 Republish a Source on a part write, a move, a recovery and a new extraction; count parts against capacity and log the parts left out.
- [x] 1.4 Raise the projector version, and reuse each stored vector whose projection text is unchanged during the refresh.

## 2. Projected search

- [x] 2.1 Index part text as documents of its variant, and fold each item to its best document before the lane cut.
- [x] 2.2 Carry the matched part through fusion, the lexical guard and the reranker, and emit `matched_part` and `passage` on the hit.
- [x] 2.3 Prove scope, scope change, move, re-extraction, trash and recovery, refresh and capacity end to end on an enrolled vault.

## 3. Delivery

- [x] 3.1 Review the exact change independently.
- [x] 3.2 Merge, sync and archive with `openspec archive`.

Evidence: PR #2486. Tasks 1.1–2.3 are proven by `tests/test_full_text_sections.py`, `tests/test_governance_projection_store.py` and `tests/test_governance_projection_namespace.py` (82 passed). The independent review approved the head that this closure commit follows, with 0 blocking findings in round 2. This commit syncs the delta and archives the change; the squash merge of the PR delivers it.
