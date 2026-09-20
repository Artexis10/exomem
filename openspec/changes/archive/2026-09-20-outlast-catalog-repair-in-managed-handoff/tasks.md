## 1. Repair

- [x] 1.1 Reproduce the outage as failing tests: a replacement that is alive, on the right release and not ready until after the cutover budget must complete the upgrade, on the cold path, on the promoted-standby path and under `--resume`.
- [x] 1.2 Add `cold_start_window()` as the single source of the post-stop readiness window, with an injectable floor so a test can shrink it without shortening production, and read `Supervisor.start()`'s budget through it.
- [x] 1.3 End the cutover budget at the offline migrator and await the replacement under the cold-start window on every path that reaches it.
- [x] 1.4 Keep the fast failures fast: a replacement that exits before readiness, and one answering with a different release, still fail immediately.
- [x] 1.5 Keep the terminal outcome of a replacement that never reports ready — record retained, `recovery-required`, ingress unavailable, owned process stopped, accepted promotion preserved — and prove it now arrives at the cold-start budget.
- [x] 1.6 Record `ready_after_ms` on the handoff, and verify `--status` already shows a transition in flight rather than adding a surface.

## 2. Operator tooling

- [x] 2.1 Extend the transition polling deadline to cover warm, cutover and cold start, derived from the same budget functions.
- [x] 2.2 Confirm `scripts/upgrade.sh`'s managed branch imposes no deadline of its own, so no change is needed there.

## 3. Admission

- [x] 3.1 Establish which answer a paused request receives when it outlasts the admission budget, and keep pausing if it is already bounded and explicit.
- [x] 3.2 Prove admission during the longer wait: a request issued while the supervisor awaits the replacement is answered as undispatched within the queue budget.

## 4. Verification

- [x] 4.1 Scoped suites green: `tests/test_service_manager.py`, `tests/test_standby_promotion.py`, `tests/test_service_upgrade.py`, plus `tests/test_service_ingress.py`, `tests/test_managed_service_e2e.py`, `tests/test_service_installers.py` and `tests/test_corpus_context_flight_join.py`, which import the changed modules.
- [x] 4.2 No existing assertion weakened: every test that pinned the old numbers still passes unchanged.
- [x] 4.3 Documentation updated: budgets table, standby sequence, polling, recovery, migration record, admission budgets.

Author-independent review of the widened window, and the full corpus run, belong
to the delivery boundary of the batch this change lands in, not to the change
itself; integration owns both.
