## ADDED Requirements

### Requirement: Verify enabled repositories in bounded idle windows

The Cloud controller SHALL check a bounded, rotating subset of retained backup packs in an enabled cell's repository. It SHALL default to no enabled cells and permit at most one verifier to run a check across the fleet. It SHALL use a time and transfer-rate limit and a read-only check that leaves managed repository operations free to proceed.

#### Scenario: Verification is enabled for one idle cell

- **WHEN** the cell has a successful backup and no conflicting work owns the repository
- **THEN** a due verification starts independently of the serving cell
- **AND** the job has no access to the vault volume or media credentials

#### Scenario: A managed restore needs the repository

- **WHEN** an already-started verification overlaps a restore
- **THEN** the verification holds no repository lock and delays no managed restore
- **AND** the in-place restore guards recognize the exact read-only verification workload
- **AND** the guards bound each workload lookup while the cell is stopped

#### Scenario: The workload create reply is lost

- **WHEN** the controller cannot prove that a create request failed
- **THEN** the reservation it wrote before submission holds the fleet slot through the possible start and runtime window
- **AND** the reservation survives replacement of the controller or cell StatefulSet
- **AND** it keeps the slot while it observes a nonterminal verifier Pod

### Requirement: Retain the last observed verification and retry the same partition

The Cloud controller SHALL expose the identity, slice, time, and result of the last observed verification separately from the cell's backup and serving status. A matching repository, create-issued Job UID, admitted scratch-only Pod and native container finish evidence SHALL be required to advance the slice. A failed, unavailable, or missing result SHALL keep the slice due after a bounded retry delay.

#### Scenario: A later slice is unreadable

- **WHEN** a backup succeeded and a later check detects an unreadable pack
- **THEN** the verification receipt records a failed attempt and retains the last successful slice
- **AND** the cell retains its backup receipt and can still serve

#### Scenario: The controller restarts after a failed slice

- **WHEN** the verification receipt records the failed slice and the retry delay elapses
- **THEN** the controller checks the same slice and advances only after observing its success
