## 1. Regression Tests

- [x] 1.1 Replace the SQLite-token veto regression with coverage proving token-only live WAL churn cannot decline a logically current detached publication.
- [x] 1.2 Add a concurrent watcher-delta regression proving a completed detached catalogue rebases changed and deleted paths to the latest exact checkpoints before publication.
- [x] 1.3 Add coverage proving one oversized final suffix catches up off-barrier while incomplete, sustained-oversized, identity-shifted, and source-drifted catch-up preserves the live catalogue and pending repair demand.
- [x] 1.4 Add managed-owner coverage proving startup, writers, watchers, and refused reads cannot start or chain a competing in-place full rebuild.
- [x] 1.5 Add privacy-safe repair progress and stable abort-reason telemetry coverage.

## 2. Convergent Repair

- [x] 2.1 Split authoritative publication proof from disposable SQLite main/WAL/SHM observation.
- [x] 2.2 Rebase a completed replacement off-barrier, keep the final barrier suffix capped, and retry a bounded number of off-barrier catch-ups before persisting and revalidating the exact current checkpoints.
- [x] 2.3 Route every managed full-rebuild request through the existing detached single-flight owner and preserve uncovered demand across an idle handoff.
- [x] 2.4 Promote readiness only from the exact published checkpoint proof and retain later generations for bounded catch-up.
- [x] 2.5 Emit privacy-safe repair phase, duration, and result-reason telemetry.

## 3. Verification and Delivery

- [x] 3.1 Run focused repair/publication tests, lint, privacy validation, and strict OpenSpec validation.
- [ ] 3.2 Run the proportional lean suite and latency/convergence gates.
- [x] 3.3 Obtain independent adversarial review and resolve every attributable finding.
- [ ] 3.4 Synchronize and archive the completed OpenSpec changes, then commit, push, and open the ready pull request.
- [ ] 3.5 Merge through required checks, cut the release, deploy personal and POLLY candidates, and accept readiness plus distinct live retrieval queries.
