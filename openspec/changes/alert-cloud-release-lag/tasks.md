## 1. Daily lag alerts

- [x] 1.1 Add a daily `schedule` trigger to `cloud-deploy`, and send `cloud-release-behind` and `cloud-cell-pinned` from `infra/scripts/cloud_deploy.py` before any stop. Verify with `tests/test_cloud_deploy.py`: a fleet behind a release that is 49 hours old sends the alert as active before it stops, and a 2-hour-old release sends it as inactive. This catches a lag alert that only a run past the platform gate sends.
- [x] 1.2 Cover the pinned-cell count. Verify with `tests/test_cloud_deploy.py`: a count of 1 sends active, 0 sends inactive, and an absent or invalid count sends nothing and prints `PINNED_UNKNOWN`. This catches Substrate before the companion change reading as no pinned cells.
- [x] 1.3 Add a runbook row for each new alert. Verify with `test_every_release_and_rollout_alert_has_a_runbook_step`, which catches an alert with no step for the person who receives it.

## 2. Companion change and first run

- [ ] 2.1 Substrate (separate repository): count the pinned cells in the rollout view, and re-send an active alert after 23 hours. Verify with the repository's tests.
- [ ] 2.2 Observe the first scheduled `cloud-deploy` run in production. Verify in the Actions log that the run prints `RELEASE_BEHIND`, `CELLS_PINNED` or neither, and that the alert receiver holds the matching alert state. This catches a schedule that GitHub never fires.
