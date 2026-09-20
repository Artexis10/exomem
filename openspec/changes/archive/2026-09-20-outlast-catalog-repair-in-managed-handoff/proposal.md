## Why

On 2026-09-20 an operator re-staged the serving release and the managed service was unavailable for about eight minutes. The standby warmed for roughly 75 s, a client write landed in that window, and promotion therefore reported `snapshot: advanced`. The promoted worker logged `managed retrieval catalog delegated to background repair`, so `/health/ready` answered `not_ready` with `retrieval_unavailable` while a repair that takes 80–220 s on that vault ran. The supervisor was still bounding the wait for readiness with the 40-second cutover budget, so it declared failure, stopped a worker that already owned state and was healthy, and entered `recovery-required` with ingress unavailable.

`--resume` then failed identically. Its no-standby branch starts the replacement inside the cutover budget too; the worker boots in about two seconds, the repair needs minutes, and every attempt killed the repair and restarted it from zero. `Supervisor.start()` refuses to launch anything while a transition record is pending, so a unit restart could not recover either. The operator was left with a service that could not be brought back by any supported action.

The cutover budget is the right bound for everything up to the stop, because until the old worker is signalled the supervisor can still abandon the upgrade and hand admission back to a worker that is serving. Once that worker is gone there is nothing to abandon to. Refusing early there does not preserve availability, it ends it: it converts "unavailable for another minute" into "unavailable until a person resumes", and the resume waits on the same replacement.

PR #1332 fixed this defect class for `Supervisor.start()` only, introducing `EXOMEM_COLD_START_SECONDS` with a 120-second floor. The handoff paths kept the old bound.

## What Changes

- The cutover budget ends at the offline migrator. Everything before it — pause, drain, record, detach streams, stop the old worker and prove its descendants exited, migrate — is unchanged and still bounded at 40 s.
- The wait for the replacement to report ready is bounded by the cold-start window instead: the promoted standby, and the cold one-worker start, in a fresh upgrade and in `--resume` alike. One window, `cold_start_window()`, sizes every wait that begins after the previous worker has stopped, including `Supervisor.start()`'s own.
- Fast failures stay fast. A replacement that exits before readiness still fails at once with `candidate worker exited before readiness`, and one answering with a different release still fails at once. Only a live candidate on the right release is waited out.
- The terminal behaviour of a replacement that never becomes ready is unchanged — record retained, `recovery-required`, ingress unavailable, owned process stopped, an accepted promotion preserved in the handoff — and now arrives at the end of the cold-start window rather than the cutover budget.
- Admission is unchanged: ingress stays paused, and a request that outlasts its own 45-second queue budget already receives a bounded, explicit, undispatched refusal rather than being queued without a bound.
- The operator client's polling deadline covers the warm, the cutover and the cold-start budgets, computed from the same two budget functions, so `scripts/upgrade.sh` no longer reports failure for a handoff that is still going to succeed.
- The handoff record gains `ready_after_ms`: the stop-to-readiness window, the part of `unavailable_ms` the replacement itself cost.

## Capabilities

### Modified Capabilities

- `managed-service-upgrades`: a replacement is awaited under the cold-start budget once the worker it replaces has been stopped.

## Impact

- Affected code: `src/exomem/service_manager.py` (`cold_start_window`, `Supervisor.__init__`, `Supervisor._replacement_budget`, `Supervisor.start`, `Supervisor.upgrade`), `src/exomem/service_upgrade.py` (`_transition_budget`). `WorkerRuntime.promote_standby` and `WorkerRuntime.start` are unchanged: both already honour the budget they are handed.
- Affected tests: `tests/test_service_manager.py` gains the slow-replacement cases for a cold upgrade, a promoted standby, a resume, a replacement that never reports ready, a replacement that exits, admission during the wait, the operator deadline and `ready_after_ms`.
- Affected docs: `docs/managed-service-upgrades.md` — budgets table, standby sequence, polling, recovery, migration record, admission budgets.
- Unavailability: an upgrade whose replacement is slow is now unavailable for as long as the replacement's warm takes, up to the cold-start budget, instead of failing into an unavailability that only an operator could end. Both are outages; the second one has no upper bound.
- `scripts/upgrade.sh` needs no change: its managed branch imposes no deadline of its own, so `_transition_budget()` is the only bound on an operator's wait.
