## 1. Daily lag alerts

- [x] 1.1 Add a daily `schedule` trigger to `cloud-deploy`, and send `cloud-release-behind` from `infra/scripts/cloud_deploy.py` once the rollout view is read. Count from the oldest release newer than the fleet's, and read the threshold from `CLOUD_DEPLOY_RELEASE_BEHIND_HOURS`, which defaults to 48 and refuses a value that is not a positive whole number. Verify with `tests/test_cloud_deploy.py`: a fleet that lacks several releases, the oldest of them published 49 hours ago and the newest 2 hours ago, sends the alert as active before its stop. This catches an alert measured from the newest release, which never fires.
- [x] 1.2 Cover the unknown and not-behind cases. Verify with `tests/test_cloud_deploy.py`: a fleet that is current, ahead or on a tag that is not a release sends the alert as inactive. An absent fleet release or an unreadable publish time sends nothing, prints a code and lets the run go on. This catches an unknown value read as "not behind".
- [x] 1.3 Send `cloud-cell-pinned`. Verify with `tests/test_cloud_deploy.py`: a count of 1 sends active, 0 sends inactive, and an absent or invalid count sends nothing and prints `PINNED_UNKNOWN`. This catches Substrate before the companion change reading as no pinned cells.
- [x] 1.4 Send `cloud-deploy-failing` from `main`. Verify with `tests/test_cloud_deploy.py`: `ATTESTATION_UNVERIFIED` sends it as active, and a run that ends ok, or on the platform gate, sends it as inactive. This catches a run that goes red for a cause no alert names.
- [x] 1.5 Add a runbook row for each new alert, and the section that finds the pinned cells. Verify with `test_every_release_and_rollout_alert_has_a_runbook_step`, which catches an alert with no step for the person who receives it.

## 2. Companion change and first run

- [x] 2.1 Substrate (separate repository): count the pinned cells in the rollout view, and re-send an active alert after 23 hours. Verify with the repository's tests.
- [x] 2.2 Observe the first scheduled `cloud-deploy` run in production. Verify in the Actions log that the run prints its codes, and that the alert receiver holds the matching alert state. This catches a schedule that GitHub never fires. Observed 2026-11-26: the scheduled run 45864892760 logged `cloud-release-platform-needed`, and the receiver recorded the matching transitions.
