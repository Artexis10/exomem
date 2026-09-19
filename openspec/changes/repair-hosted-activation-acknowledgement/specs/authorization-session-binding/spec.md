## ADDED Requirements

### Requirement: Hosted custody publication is monotonic across delivery channels

Projected refresh and prompt acknowledgement delivery SHALL use one serialized custody publisher and one authoritative external bundle. An older activation or membership generation SHALL NOT overwrite a newer installed generation; equal epochs with conflicting digests SHALL fail closed. Incomparable generations SHALL require authenticated authoritative reconciliation rather than locally composed fields. Pod replacement SHALL reconstruct acknowledged state from external authority.

#### Scenario: Secret projection lags a fast acknowledgement
- **WHEN** the projected Secret still contains the predecessor of an installed acknowledged successor
- **THEN** refresh retains the verified successor until projection catches up
- **AND** it does not restore the predecessor

#### Scenario: Renewal and acknowledgement arrive out of order
- **WHEN** delivery channels expose incomparable activation and membership generations
- **THEN** the publisher obtains one authenticated authoritative bundle and validates its identities and signatures
- **AND** it never combines individual fields from separate generations

#### Scenario: Runtime pod is replaced
- **WHEN** a new pod starts after an acknowledged write or during exact pending-publication recovery
- **THEN** its custody derives from current external authority and it reconciles only the exact pending publication
- **AND** the new pod cannot resurrect stale activation authority
