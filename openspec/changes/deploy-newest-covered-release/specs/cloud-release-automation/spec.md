## ADDED Requirements

### Requirement: The release automation rolls out the newest release the platform carries

When the newest release fails the platform gate or has an uncrossed barrier, the workflow SHALL send that alert as active and print its code and detail. It SHALL then roll out the newest older release that the fleet lacks and the platform carries, and print `COVERED_RELEASE <tag>`. It SHALL NOT resolve `cloud-release-behind`. It SHALL set nothing and stop with the block when no release qualifies, the release list is unreadable, the fleet's release is unknown, or no platform Deployment exists.

#### Scenario: An older release matches the deployed platform

- **WHEN** the newest release changes a platform build input, and an older release newer than the fleet's changes none, has no uncrossed barrier and has a published image
- **THEN** the run sends `cloud-release-platform-needed` as active and prints its code and detail, prints `COVERED_RELEASE` with that release's tag, sets that release's own image and waits for the canary, and it does not resolve `cloud-release-behind`

#### Scenario: No release between the fleet and the newest matches the platform

- **WHEN** every published release between the fleet's release and the newest changes a platform build input, or the fleet already runs the image of the one that qualifies
- **THEN** the run sets nothing and stops with `cloud-release-platform-needed`

#### Scenario: A covered release has an uncrossed barrier

- **WHEN** a candidate has a barrier entry at or below it that the platform Deployment does not list
- **THEN** the run skips that candidate and picks the next older one that qualifies, or sets nothing and stops with the barrier code when none does

#### Scenario: The release list cannot be read

- **WHEN** the one read of the release list fails or answers with no JSON
- **THEN** the run looks for no fallback, sets nothing and stops with the original platform or barrier code

#### Scenario: The fleet's release is unknown

- **WHEN** the rollout view has no readable release for the fleet
- **THEN** the run looks for no fallback, sets nothing and stops with the original platform or barrier code

#### Scenario: The covered release fails its attestation

- **WHEN** the attestation of the picked release does not verify
- **THEN** the run sets nothing and stops with `ATTESTATION_UNVERIFIED`, and it tries no older release

#### Scenario: Substrate says the fleet has passed the covered release

- **WHEN** Substrate answers `CELL_IMAGE_SEEN` or `CELL_RELEASE_OLDER` to the covered release
- **THEN** the run ends ok with that code and sets nothing, and the platform or barrier alert stays active

#### Scenario: Git cannot diff a candidate

- **WHEN** the platform diff for a candidate fails
- **THEN** the run fails with `PLATFORM_DIFF_FAILED` and sets nothing, and it does not skip the candidate
