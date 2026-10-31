## 1. Attribution

- [x] 1.1 Emit `lexical_repair_started` and `lexical_repair_finished` with closed kind, cause, result and stage tokens, and catalog both events.

## 2. Convergence

- [x] 2.1 Drop the registry binding from detached replays and keep their per-path proofs.
- [x] 2.2 Excuse only the census replay's paths against one registry snapshot taken after the walk, and stay strict for a delta that needs a source proof.

## 3. Proof

- [x] 3.1 Add census cases for writes on both sides of the walk, a missed edit beside an observed write, a missed edit after the page's own event and a tainted delta; add a replay case for an unrelated observed write.
- [x] 3.2 Watch the convergence cases fail on the unchanged code and each guard case fail against an over-excusing build.
- [x] 3.3 Measure repair passes by result and stage on a 30,000-note vault before and after the change.
