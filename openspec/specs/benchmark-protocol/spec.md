# benchmark-protocol Specification

## Purpose
TBD - created by archiving change amend-epistemic-bench-families. Update Purpose after archive.
## Requirements
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

### Requirement: A pending acknowledgment withholds families, not the repository

A receipted amendment whose founder acknowledgment is still pending SHALL NOT refuse contract identity derivation, amendment chain folding, working-state drift validation, run-manifest construction, or manifest loading. Those paths establish *what the contract is*, and a pending amendment is a fact about the contract rather than a fault in it. The pending state SHALL refuse exactly one thing: the use of the scenario families that amendment introduced, in any comparative run, score, or published claim. The refusal SHALL carry the typed pending-acknowledgment error and name both the amendment sequence and the withheld families.

The families an amendment introduces SHALL be derived, not declared: they are the §1 family-table rows present in the amendment's own contract document and absent from its parent's. A caller that names no family, or only families from the ratified base, is unaffected.

#### Scenario: Unrelated work proceeds while an amendment is pending

- **WHEN** a run manifest is constructed, loaded, or drift-validated while an amendment receipt is pending acknowledgment, and the caller declares no amended family
- **THEN** the chain folds, the identity derives, and the manifest is produced or read normally

#### Scenario: A run declaring an amended family refuses

- **WHEN** a run manifest is constructed, or a claim is read back from a recorded manifest, declaring a family introduced by a pending amendment
- **THEN** it refuses with the typed pending-acknowledgment error naming the amendment sequence and that family, before any artifact is written

#### Scenario: Pending state is recorded rather than hidden

- **WHEN** an identity is derived over a pending amendment
- **THEN** the amendment identity records its acknowledgment status and the families it introduced, so any manifest written against it shows a reader that the run executed under an unacknowledged contract state

### Requirement: A pending receipt derives its amended-document revision

An amendment receipt SHALL be permitted to defer `repository_revision` until acknowledgment. While it is deferred, identity derivation SHALL use the receipt's uniquely reconstructed introduction commit as the amended-document revision, and SHALL apply every digest and ancestry binding to it that a named revision receives. The strict-descendant check between the amended-document revision and the receipt introduction SHALL apply only to a receipt that names its own revision, because a pending receipt's amendment and receipt land in the same commit.

#### Scenario: Pending receipt still binds real repository bytes

- **WHEN** an identity is derived over a receipt whose `repository_revision` is null
- **THEN** the amended contract bytes are read at the receipt's introduction commit and MUST hash to the receipt's `contract_sha256`, and that commit MUST be an ancestor of the run pin and a strict descendant of the previous receipt's introduction
