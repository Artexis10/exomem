## 1. Idle Reclamation Keeps The Resolver

- [x] 1.1 Add a memory-reclamation entry point that retains the recall resolver,
      distinct from `unload_ram_caches`, and record the measurement that
      justifies it.
- [x] 1.2 Point `model_reaper` and the quiet-mode switch at it.
- [x] 1.3 Leave every `epistemic_graph` call site on `unload_ram_caches`: those
      evict for correctness, not for memory.
- [x] 1.4 Test: idle reclamation clears pages and hot find caches and keeps the
      recall resolver.
- [x] 1.5 Test: `unload_ram_caches` still clears it.
- [x] 1.6 Test: a reader after idle reclamation is served from memory without
      walking the vault.

## 2. Evidence

- [x] 2.1 Record the resolver's retained size and the cold rebuild cost on a
      2,400-page vault in the PR.
- [x] 2.2 Validate the OpenSpec change artifacts.

## Measured out

Hoisting the per-path `root.resolve()` out of `recall_policy.is_recall_candidate`
was in this change and was dropped. It removes 4,802 of 9,604
`_getfinalpathname` calls in an admission pass over 2,400 pages, worth 0.66 s of
a ~45 s cold build -- 1.5%. Buying it means memoizing a resolved path on the
no-follow/reparse boundary, which needs an invalidation story for a root replaced
underneath a running process; a stat-keyed cache costs about as much as the call
it replaces. Not a trade worth making on a security check for 1.5%.
