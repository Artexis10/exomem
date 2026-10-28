## ADDED Requirements

### Requirement: Unused release environments are removed only after a verified promotion

The managed upgrade operator SHALL remove an existing release directory only after the staged target is confirmed active. It SHALL keep the serving release, the release that the promotion replaced, every release that the manager's active or transition record names, and every entry under the releases directory that it cannot prove is an unused release that staging created. A release in use SHALL be matched by directory identity, not by path spelling. Retention SHALL never fail an upgrade that already succeeded; the upgrade result SHALL report what was removed, what was kept and why, each failure, and why retention stopped early. A staging that fails or is interrupted SHALL remove the release directory that it created.

#### Scenario: A verified promotion keeps serving, replaced and recorded releases
- **WHEN** a staged target is confirmed active and the releases directory holds the serving release, the replaced release, a release named by the transition record and other releases that staging created
- **THEN** the operator removes only the other releases
- **AND** the serving, replaced and recorded releases remain

#### Scenario: An unreadable manager record removes nothing
- **WHEN** a manager record cannot be read or names no absolute interpreter
- **THEN** the operator removes no release directory
- **AND** the upgrade succeeds with retention reported as skipped

#### Scenario: A removal failure never fails the upgrade
- **WHEN** the operator cannot remove an unused release directory, or an interrupt stops the removal
- **THEN** the operator still reports the upgrade as successful and prints its result
- **AND** the report names the failure or the interrupt and marks the freed bytes as a lower bound

#### Scenario: A failed staging removes its own directory
- **WHEN** staging fails after it created its release directory, including when wheel verification fails
- **THEN** the release directory is removed
- **AND** the existing worker continues serving
