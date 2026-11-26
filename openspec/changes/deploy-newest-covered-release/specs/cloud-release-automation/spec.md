## ADDED Requirements

### Requirement: The release automation rolls out the newest release the platform carries

When the newest release fails the platform gate or has an uncrossed barrier, the `cloud-deploy` run SHALL send that alert as active. It SHALL then roll out the newest older release that the fleet lacks and the platform carries, and print `COVERED_RELEASE <tag>`. It SHALL NOT resolve `cloud-release-behind`. It SHALL set nothing and stop as before when the fleet's release is unknown, no platform Deployment exists, no release qualifies, or the fleet already runs its image.

#### Scenario: An older release matches the deployed platform

- **WHEN** the newest release changes a platform build input, and an older release newer than the fleet's changes none and has no uncrossed barrier
- **THEN** the run sends `cloud-release-platform-needed` as active, prints `COVERED_RELEASE` with that release's tag, sets its image and waits for the canary, and it does not resolve `cloud-release-behind`

#### Scenario: No release between the fleet and the newest matches the platform

- **WHEN** every published release between the fleet's release and the newest changes a platform build input
- **THEN** the run sets nothing and stops with `cloud-release-platform-needed`

#### Scenario: A covered release has an uncrossed barrier

- **WHEN** a candidate has a barrier entry at or below it that the platform Deployment does not list
- **THEN** the run skips that candidate and picks the next older one that qualifies, or sets nothing and stops with the barrier code when none does

#### Scenario: The fleet's release is unknown

- **WHEN** the rollout view has no readable release for the fleet
- **THEN** the run looks for no fallback, sets nothing and stops with the original platform or barrier code
