## ADDED Requirements

### Requirement: The release automation reports a lagging fleet daily

The `cloud-deploy` workflow SHALL run once a day on `main`. After it reads the release and the rollout view, and before any later stop, it SHALL send `cloud-release-behind` and `cloud-cell-pinned`. The first SHALL be active when the fleet has lacked a release for longer than `CLOUD_DEPLOY_RELEASE_BEHIND_HOURS` (default 48). The second SHALL be active when the view counts a pinned cell. An unknown value SHALL send nothing. A failing stop without its own alert SHALL send `cloud-deploy-failing`.

#### Scenario: The fleet stays behind several releases

- **WHEN** the newest release is 2 hours old, the fleet lacks four releases, the oldest of them was published longer ago than the threshold, a platform upgrade is still needed, and no older release that the platform carries qualifies
- **THEN** the run sends `cloud-release-behind` as active, and it then stops with `cloud-release-platform-needed`

#### Scenario: The fleet missed only a young release

- **WHEN** the fleet lacks only the newest release, and that release is younger than the threshold
- **THEN** the run sends `cloud-release-behind` as inactive, and it continues with the rollout

#### Scenario: The fleet runs the newest release

- **WHEN** the fleet runs the newest release, however old
- **THEN** the run sends `cloud-release-behind` as inactive, and it ends with `CELL_IMAGE_CURRENT`

#### Scenario: A cell is pinned off the fleet image

- **WHEN** the rollout view's `cells.pinned_off_fleet` is 1
- **THEN** the run sends `cloud-cell-pinned` as active, and the log shows only the count

#### Scenario: A value is unknown

- **WHEN** the view has no valid `cells.pinned_off_fleet`, no fleet release, or the missed release has no readable publish time
- **THEN** the run sends no transition for that alert, and the log shows the matching `UNKNOWN` or `UNREADABLE` code

#### Scenario: A run fails without an alert of its own

- **WHEN** a run ends on `ATTESTATION_UNVERIFIED`, and a later run ends ok
- **THEN** the first run sends `cloud-deploy-failing` as active, and the later run sends it as inactive
