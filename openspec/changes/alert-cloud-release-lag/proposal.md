## Why

On 2026-11-25 the Cloud fleet had run an older release for days. Each release from v0.202.0 on needed a platform upgrade that no one owned. Each release's run reached the platform gate and sent `cloud-release-platform-needed` as active. The alert receiver emailed only when an alert changed state, so one email went out and then nothing, while the alert stayed active. A companion Substrate change now re-sends an active alert whose last email is more than 23 hours old.

Three gaps remain after that fix:

- No run happens on a day without a release, so nothing re-reports an active alert and nothing retries a rollout that a missed dispatch left behind.
- No alert says that the fleet has missed a release for days, whatever the cause.
- A cell pinned to an old image is outside every rollout, and a run that fails for a cause with no alert of its own, such as an unverified attestation, goes unreported.

## What Changes

- **A daily run.** `cloud-deploy` gains a `schedule` trigger, once a day. A daily run re-reports active alerts and retries a rollout that a missed dispatch left behind. With the fleet current, the run resolves the alerts and ends with `CELL_IMAGE_CURRENT`.
- **`cloud-release-behind`.** The alert is active when the fleet has lacked a release for longer than `CLOUD_DEPLOY_RELEASE_BEHIND_HOURS`, a repository variable that defaults to 48 hours. A run counts from the publish time of the oldest release newer than the fleet's, because releases come every few hours and the newest one is always young. One bounded read of the release list gives that release. A fleet on the newest release, ahead of it, or on a tag that is not a release is not behind. An unknown fleet release or an unreadable publish time sends no transition and prints a code. The run sends the alert as soon as it has read the rollout view, so the paused, current, platform and barrier stops all report it.
- **`cloud-cell-pinned`.** Substrate's rollout view gains `cells.pinned_off_fleet`, the count of running or read-only cells pinned to an image other than the fleet's `cell_image`. The alert is active above 0. A view without a valid count sends no transition and prints `PINNED_UNKNOWN`.
- **`cloud-deploy-failing`.** A run that ends on a failing stop sends this alert as active, except for the stops that have an alert of their own (the platform gate, the barrier and a paused rollout, which cellctl reports as `cloud-rollout-paused`). A run that ends otherwise sends it as inactive.
- **Runbook.** "Respond to a release or rollout alert" gains a row for each new alert, and a new section lists the pinned cells.

## Capabilities

### New Capabilities

- `cloud-release-automation`: one requirement for the daily run and the three alerts. The capability is not canonical yet. It lives in the delta of `automate-cloud-releases`, so this change archives after that one.

## Impact

- **Substrate (separate repository):** the rollout view counts the pinned cells, and the alert receiver re-sends an active alert after 23 hours.
- **CI:** the `cloud-deploy` workflow, `infra/scripts/cloud_deploy.py` and the optional `cloud-deploy` variable `CLOUD_DEPLOY_RELEASE_BEHIND_HOURS`.
- **Docs:** `docs/runbooks/cloud-platform-deploy.md`.
- **Not changed:** the `send_alert` contract (job, alert, active), the platform gate, the barrier file and the deploy token's scope.
