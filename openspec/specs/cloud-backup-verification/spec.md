# cloud-backup-verification Specification

## Purpose
Check retained Cloud backup packs for damage on a bounded schedule. Report repository verification independently of backup freshness and cell readiness.

## Requirements

### Requirement: Verify enabled repositories in bounded idle windows

The Cloud controller SHALL check a bounded, rotating subset of retained backup packs in an enabled cell's repository. It SHALL default to no enabled cells and permit at most one verification job across the fleet. It SHALL use a time and transfer-rate limit and a read-only check that leaves managed repository operations free to proceed.

#### Scenario: Verification is enabled for one idle cell

- **WHEN** the cell has a successful backup and no conflicting work owns the repository
- **THEN** a due verification starts independently of the serving cell
- **AND** the job has no access to the vault volume or media credentials

#### Scenario: A managed restore needs the repository

- **WHEN** an already-started verification overlaps a restore
- **THEN** the verification holds no repository lock and delays no managed restore
- **AND** the in-place restore guards recognize the exact read-only verification workload

#### Scenario: The workload create reply is lost

- **WHEN** the controller cannot prove that a create request failed
- **THEN** it holds the fleet slot through the workload's possible start and runtime window
- **AND** it keeps the slot while it observes a nonterminal verifier Pod

### Requirement: Retain the last observed verification and retry the same partition

The Cloud controller SHALL expose the identity, slice, time, and result of the last observed verification separately from the cell's backup and serving status. A matching repository, create-issued Job UID, exact Pod and native container finish evidence SHALL be required to advance the slice. A failed, unavailable, or missing result SHALL keep the slice due after a bounded retry delay.

#### Scenario: A later slice is unreadable

- **WHEN** a backup succeeded and a later check detects an unreadable pack
- **THEN** the verification receipt records a failed attempt and retains the last successful slice
- **AND** the cell retains its backup receipt and can still serve

#### Scenario: The controller restarts after a failed slice

- **WHEN** the verification receipt records the failed slice and the retry delay elapses
- **THEN** the controller checks the same slice and advances only after observing its success
