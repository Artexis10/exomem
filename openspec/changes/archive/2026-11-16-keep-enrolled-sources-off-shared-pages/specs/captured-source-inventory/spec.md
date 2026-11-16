## ADDED Requirements

### Requirement: Enrolled Sources stay off shared generated pages

The system SHALL NOT add a line for a Source captured under erasure custody to the top index, the activity log or the Sources index.
The captured-Source inventory SHALL remain the listing for those Sources.
Sources outside erasure custody SHALL keep their listing on those pages.
Source counts on those pages MAY include enrolled Sources, because a count carries no Source text.
A vault with no recorded shared-page custody SHALL write those pages without generated custody.
A vault where an earlier build recorded ownership of enrolled lines SHALL keep carrying that ownership through trims, copies and log rotation until erasure retires it.

#### Scenario: A service caller captures an enrolled Source

- **WHEN** a service caller captures a Source under an issued reservation or an erasure-enabled identity system
- **THEN** the top index, the activity log and the Sources index keep their bytes
- **AND** the captured-Source inventory lists the Source

#### Scenario: An agent saves a Source outside custody

- **WHEN** an agent saves a Source that no erasure custody covers
- **THEN** the shared pages list the Source as they did before this change

#### Scenario: An unenrolled write follows enrolled captures

- **WHEN** a vault holds enrolled Sources and no recorded shared-page custody
- **AND** an unenrolled write changes the shared pages
- **THEN** the write records no generated custody for those pages

#### Scenario: An earlier build recorded enrolled lines

- **WHEN** a shared page holds an enrolled line whose ownership an earlier build recorded
- **AND** a later write trims, copies or rotates that line
- **THEN** the line keeps its recorded ownership in every resulting version
