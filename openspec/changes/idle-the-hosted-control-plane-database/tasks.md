## 1. Worker idle backoff

- [x] 1.1 Add an idle interval to both provisioner settings models beside the existing `poll_seconds`, with a ceiling that permits a value past the endpoint's autosuspend window.
- [x] 1.2 Replace the fixed idle sleep in `production.py` and `volume.py` with an escalation from `poll_seconds` towards that interval on consecutive empty passes, resetting to the floor on any pass that did work.
- [x] 1.3 Red-first tests: consecutive empty passes escalate and cap; a pass that does work resets to the floor; the escalation never exceeds the configured interval.

## 2. Heartbeat cadences

- [x] 2.1 Widen `exomem-reconcile` and `exomem-access-delivery` in the vendored scheduler contract past the autosuspend window, and raise `missedRunAlertAfterSeconds` above the new cadence in the same edit.
- [x] 2.2 Update the pinned scheduler digest and the template's inline threshold assertion so the chart renders, and mirror the schedule into the Substrate copy of the contract so the two do not drift.
- [x] 2.3 Widen `capacityCollector.schedule` and the durability `deletionDispatcher` schedule, updating the durability contract digest.
- [x] 2.4 Render the chart and assert every CronJob's schedule clears the autosuspend window, so a future addition inside it fails a test rather than the bill.

## 3. Provider setting

- [ ] 3.1 Set the endpoint's autosuspend to sixty seconds and record the previous value in the runbook so it can be restored.

## 4. Budget preflight

- [ ] 4.1 Extend `infra/scripts/audit_hosted_database_budget.py` to classify each declared consumer as request-driven or always-on and fail the control when an always-on consumer shares a metered scale-to-zero endpoint.
- [ ] 4.2 Resolve consumers by endpoint identity rather than hostname, so a pooled and a direct name count once.
- [ ] 4.3 Record the always-on floor arithmetic in `docs/runbooks/hosted/database-budget.md` and state that a threshold below it schedules an outage rather than restraining spend.

## 5. Verification

- [x] 5.1 Full provisioner suite, `ruff`, and the Helm contract tests.
- [ ] 5.2 After deploy, confirm the endpoint reaches a suspended state during an idle window rather than only reporting a lower duty cycle.
- [ ] 5.3 Measure active time over a full week and record observed CU-hours against the 182.5 always-on figure and the 100-hour allowance.
