## Why

On 2026-11-26 the Cloud fleet ran v0.201.0 and the platform was deployed at v0.204.0. Releases v0.205.0, v0.206.0 and v0.206.1 each changed the chart or controller build inputs. Each run of `cloud-deploy` targeted only the newest release, stopped at the platform gate and set nothing. Releases ship several times a day and often change these inputs, so the fleet stayed on an old release even though the releases from v0.201.1 to v0.204.0 matched the deployed platform and passed every other gate.

## What Changes

- **A fallback target.** When the newest release fails the platform gate or has an uncrossed barrier, the run still sends that alert as active and exactly as before. It then looks for the newest older release that the fleet lacks and the deployed platform carries. The release must be newer than the fleet's release, and older than the newest release. It must be a published release: a draft or prerelease never counts. It must change no platform build input since the platform's release, and it must have no uncrossed barrier. The run walks the candidates from the newest and stops at the first that qualifies.
- **The existing rollout path.** The run then handles the covered release as it handles the newest one. A candidate with no published image is skipped. The run verifies the attestation, and a failed attestation stops the run with `ATTESTATION_UNVERIFIED`: it never skips to an older candidate. The run prints `COVERED_RELEASE <tag>`, sets the image and waits for the canary.
- **No fallback** when the fleet's release is unknown or unreadable, when no platform Deployment exists, when no candidate qualifies, or when the fleet already runs the covered release's image. These runs stop as before.
- **Alerts.** The platform or barrier alert stays active, and `cloud-release-behind` stays unresolved, because the fleet still lacks the newest release. The next run measures again. The run's final stop comes from the canary wait.
- **One list read.** The lag alert and the fallback share the single bounded read of the release list. A run adds no second GitHub list call.
- **Runbook.** The `cloud-release-platform-needed` row tells the reader that the run may have rolled out the newest release that the platform carries.

## Capabilities

### New Capabilities

- `cloud-release-automation`: one requirement for the fallback rollout. The capability is not canonical yet. It lives in the delta of `automate-cloud-releases`, so this change archives after that one and after `alert-cloud-release-lag`.

## Impact

- **CI:** `infra/scripts/cloud_deploy.py`, run by the `cloud-deploy` workflow.
- **Docs:** `docs/runbooks/cloud-platform-deploy.md`.
- **Not changed:** the gate globs, the barrier file, the deploy token's scope, the `send_alert` contract and the body that the run sends to Substrate (`cellImage` and `release` only).
