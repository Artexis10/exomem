## ADDED Requirements

### Requirement: Verify enabled repositories in bounded idle windows

The Cloud controller SHALL check a bounded, rotating subset of retained backup packs in an enabled cell's repository. It SHALL default to no enabled cells and permit at most one verification job across the fleet. It SHALL use a time and transfer-rate limit, keep the repository lock, and give managed repository operations priority.

#### Scenario: Verification is enabled for one idle cell

- **WHEN** the cell has a successful backup and no conflicting work owns the repository
- **THEN** a due verification starts independently of the serving cell
- **AND** the job has no access to the vault volume or media credentials

#### Scenario: A managed restore needs the repository

- **WHEN** a verification job still owns the cell's repository slot
- **THEN** the controller stops verification and waits until its process has ended before launching the restore

### Requirement: Retain the last observed verification and retry the same partition

The Cloud controller SHALL expose the identity, slice, time, and result of the last observed verification separately from the cell's backup and serving status. Only a positive result for the matching repository and Job SHALL advance the slice. A failed, unavailable, or missing result SHALL keep the slice due after a bounded retry delay.

#### Scenario: A later slice is unreadable

- **WHEN** a backup succeeded and a later check detects an unreadable pack
- **THEN** the verification receipt records a failed attempt and retains the last successful slice
- **AND** the cell retains its backup receipt and can still serve

#### Scenario: The controller restarts after a failed slice

- **WHEN** the verification receipt records the failed slice and the retry delay elapses
- **THEN** the controller checks the same slice and advances only after observing its success
