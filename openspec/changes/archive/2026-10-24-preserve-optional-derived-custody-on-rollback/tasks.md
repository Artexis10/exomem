# Tasks

## 1. Earlier-application compatibility

- [x] 1.1 Create the maintenance task worktree from the earlier release and target its PR at `maintenance/0.134`; bind version `0.134.6` after collision checks.
- [x] 1.2 Add an invented retained-receipt replay regression first; prove that the unchanged earlier application rejects it without repeating a canonical mutation.
- [x] 1.3 Backport explicit catalogue reading, absent-row handling and exact preparation matching; pass historical replay and old-batch cases without weaker content proofs.
- [x] 1.4 Insert only nine supported rows into the narrow earlier schema; exclude optional work from selection, recovery attempts and backoff without changing its custody.
- [x] 1.5 Apply acknowledgement and bounded visibility retirement to eligible ready parents; prove restart after a lost callback, supported recall and required pending behavior.
- [x] 1.6 Document the distinct compatibility target and deferred-work diagnostics; verify an unsupported required component still fails explicitly.

Evidence: PR #2008 merged into `maintenance/0.134` as c3bf9893 after an independent exact-content review.
The unchanged 0.134.5 replay test raised `ValueError` for `context_catalogue`; the corrected source passed the scoped cases, including both state-guard negative cases.

## 2. Installed journey and delivery

- [x] 2.1 Build the candidate wheel through maintained packaging; run one installed capable-to-earlier-to-capable journey with invented state through the existing supervisor.
- [x] 2.2 Verify historical replay, narrow-schema writes, recovery failure, lost-callback restart, retained custody and forward recovery; bind results and process cleanup to both wheels.
- [x] 2.3 Complete independent review and exact-head full CI; publish through the maintained tag producer with the maintenance GitHub release excluded from latest selection.
- [x] 2.4 Match the official artifact to the tested candidate or repeat only the changed integration boundary; name the qualified rollback target and retain its receipt.

Evidence: the installed candidate journey passed replay, retained custody, restart recall and forward recovery, with every worker exited and the invented state deleted.
Graph adoption in that journey was simulated.
Exact-head full CI run 39999640224 passed on c2a8911d.
Tag `v0.134.6` points at c2a8911d; the original publisher run 40008411886 succeeded, and the release is not marked latest.
The official wheel, source distribution and five signed images were then qualified against the tested candidate, which names 0.134.6 as the qualified rollback target.

## 3. Operational follow-through

- [x] 3.1 Reconcile the ordinary feature-operation packet with the qualified rollback target; verify current authority, identity, admission and undo preconditions before any live action.
- [x] 3.2 Deliver installed and cleanup evidence after an authorized operation; synchronize and archive this contract only when all required tasks have evidence.

Evidence: the 0.138.0 managed worker promotion on 2026-10-24 reused the qualified 0.134.6 release unchanged as its ready-state undo target.
It closed with installed parity, an independent installed comparison and complete cleanup.
