## 1. Red Transaction Tests

- [x] 1.1 Prove explicit deferred completion stages floor → caller writes, returns the exact checkpoint and predecessor, and never replaces the shared checkpoint.
- [x] 1.2 Prove ordinary callers retain floor → caller writes → checkpoint even with `post_commit_fanout=False`, including rollback behavior.
- [x] 1.3 Add a native Windows held-checkpoint test for a deferred media sidecar commit.
- [x] 1.4 Record the focused red failures against the old implementation.

## 2. Canonical And Derived Boundary

- [x] 2.1 Add the narrow deferred-completion option with exact checkpoint output, floor retention, checkpoint omission, and unchanged defaults.
- [x] 2.2 Wire only explicit media call sites; re-enter the canonical coordinator and CAS-publish the exact checkpoint only when floor/predecessor still match before fanout.
- [x] 2.3 Add a deterministic two-writer test proving a newer epoch supersedes a stale media token without regression, false fanout success, or receipt clearing.
- [x] 2.4 Prove all graph-internal and ordinary false-fanout callers retain their epoch behavior.

## 3. Durable Convergence Without Re-Extraction

- [x] 3.1 Add red tests proving receipt admission failure aborts before mutation and CAS-only clearing occurs after checkpoint plus completed or verified exact downstream handoffs, including concurrent revision.
- [x] 3.2 Prove checkpoint/fanout failure completes media once, retains the receipt, and never stores rollback-incomplete.
- [x] 3.3 Prove real full-receipt drain recovers floor-ahead state, converges graph, and CAS-clears only completed work without extraction.
- [x] 3.4 Prove real watcher startup recovers floor-ahead state before rebuild and does not re-extract a committed transcript.
- [x] 3.5 Wire only missing recovery behavior using existing graph, watcher, and deferred-work owners.

## 4. Truthful Legacy Reconciliation

- [x] 4.1 Add pure red tests for valid envelopes, trusted malformed/truncated/oversized/malicious prefixes, and unrelated error text.
- [x] 4.2 Add status tests for validated code/targets, bounded sanitized `jobs[].error` and top-level errors, non-retryability, reconciliation count, unhealthy aggregate, and targeted-safe remediation.
- [x] 4.3 Prove automatic retry-all excludes every ambiguous job.
- [x] 4.4 Add targeted retry tests for matching complete transcript, matching pending sidecar, missing/conflicting provenance, and changed identity.
- [x] 4.5 Implement strict classification and provenance-governed transitions without replaying ambiguous batches or mutating retained workspaces.

Red evidence (2026-08-14): focused parser/status tests fail because no batch-failure classifier or status projection exists; retry-all filters after its capped fetch and starves ordinary eligible work behind ambiguous jobs; targeted retry requeues missing/conflicting/changed provenance and rewrites a completed transcript missing provenance. Aggregate health misses an old ambiguous failure beyond both bounded status projections. Matching complete and matching pending-sidecar reconciliation preserve retained batch workspaces without replay.

## 5. Verification And Delivery

- [x] 5.1 Run strict OpenSpec validation and focused transactional/media/deferred tests on native Windows.
- [x] 5.2 Run focused Ruff, public-artifact validation, and `git diff --check`.
- [x] 5.3 Run the full lean suite and separate baseline native Windows failures from introduced regressions.

Native verification (2026-08-14): strict OpenSpec validation, public-artifact validation,
changed-file Ruff, and `git diff --check` pass. Focused media-job (43), media-processing
(68), deferred-drain (26), deferred-index (10), preserve (17), and graph-recovery (4)
suites pass. Native full collection remains blocked by the same four POSIX-only collection
errors as `origin/main`. Supported Linux verification at `29e5793e` passed all eight lean
Python 3.11/3.13 shards plus the required CI gate. The run also caught and corrected stale
full-index test doubles that returned unverified legacy values; the related index/deferred
regression set then passed 58 tests. One unrelated continuation-prune deadline assertion
failed once under full-suite load, passed as an exact Linux 3.13 reproduction, and passed on
the clean failed-job retry.
