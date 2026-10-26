## ADDED Requirements

### Requirement: Compact status rows name the run's source folder

Each compact row that `adoption_studio(action="status")` returns without a `run_id` SHALL carry `source_root`, the vault-relative folder that the run scanned, as the run recorded it when it started. The row SHALL keep its other fields. A caller that groups runs by folder SHALL read `source_root` and SHALL NOT infer the folder from the run's identifier. Filed-file counts SHALL come from the scoped aggregate, because run totals can overlap.

#### Scenario: A listing of runs

- **WHEN** a run scanned `_Imports/<import id>` and the owner lists runs
- **THEN** its row carries `source_root` with that folder, beside its identifier, phase, created time and counts

#### Scenario: Several runs over one import

- **WHEN** two runs scanned folders under one staged import
- **THEN** each row carries its own `source_root`
- **AND** a caller uses the scoped aggregate to count filed originals, because per-run totals can overlap

### Requirement: Scoped status counts each filed original once

With `action="status"`, no `run_id` and a subtree in `path`, Adoption Studio SHALL return `adopted_files` for that subtree. It SHALL count distinct original paths with successful filing outcomes across all matching runs. Repeated and overlapping runs SHALL count an original once. Compact run rows SHALL keep their existing per-run counts. If matching run state cannot be read completely, the aggregate SHALL be unknown and marked incomplete. Older callers that omit `path` SHALL keep their existing listing behavior.

#### Scenario: A second run files the same original

- **WHEN** one run applied an original and another recorded it as already applied
- **THEN** the scoped adopted count includes that original once

#### Scenario: Parent and child runs overlap

- **WHEN** runs over a staged folder and its child both record the same original
- **THEN** the scoped adopted count includes that original once

#### Scenario: A matching run cannot be read

- **WHEN** the scoped status cannot read all matching run state
- **THEN** it marks the aggregate incomplete and reports no numeric adopted count
