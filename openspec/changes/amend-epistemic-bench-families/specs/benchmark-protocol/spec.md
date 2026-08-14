# benchmark-protocol — delta

## ADDED Requirements

### Requirement: Amendment receipt contract

The protocol contracts SHALL define `preregistration-amendment-receipt.v1` mirroring the ratification receipt: base contract sha256, amended file sha256, amendment date, reason, and founder acknowledgment (ratifier, acknowledged_on). Receipts live beside `ratification.v1.json` under `benchmarks/epistemic/contracts/`.

#### Scenario: Loader validates the amendment chain

- **WHEN** protocol contracts load and one or more amendment receipts exist
- **THEN** the chain MUST fold cleanly from the pinned ratified base sha to the current file hash, and any missing, out-of-order, or mismatched receipt refuses with a typed error

#### Scenario: Absent amendments preserve current behavior

- **WHEN** no amendment receipts exist
- **THEN** loading behaves exactly as today: the pinned base and ratification receipt shas alone are validated

### Requirement: Run-manifest amendment lineage

The run manifest SHALL record the pre-registration lineage: the ratified base identity (as today) plus, when amendments exist, the ordered list of amendment receipt identities culminating in the effective pre-registration sha the run executed against.

#### Scenario: Lineage is additive and optional

- **WHEN** a manifest is produced for a run with no amendments
- **THEN** the manifest remains valid without the lineage block, and existing consumers are unaffected

#### Scenario: Runs against unreceipted state refuse

- **WHEN** a run is constructed while the working pre-registration matches neither the ratified base nor a receipted chain
- **THEN** manifest construction refuses with a named error rather than recording a fabricated identity
