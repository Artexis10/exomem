## ADDED Requirements

### Requirement: Cell backups survive a compromised credential

Cloud cell backups SHALL live in a bucket with Object Lock in compliance mode and a default retention period. No running service and no single key SHALL be able to remove a backup version before its retention ends. A delete by a backup credential SHALL hide a version, not remove it.

The platform SHALL keep every current version's lock at least 23 days in the future, so that a version hidden at any time remains for at least 23 days. The job that extends locks SHALL run outside the controller and outside every namespace the controller can write. It SHALL hold no capability that shortens or removes a lock, and an alert SHALL fire when a run is missed. It SHALL refuse to set a retention date more than the retention period plus one day ahead, because a compliance-mode extension cannot be undone. It SHALL check its clock against the object store's time before each run, and refuse when they disagree.

A lifecycle rule SHALL remove only hidden versions whose lock has expired, and never a current version.

Import and export staging objects SHALL NOT be stored in the locked bucket.

A procedure SHALL restore a cell's backup repository from its hidden versions. A rehearsal SHALL prove it.

#### Scenario: The controller deletes every backup

- **WHEN** an identity holding everything the controller holds deletes every object under every cell's backup prefix
- **THEN** every version remains for at least 23 days, including packs written long before, and the recovery procedure restores each repository

#### Scenario: An old pack is hidden

- **WHEN** a restic pack written 60 days ago is hidden today
- **THEN** its version stays locked for at least 23 more days

#### Scenario: The extender computes a wrong date

- **WHEN** the extender computes a retention date more than the retention period plus one day ahead
- **THEN** it applies no extension and raises an alert

#### Scenario: The extender's clock runs fast

- **WHEN** the extender's local clock is a year ahead of the object store's time
- **THEN** it applies no extension and raises an alert

#### Scenario: Restic prunes

- **WHEN** a cell's backup runs `forget --prune`
- **THEN** it succeeds, the pruned packs become hidden versions, and a lifecycle rule removes them after their retention ends

#### Scenario: An export is deleted

- **WHEN** a tenant deletes a completed export
- **THEN** its object is removed without waiting for a backup retention period
