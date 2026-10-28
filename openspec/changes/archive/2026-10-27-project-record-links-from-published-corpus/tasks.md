# Tasks

## 1. Implementation

- [x] 1.1 Add `semantic_contract.borrow_link_snapshot`: non-blocking lock, no build, closed decline reasons.
- [x] 1.2 Add the `memory_refs` owner lookup that applies pending identity custody and fails closed.
- [x] 1.3 Make `_LinkProjector` borrow first, apply visibility to bare links and memory owners, recheck owners in the stale window, and keep the walk as the fallback.
- [x] 1.4 Fix `normalize_wikilink` step 2 and remove its unreachable twin in the corpus resolver.
- [x] 1.5 Keep the egress spelling walk's directory entries and check the leaf by entry.

## 2. Proof

- [x] 2.1 Tests: hidden stem, title and vault-root twins; memory id twins on both routes; an over-cap vault with a published corpus; a service write resolved on the next query; a stale-window edit; the restricted writer path; a symlinked page reference.
- [x] 2.2 Compare failure names with the branch base over the Records, Planning, memory reference, egress, vault, semantic contract and collection store test modules.

## 3. Delivery

- [x] 3.1 Review the exact change independently, with disclosure, freshness and egress as the main risks; resolve findings. (Three rounds on PR #2159; approved with no blocking findings at 5f36b9bef.)
- [x] 3.2 Measure link-bearing record queries at the MCP boundary on the sandbox vault before and after. (Watcher-on crossover: the link-bearing Records query case ran 146 to 251 ms faster per orientation; every link index span borrowed; other groups showed no change.)
- [x] 3.3 Merge, then synchronize this delta into the canonical specs and archive this change with `openspec archive`. (Merged as ececce3df.)
