# Tasks

## 1. Earlier-application compatibility

- [ ] 1.1 Create the maintenance task worktree from the earlier release and target its PR at `maintenance/0.134`; bind version `0.134.6` after collision checks.
- [ ] 1.2 Add an invented retained-receipt replay regression first; prove that the unchanged earlier application rejects it without repeating a canonical mutation.
- [ ] 1.3 Backport explicit catalogue reading, absent-row handling and exact preparation matching; pass historical replay and old-batch cases without weaker content proofs.
- [ ] 1.4 Insert only nine supported rows into the narrow earlier schema; exclude optional work from selection, recovery attempts and backoff without changing its custody.
- [ ] 1.5 Apply acknowledgement and bounded visibility retirement to eligible ready parents; prove restart after a lost callback, supported recall and required pending behavior.
- [ ] 1.6 Document the distinct compatibility target and deferred-work diagnostics; verify an unsupported required component still fails explicitly.

Source evidence: the unchanged 0.134.5 replay test raises `ValueError` for `context_catalogue`.
The corrected source passes 163 scoped cases, including both existing state-guard negative cases.
Independent exact-content review approves the five product files and three test files with zero findings.
The source remains unmerged, so the task boxes stay open.
The installed candidate journey passes replay, retained custody, restart recall and forward recovery.
Graph adoption remains simulated; official artifact equivalence and operational qualification remain open below.

## 2. Installed journey and delivery

- [ ] 2.1 Build the candidate wheel through maintained packaging; run one installed capable-to-earlier-to-capable journey with invented state through the existing supervisor.
- [ ] 2.2 Verify historical replay, narrow-schema writes, recovery failure, lost-callback restart, retained custody and forward recovery; bind results and process cleanup to both wheels.
- [ ] 2.3 Complete independent review and exact-head full CI; publish through the maintained tag producer with the maintenance GitHub release excluded from latest selection.
- [ ] 2.4 Match the official artifact to the tested candidate or repeat only the changed integration boundary; name the qualified rollback target and retain its receipt.

## 3. Operational follow-through

- [ ] 3.1 Reconcile the ordinary feature-operation packet with the qualified rollback target; verify current authority, identity, admission and undo preconditions before any live action.
- [ ] 3.2 Deliver installed and cleanup evidence after an authorized operation; synchronize and archive this contract only when all required tasks have evidence.
