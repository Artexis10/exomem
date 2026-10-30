## ADDED Requirements

### Requirement: The inventory lists stored members of a recorded stage

`adoption_studio(action="start")` SHALL list each stored member of a recorded stage in the candidate inventory, with the path, size and SHA-256 of its import receipt entry and the import form `artifact`, although no staged file is present.

#### Scenario: A stage holds a stored video and notes

- **WHEN** `start` scans a recorded stage whose video is stored and whose notes are on the volume
- **THEN** the inventory has a row for the video with the size and SHA-256 of its import receipt entry
- **AND** it has a row for each note

#### Scenario: A listed member is missing

- **WHEN** `start` scans a recorded stage with a listed member that is neither on the volume nor stored
- **THEN** the inventory keeps the member as ineligible with a machine-readable reason

### Requirement: The adoption ceiling applies only to files filed by copy

The adoption ceiling for a staged artifact SHALL apply only to a file that adoption copies, and SHALL NOT apply to a stored member that adoption files by reference.

#### Scenario: A stored video above the ceiling is eligible

- **WHEN** a stored staged video is 6 GiB and the adoption ceiling is 200 MiB
- **THEN** the video is an eligible inventory row

#### Scenario: A local member above the ceiling stays in the residual

- **WHEN** a store outage left a 500 MiB staged member on the volume and the adoption ceiling is 200 MiB
- **THEN** the member stays in the residual with the code that names the ceiling
- **AND** it becomes eligible after stage offload stores it

### Requirement: The residual reports stored members as store bytes

`adoption_studio(action="finish")` SHALL report each stored member that adoption did not file under the residual's store bytes, apart from the residual's volume bytes.

#### Scenario: Unselected stored members remain

- **WHEN** `finish` runs while two unselected stored members of 3 GiB in total and notes of 1 MiB remain in the stage
- **THEN** the residual reports 3 GiB as store bytes and 1 MiB as volume bytes
- **AND** it names the discard route, which releases the stored members
