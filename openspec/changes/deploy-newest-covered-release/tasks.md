## 1. Covered release rollout

- [x] 1.1 Share one bounded release-list read between the lag alert and the fallback in `infra/scripts/cloud_deploy.py`. Verify with `tests/test_cloud_deploy.py`: a run that sends the lag alert and takes the fallback reads the release list once. This catches a second list call per run.
- [x] 1.2 Add the fallback: after the newest release fails the platform gate or has an uncrossed barrier, walk the published releases between the fleet's release and the newest, newest first, and roll out the first that passes both gates, with its attestation. Verify with `tests/test_cloud_deploy.py`: the run sets that release, waits for the canary, keeps `cloud-release-platform-needed` active and does not resolve `cloud-release-behind`. This catches a fleet that stays on an old release while a newer release matches the platform.
- [x] 1.3 Keep the old stop where no fallback applies. Verify with `tests/test_cloud_deploy.py`: no qualifying candidate, an unknown fleet release, and a fleet already on the covered release's image each set nothing and stop with the original code. This catches a fallback that guesses.
- [x] 1.4 Skip a candidate with an uncrossed barrier or no published image, and never skip a failed attestation. Verify with `tests/test_cloud_deploy.py`. This catches a release set past a one-way change, and an unverified image set for an older release.
- [x] 1.5 Add the sentence to the `cloud-release-platform-needed` row of `docs/runbooks/cloud-platform-deploy.md`. Verify with `test_every_release_and_rollout_alert_has_a_runbook_step`.

## 2. First run

- [ ] 2.1 Observe the first fallback in production. Verify in the `cloud-deploy` Actions log that the run prints `COVERED_RELEASE <tag>`, that the canary reaches the target, and that the platform alert stays active. This catches a fallback that tests on recorded answers cannot show, such as a tag missing from the checkout.
