## Why

On 2026-11-25 the Cloud fleet had run an older release for days. Newer releases, from v0.202.0 on, each needed a platform upgrade that no one owned. The workflow sent `cloud-release-platform-needed` once, and then it sent nothing: the workflow runs only when a release or a platform upgrade dispatches it, so nothing re-checked the fleet until the next release. A cell pinned to an old image is also outside every rollout, and no check noticed it.

## What Changes

- **A daily run.** `cloud-deploy` gains a `schedule` trigger, once a day. A daily run re-reports active alerts and retries a rollout that a missed dispatch left behind. With the fleet current, the run resolves the alerts and ends with `CELL_IMAGE_CURRENT`.
- **`cloud-release-behind`.** Every run reads the newest release's publish time from the GitHub release. The alert is active when the release is more than 48 hours old and the fleet's release is another. The run sends it before the paused, current, platform and barrier stops, so it reports whichever stop ends the run. An unreadable publish time fails the run with `RELEASE_TIME_UNREADABLE`.
- **`cloud-cell-pinned`.** Substrate's rollout view gains `cells.pinned_off_fleet`, the count of running or read-only cells pinned to an image other than the fleet's `cell_image`. The alert is active above 0. A view without the field sends no transition and prints `PINNED_UNKNOWN`.
- **Runbook.** "Respond to a release or rollout alert" gains a row for each new alert.

## Capabilities

### New Capabilities

- `cloud-release-automation`: one requirement for the daily run and the two alerts. The capability is not canonical yet. It lives in the delta of `automate-cloud-releases`, so this change archives after that one.

## Impact

- **Substrate (separate repository):** the rollout view counts the pinned cells, and the alert receiver re-sends an active alert after 23 hours, so that a daily re-report stays one open alert.
- **CI:** the `cloud-deploy` workflow and `infra/scripts/cloud_deploy.py`.
- **Docs:** `docs/runbooks/cloud-platform-deploy.md`.
- **Not changed:** the `send_alert` contract (job, alert, active), the platform gate, the barrier file and the deploy token's scope.
