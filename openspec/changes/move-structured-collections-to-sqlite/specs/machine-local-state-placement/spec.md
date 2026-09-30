## MODIFIED Requirements

### Requirement: Persistent machine-local state lives outside the vault

Every persistent vault-scoped machine-local family SHALL be classified as
`external-state` and live under a per-user, per-vault state root outside the
vault directory. That root SHALL resolve through a single code seam: an
absolute `EXOMEM_STATE_ROOT` environment override, else the platform state
directory. No external-state consumer SHALL compose the root itself. Vault
content such as notes, access policy and durable human-owned artifacts SHALL
remain `vault-canonical`.

The live structured-collection store SHALL be classified `external-canonical`.
It SHALL live under the same state root and resolve through the same seam, so
no file-sync agent sees a live database. Unlike `external-state`, it is
canonical: index maintenance and rebuild SHALL NEVER delete, rebuild or reset
it, state migration SHALL move it only losslessly, and backups and
portability exports SHALL include a consistent snapshot of it. Its in-vault
replica SHALL be a single-file consistent snapshot, published by atomic
rename and never written in place. It SHALL be classified `vault-canonical`
and SHALL NOT count as persistent machine-local state under the vault. The
vault-side collection mode marker beside it SHALL likewise be `vault-canonical`,
and SHALL be the only authority for whether a replica may be adopted.

Batch and held-publication intermediates SHALL be classified separately as
`target-adjacent`: they SHALL remain beside the publication destination for
same-volume atomic rename/link, SHALL exist only during an active publication
or bounded crash recovery, and SHALL NOT be treated as migratable persistent
state. After migration completes, no persistent machine-local state SHALL
remain under a quiescent vault.

#### Scenario: A synced quiescent vault carries no persistent machine-local state

- **WHEN** a vault that completed state migration has no publication in progress
- **THEN** no persistent index store, epoch record, receipt, lock, rebuild directory, projection, review record, live collection store, write-ahead log, or other external-state family exists under the vault for a file-sync agent to hash, hold, or replace

#### Scenario: The collection store survives index rebuild

- **WHEN** an operator rebuilds or resets derived indexes for a vault
- **THEN** the `external-canonical` collection store is untouched

#### Scenario: Atomic publication scratch follows its target

- **WHEN** a batch or held publication stages a destination
- **THEN** its target-adjacent intermediate is created under the destination parent on the same volume
- **AND** it is cleaned after publication or surfaced through bounded crash recovery
- **AND** it is never migrated to the external state root merely because it is reserved

#### Scenario: The state root resolves through one seam

- **WHEN** any external-state or external-canonical consumer derives its persistent path
- **THEN** the path resolves under the root returned by the single resolver
- **AND** setting an absolute `EXOMEM_STATE_ROOT` relocates every such consumer at once
- **AND** a relative `EXOMEM_STATE_ROOT` is rejected

#### Scenario: A pre-existing nested state root never gains admission

- **WHEN** an absolute state-root override resolves to the vault or one of its descendants, even if it already contains a structurally valid complete manifest
- **THEN** readiness and every external-state owner refuse before cache admission or state I/O
- **AND** neither the manifest nor pre-existing destination bytes can waive the outside-vault invariant

#### Scenario: Distinct vaults get distinct roots

- **WHEN** two vaults are served on the same machine
- **THEN** their state roots are distinct directories keyed by stable vault identity

#### Scenario: Placement inventory cannot be self-consistently incomplete

- **WHEN** a persistent operational family is introduced or described by a source constructor
- **THEN** a contract test requires it to have a reserved descriptor and explicit placement
- **AND** the constructor inventory and registry inventory are checked independently
