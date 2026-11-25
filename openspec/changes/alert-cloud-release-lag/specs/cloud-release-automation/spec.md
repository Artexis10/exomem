## ADDED Requirements

### Requirement: The release automation reports a lagging fleet daily

The `cloud-deploy` workflow SHALL run once a day on `main`, and each run SHALL send two alerts before any stop ends it. `cloud-release-behind` SHALL be active when the newest release is more than 48 hours old and the fleet's release is another, and inactive otherwise. `cloud-cell-pinned` SHALL be active when the rollout view counts a cell pinned off the fleet, and inactive at zero. An absent or invalid count SHALL send nothing. An unreadable publish time SHALL fail the run with a visible code.

#### Scenario: The fleet stays behind an old release

- **WHEN** the newest release is 49 hours old, the fleet runs an earlier release, and a platform upgrade is still needed
- **THEN** the run sends `cloud-release-behind` as active, and it then stops with `cloud-release-platform-needed`

#### Scenario: A release is new

- **WHEN** the newest release is 2 hours old and the fleet runs an earlier release
- **THEN** the run sends `cloud-release-behind` as inactive, and it continues with the rollout

#### Scenario: The fleet runs the newest release

- **WHEN** the fleet's release is the newest release, however old
- **THEN** the run sends `cloud-release-behind` as inactive, and it ends with `CELL_IMAGE_CURRENT`

#### Scenario: A cell is pinned off the fleet image

- **WHEN** the rollout view's `cells.pinned_off_fleet` is 1
- **THEN** the run sends `cloud-cell-pinned` as active, and the log shows only the count

#### Scenario: The pin count is unknown

- **WHEN** the rollout view has no `cells.pinned_off_fleet`, or its value is not a non-negative integer
- **THEN** the run sends no `cloud-cell-pinned` transition, and the log shows `PINNED_UNKNOWN`
