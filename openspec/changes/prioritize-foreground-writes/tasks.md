## 1. Priority

- [x] 1.1 Mark every dispatched command as foreground in one innermost dispatcher decorator, and remove the activation-only mark.
- [x] 1.2 Mark every mutation-boundary wait and hold as foreground in `VaultMutationCoordinator.hold`, and count the boundary's process-local waiters.
- [x] 1.3 Add `awaiting_bulk()` and take it around the resolver follower wait, the corpus-context flight join, the graph snapshot proof wait and the recall replacement lock.
- [x] 1.4 Add `outside_foreground()` and take it at the graph rebuild join.
- [x] 1.5 Rewrite the module rule in `foreground_priority` to state that no foreground thread waits on a parked bulk pass.

## 2. Drain

- [x] 2.1 Leave the whole batch queued on a readiness refusal, and isolate receipts only for other failures.
- [x] 2.2 Replay at least one receipt per pass, then return to the drain loop between isolated receipts when a writer waits on the boundary.

## 3. Spans

- [x] 3.1 Add content-free spans for a page creation's commit stages, the post-commit blocks, the default write advisory, and the terminal filter on the mutation path.
- [x] 3.2 Record `spans_truncated` on a ledger row that dropped spans, and sum spans past the producer's name bound as `spans.overflow`.

## 4. Proof

- [x] 4.1 Prove that a GIL-releasing phase under the boundary, and in a dispatched read and write, keeps its quiet speed beside a real bulk pass, and goes slow with the mark removed.
- [x] 4.2 Prove that a foreground follower of a bulk resolver build does not wait out the progress floor.
- [x] 4.3 Prove that a rebuild join leaves other requests their priority and still completes.
- [x] 4.4 Prove that a busy boundary leaves the drain batch queued with no receipt replay, that replay steps aside for a waiting writer after one receipt, and that a page that always fails cannot pin a healthy page.
- [x] 4.5 Prove that a ledger row that dropped spans carries `spans_truncated`, and that spans past the name bound reach `spans.overflow`.
- [ ] 4.6 On the live service after release, compare each before and after on the same instrument: per-phase write durations during and outside whole-vault passes, `MUTATION_BUSY` refusals during passes, drain hold durations, whole-vault pass duration, and `GRAPH_SYNC_STABILIZATION_EXHAUSTED` counts under write traffic.
