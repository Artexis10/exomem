## ADDED Requirements

### Requirement: Storage completion reflects the bound claim

The controller SHALL report actual claim UID, resource version and status capacity only after existing identity checks pass.
It SHALL report the satisfied cell generation only when capacity meets the desired size and no resize remains pending.
It SHALL preserve the existing local grown-size floor and SHALL NOT treat pod readiness as capacity proof.

#### Scenario: The desired size changes before expansion finishes

- **WHEN** the pod is Ready but claim capacity remains below the new target
- **THEN** the receipt does not report that generation as satisfied

#### Scenario: Filesystem expansion is pending

- **WHEN** the claim reports sufficient bytes but a resize condition remains active
- **THEN** the receipt keeps the satisfied generation unknown

#### Scenario: The binding conflicts

- **WHEN** the claim, bound volume or namespace fails the existing identity checks
- **THEN** the controller clears the storage receipt without changing the desired size
