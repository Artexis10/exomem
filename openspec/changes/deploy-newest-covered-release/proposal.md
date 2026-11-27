## Why

On 2026-11-26 the Cloud fleet ran v0.201.0 and the platform was deployed at v0.204.0. Releases v0.205.0, v0.206.0 and v0.206.1 each changed the chart or controller build inputs. Each run of `cloud-deploy` targeted only the newest release, stopped at the platform gate and set nothing. Releases ship several times a day and often change these inputs, so the fleet stayed on an old release even though the releases from v0.202.0 to v0.204.0 matched the deployed platform and passed every other gate.

## What Changes

- **A fallback target.** When the newest release fails the platform gate or has an uncrossed barrier, the run still sends that alert as active and exactly as before. It then looks for the newest older release that the fleet lacks and the deployed platform carries. The release must be newer than the fleet's release, and older than the newest release. It must be a published release: a draft or prerelease never counts. It must change no platform build input since the platform's release, and it must have no uncrossed barrier. The run walks the candidates from the newest and stops at the first that qualifies.
- **The existing rollout path.** The run prints the block's code and detail, so the log still names a barrier ID. It then handles the covered release as it handles the newest one. A candidate with no published image is skipped. The run verifies the attestation, and a failed attestation stops the run with `ATTESTATION_UNVERIFIED`: it never skips to an older candidate. The run prints `COVERED_RELEASE <tag>`, sets the image and waits for the canary.
- **No fallback** when the fleet's release is unknown or unreadable, when no platform Deployment exists, when no candidate qualifies, or when the fleet already runs the covered release's image. These runs stop as before.
- **Two stops stay.** When Substrate answers `CELL_IMAGE_SEEN` or `CELL_RELEASE_OLDER` to the covered release, the run ends ok and the platform alert stays active. When git cannot diff a candidate, the run fails with `PLATFORM_DIFF_FAILED`, as it does for the newest release.
- **Alerts.** The platform or barrier alert stays active, and `cloud-release-behind` stays unresolved, because the fleet still lacks the newest release. The next run measures again. The run's final stop comes from the canary wait.
- **A registry failure stops the run visibly.** A network error or an HTTP error other than 404 on an image lookup ends the run with `REGISTRY_LOOKUP_FAILED`, the tag and the error class, and sends `cloud-deploy-failing`. A 404 still means the image is not published. The fallback adds lookups, so a failure there must not end in a traceback with no alert.
- **One list read.** The lag alert and the fallback share the single bounded read of the release list. A run adds no second GitHub list call.
- **Runbook.** The `cloud-release-platform-needed` and `cloud-release-barrier` rows tell the reader that the run may have rolled out the newest release that the platform carries.

## Capabilities

### Modified Capabilities

- `cloud-release-automation`: adds one requirement for the fallback rollout.

## Impact

- **CI:** `infra/scripts/cloud_deploy.py`, run by the `cloud-deploy` workflow.
- **Docs:** `docs/runbooks/cloud-platform-deploy.md` and the header comment of `.github/workflows/cloud-deploy.yml`.
- **Active changes:** the requirements and design D1 to D3 of `automate-cloud-releases`, and one scenario of `alert-cloud-release-lag`, said that the run acts only on the newest release and sets nothing when a gate stops it. They now name the fallback.
- **Not changed:** the gate globs, the barrier file, the deploy token's scope, the `send_alert` contract and the body that the run sends to Substrate (`cellImage` and `release` only).
