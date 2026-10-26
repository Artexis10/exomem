## ADDED Requirements

### Requirement: Compact status rows name the run's source folder

Each compact row that `adoption_studio(action="status")` returns without a `run_id` SHALL carry `source_root`, the vault-relative folder that the run scanned, as the run recorded it when it started. The row SHALL keep its other fields. A caller that sums a run's applied count by folder SHALL read it from `source_root` and SHALL NOT infer the folder from the run's identifier.

#### Scenario: A listing of runs

- **WHEN** a run scanned `_Imports/<import id>` and the owner lists runs
- **THEN** its row carries `source_root` with that folder, beside its identifier, phase, created time and counts

#### Scenario: Several runs over one import

- **WHEN** two runs scanned folders under one staged import
- **THEN** each row carries its own `source_root`
- **AND** a caller sums `counts.applied` over the rows whose folder is under that import
