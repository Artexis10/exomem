## 1. Priority

- [x] 1.1 Mark every dispatched command as foreground in one innermost dispatcher decorator, and remove the activation-only mark.
- [x] 1.2 Mark every mutation-boundary wait and hold as foreground in `VaultMutationCoordinator.hold`, and count the boundary's process-local waiters.
- [x] 1.3 Add `awaiting_bulk()` and take it around the resolver follower wait, the corpus-context flight join, the graph rebuild join and the graph snapshot proof wait.
- [x] 1.4 Rewrite the module rule in `foreground_priority` to state that no foreground thread waits on a parked bulk pass.

## 2. Drain

- [x] 2.1 Leave the whole batch queued on a readiness refusal, and isolate receipts only for other failures.
- [x] 2.2 Return to the drain loop between isolated receipts when a writer waits on the boundary.

## 3. Spans

- [x] 3.1 Add content-free spans for a page creation's commit stages, the post-commit blocks, the default write advisory, and the terminal filter on the mutation path.

## 4. Proof

- [x] 4.1 Prove that a GIL-releasing phase under the boundary, and in a dispatched read and write, keeps its quiet speed beside a real bulk pass, and goes slow with the mark removed.
- [x] 4.2 Prove that a foreground follower of a bulk resolver build does not wait out the progress floor.
- [x] 4.3 Prove that a busy boundary leaves the drain batch queued with no receipt replay, and that replay steps aside for a waiting writer.
- [ ] 4.4 On the live service after release, compare per-phase write durations during and outside whole-vault passes, `MUTATION_BUSY` refusals during passes, and drain hold durations against the values before release.
