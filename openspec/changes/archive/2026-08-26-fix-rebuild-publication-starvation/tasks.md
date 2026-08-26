## 1. Regression Tests

- [x] 1.1 Replace the SQLite-token veto regression with coverage proving token-only live WAL churn cannot decline a logically current detached publication.
- [x] 1.2 Add a concurrent watcher-delta regression proving a completed detached catalogue rebases changed and deleted paths to the latest exact checkpoints before publication.
- [x] 1.3 Add coverage proving one oversized final suffix catches up off-barrier while incomplete, sustained-oversized, identity-shifted, and source-drifted catch-up preserves the live catalogue and pending repair demand.
- [x] 1.4 Add managed-owner coverage proving startup, writers, watchers, and refused reads cannot start or chain a competing in-place full rebuild, and a current published idle handoff does not launch a redundant scan.
- [x] 1.5 Add privacy-safe repair progress and stable abort-reason telemetry coverage.
- [x] 1.6 Add a restart regression proving a missed event discovered by the periodic reconcile persists the exact lexical checkpoint after replay, after an independent off-barrier source proof.
- [x] 1.7 Add coverage proving the reconcile source proof runs off-barrier (never while the publication barrier is held), a mixed or superseded walk refuses only the affected scope while sibling scopes bless, a proof invalidated between prepare and barrier refuses then converges on the next batch, a failing proof walk fails closed without losing the batch's rows or scheduling a rebuild, readiness over a tainted bridge never walks, and proof telemetry stays content-free.

## 2. Convergent Repair

- [x] 2.1 Split authoritative publication proof from disposable SQLite main/WAL/SHM observation.
- [x] 2.2 Rebase a completed replacement off-barrier, keep the final barrier suffix capped, and retry a bounded number of off-barrier catch-ups before persisting and revalidating the exact current checkpoints.
- [x] 2.3 Route every managed full-rebuild request through the existing detached single-flight owner, preserve uncovered demand across an idle handoff, and re-prove a successful published handoff before repeating the scan.
- [x] 2.4 Promote readiness only from the exact published checkpoint proof and retain later generations for bounded catch-up.
- [x] 2.5 Emit privacy-safe repair phase, duration, and result-reason telemetry.
- [x] 2.6 Preserve same-policy reconcile map diffs as bridgeable recall history with explicit reconcile provenance so bounded replay remains durable across process restart, persisting a checkpoint only after an independent off-barrier source proof.
- [x] 2.7 Execute the reconcile source proof synchronously at watcher-batch entry with no locks held, validate it under the barrier as an O(1) exact-checkpoint comparison (single-use, per-scope, store-local), refuse tainted scopes on request paths and in the in-process witness map by proof absence, and count proof outcomes in stable content-free telemetry.

## 3. Verification and Delivery

- [x] 3.1 Run focused repair/publication tests, lint, privacy validation, and strict OpenSpec validation.
- [x] 3.2 Run the proportional lean suite and latency/convergence gates.
- [x] 3.3 Obtain independent adversarial review and resolve every attributable finding.
- [x] 3.4 Synchronize and archive the completed OpenSpec changes, then commit, push, and open the ready pull request.
- [x] 3.5 Merge through required checks, cut the release, deploy personal and POLLY candidates, and accept readiness plus distinct live retrieval queries.
