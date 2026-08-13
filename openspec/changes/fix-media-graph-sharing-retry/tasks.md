## 1. Red Transaction Tests

- [ ] 1.1 Prove explicit deferred completion stages floor → caller writes, returns the exact checkpoint, and never replaces the shared checkpoint.
- [ ] 1.2 Prove ordinary callers retain floor → caller writes → checkpoint even with `post_commit_fanout=False`, including rollback behavior.
- [ ] 1.3 Add a native Windows held-checkpoint test for a deferred media sidecar commit.
- [ ] 1.4 Record the focused red failures against the old implementation.

## 2. Canonical And Derived Boundary

- [ ] 2.1 Add the narrow deferred-completion option with exact checkpoint output, floor retention, checkpoint omission, and unchanged defaults.
- [ ] 2.2 Wire only explicit media call sites and publish the exact checkpoint after the mutation guard before fanout.
- [ ] 2.3 Prove all graph-internal and ordinary false-fanout callers retain their epoch behavior.

## 3. Durable Convergence Without Re-Extraction

- [ ] 3.1 Add red tests for write-ahead full-receipt admission and CAS-only clearing after checkpoint plus graph/index success, including concurrent revision.
- [ ] 3.2 Prove checkpoint/fanout failure completes media once, retains the receipt, and never stores rollback-incomplete.
- [ ] 3.3 Prove real full-receipt drain recovers floor-ahead state, converges graph, and CAS-clears only completed work without extraction.
- [ ] 3.4 Prove real watcher startup recovers floor-ahead state before rebuild and does not re-extract a committed transcript.
- [ ] 3.5 Wire only missing recovery behavior using existing graph, watcher, and deferred-work owners.

## 4. Truthful Legacy Reconciliation

- [ ] 4.1 Add pure red tests for valid envelopes, trusted malformed/truncated prefixes, and unrelated error text.
- [ ] 4.2 Add status tests for validated code/targets, non-retryability, reconciliation count, unhealthy aggregate, and targeted-safe remediation.
- [ ] 4.3 Prove automatic retry-all excludes every ambiguous job.
- [ ] 4.4 Add targeted retry tests for matching complete transcript, matching pending sidecar, missing/conflicting provenance, and changed identity.
- [ ] 4.5 Implement strict classification and provenance-governed transitions without replaying ambiguous batches or mutating retained workspaces.

## 5. Verification And Delivery

- [ ] 5.1 Run strict OpenSpec validation and focused transactional/media/deferred tests on native Windows.
- [ ] 5.2 Run focused Ruff, public-artifact validation, and `git diff --check`.
- [ ] 5.3 Run the full lean suite and separate baseline native Windows failures from introduced regressions.
