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

`collections-store-v1` SHALL be an optional compatibility descriptor, not a
physical migratable state family. Complete manifests SHALL contain exactly
the recorded physical families, each complete; readiness SHALL additionally
require support for every recorded optional ID. Only a closed recognized
catalog MAY be subtracted from the physical set, and unknown IDs SHALL refuse.
Family migration and external adoption SHALL preserve optional IDs. Fresh
file-only manifests SHALL not acquire them merely because a release recognizes
them. Candidate probes SHALL verify optional runtime support separately from
required families. Optional support SHALL remain privately bound to the exact
verified candidate; managed target requests, replies and retained transition
targets SHALL keep the legacy `{python, version, state_descriptors}` shape.
Missing, mismatched or failed verification SHALL not supply optional support.
An incompatible candidate SHALL refuse before stopping the
serving worker, with fresh admission rechecked at quiesced cutover. Candidate
rejection cleanup SHALL restore admission before awaiting cleanup outside the
stop/migration timeout, without stopping the serving worker; compatible
unchanged-family upgrades SHALL not run a needless migration. Parser recognition
alone SHALL not claim mixed/store runtime support. A fixed-ID internal enrollment
seam SHALL require the trusted create/adoption authority and durable publication
before marker cutover, without itself migrating families or proving a stop window.
The keep-vault state-adoption remedy SHALL refuse an enrolled optional
collection-store contract before deleting external state or its compatibility
fence; that remedy SHALL not substitute for validated collection export.

The store runtime SHALL retain its ordinary opening-thread-owned writer and
bounded cache between operations. Every use, including read snapshots and
standalone calls, SHALL be scoped to that runtime's handle checkout. Ordinary
handoff SHALL stop new checkouts, drain borrowers and authority holders before
acquiring the canonical mutation boundary, then retire the quiescent handle
and publish through a lifecycle-thread-owned writer. Cross-thread quiescent
retirement SHALL NOT permit cross-thread SQL, cache access or publication.
The lease head provider SHALL read the exact store/instance identity and
committed head through one fresh read-only SQLite snapshot on its calling
thread; unavailable enrolled stores SHALL NOT be reported as absent heads.
Renew/release reporting SHALL serialize fresh sampling and its exact-token RPC
without holding the manager lock across boundary acquisition. Renewal SHALL
remain available during a synchronous flush, including idle release on the
renewer itself. Only a verified flushed head SHALL complete ordinary release;
busy or unsuccessful idle publication SHALL defer handoff, and explicit
shutdown SHALL expose unsuccessful or pending publication. File-only lifecycle
behavior SHALL remain unchanged.

Batch and held-publication intermediates SHALL be classified separately as
`target-adjacent`: they SHALL remain beside the publication destination for
same-volume atomic rename/link, SHALL exist only during an active publication
or bounded crash recovery, and SHALL NOT be treated as migratable persistent
state. Collection replica staging and retirement SHALL use an exclusively
owned child workspace under that destination parent, excluded from incoming
and outgoing file replication before use. Shared replica names SHALL remain
external inputs; cleanup SHALL NOT unlink them. After migration completes, no persistent machine-local state SHALL
remain under a quiescent vault.

#### Scenario: A synced quiescent vault carries no persistent machine-local state

- **WHEN** a vault that completed state migration has no publication in progress
- **THEN** no persistent index store, epoch record, receipt, lock, rebuild directory, projection, review record, live collection store, write-ahead log, or other external-state family exists under the vault for a file-sync agent to hash, hold, or replace

#### Scenario: The collection store survives index rebuild

- **WHEN** an operator rebuilds or resets derived indexes for a vault
- **THEN** the `external-canonical` collection store is untouched

#### Scenario: Optional compatibility survives a physical-family upgrade

- **WHEN** a mixed/store vault gains a required physical state family
- **THEN** the family migration retains its enrolled `collections-store-v1` descriptor without creating a physical family for it
- **AND** a pre-store runtime still refuses the enrolled manifest

#### Scenario: Compatible store upgrades retain the ordinary handoff

- **WHEN** the installed candidate supports every enrolled optional ID and requires the existing physical families
- **THEN** upgrade skips family migration and uses the ordinary handoff
- **AND** a candidate lacking that support is refused before the serving worker stops

#### Scenario: Mixed operator and supervisor versions retain file-only upgrades

- **WHEN** a file-only vault upgrades through an older supervisor with a newer operator, or a newer supervisor with an older operator
- **THEN** the target request and response retain exactly the legacy interpreter, version and required-family declarations
- **AND** optional candidate support, whether empty or nonempty, remains verified internal metadata rather than an extra target field

#### Scenario: A rejected candidate never makes its cleanup the serving worker's stop

- **WHEN** fresh enrollment makes a candidate incompatible during draining and candidate cleanup outlasts the remaining cutover budget
- **THEN** ordinary admission resumes before cleanup and the serving worker remains available

#### Scenario: Optional support belongs only to a successfully inspected candidate

- **WHEN** an enrolled vault checks a mismatched target or inspection fails after another candidate reported optional support
- **THEN** the other candidate's support is not reused and admission refuses without stopping the serving worker

#### Scenario: Atomic publication scratch follows its target

- **WHEN** a batch or held publication stages a destination
- **THEN** its target-adjacent intermediate is created under the destination parent on the same volume
- **AND** it is cleaned after publication or surfaced through bounded crash recovery
- **AND** it is never migrated to the external state root merely because it is reserved

#### Scenario: Ordinary store operations preserve the warm handle until quiescent handoff

- **WHEN** successive operations use one store and a read borrower is still active when handoff begins
- **THEN** the operations reuse their normal writer cache and retirement waits for that borrower without holding the canonical mutation boundary
- **AND** publication uses a new lifecycle-thread-owned writer only after the retired handle is no longer usable

#### Scenario: A slow flush does not turn lease expiry into successful handoff

- **WHEN** ordinary release publication spans a renewal interval or loses its exact writer token
- **THEN** due renewal reports a freshly sampled committed head without borrowing the business writer or regressing concurrent reports
- **AND** release completes only after a verified flush while that token remains valid; failed publication is reported as pending or unsuccessful

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
