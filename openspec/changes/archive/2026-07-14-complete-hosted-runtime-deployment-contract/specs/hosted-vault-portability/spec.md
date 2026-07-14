## MODIFIED Requirements

### Requirement: Portability operations use an explicit quiescence boundary

Source snapshot export and deletion preparation SHALL run only after the serving hosted cell enters explicit quiescence at the shared lifecycle/mutation boundary. Quiescence MUST stop admission of new mutations and transfers, wait for admitted commands, uploads/downloads, and durable background writers to finish, and fail without a success artifact or deletion clearance when bounded drain cannot complete. In-place or live restore SHALL remain forbidden. Offline restore into a new unserved target SHALL not require a nonexistent running lifecycle to quiesce; instead it SHALL require stopped external routing/workload plus an exclusive target lifetime lock shared with server startup and every restore process. Read admission during source quiescence SHALL be explicitly reported and MUST NOT weaken mutation exclusion.

#### Scenario: Source cell reaches export quiescence

- **WHEN** the private operator requests export preparation for a ready hosted cell
- **THEN** the cell stops admitting new mutations/transfers and drains all admitted command, upload/download, and background writes
- **AND** snapshot enumeration begins only after lifecycle and mutation authorities report no in-flight participant

#### Scenario: Mutation or transfer arrives while export is quiesced

- **WHEN** new MCP, REST, CLI, upload/download, or background work reaches a source cell after quiescence begins
- **THEN** it is rejected or held outside snapshot generation according to the documented hook state
- **AND** it cannot partially enter the exported snapshot

#### Scenario: Source cell cannot drain within its bound

- **WHEN** an admitted writer, transfer, or background job does not finish before the configured quiescence deadline
- **THEN** the operation fails with a stable quiescence error
- **AND** no export is reported complete and no deletion clearance is issued

#### Scenario: Offline target restore acquires exclusivity

- **WHEN** routing/workload are stopped and restore acquires the target state-root lifetime lock before creating or inspecting target bindings
- **THEN** it may prepare a new candidate without a running target lifecycle
- **AND** concurrent server startup/restore blocks or fails before publishing canonical bytes

#### Scenario: Restore targets a serving or lock-owned cell

- **WHEN** a target server/restore owns the lifetime lock, routing/workload is not declared stopped, or a live/in-place destination is requested
- **THEN** restore fails closed without overlaying or mutating canonical target data

## ADDED Requirements

### Requirement: Offline Restore Publishes A Recoverable Target-Bound Candidate

The hosted image SHALL implement the normative offline restore operator command for a new target cell that preserves the source logical vault identity. It SHALL require an authorized opaque artifact reference and expected archive SHA-256 outside the unsigned manifest; verify archive bytes, file digests, source cell/vault identity, and target distinction; reject all source binding/credential/lifecycle/lease/idempotency/replay/temp/runtime entries; and create fresh target vault/state/log bindings. State/log setup and operation progress SHALL use a durable request-bound journal. Only canonical vault publication SHALL be claimed atomic, using one same-filesystem rename from an unclaimed sibling staging root to an absent target root. Rebuildable state MUST be regenerated from published canonical files and never copied from source.

#### Scenario: Valid archive becomes a target candidate

- **WHEN** an authorized locked restore supplies an archive matching the out-of-band SHA-256 and source identities, a distinct target cell ID, the same logical vault ID, empty/unclaimed roots, and valid non-root UID/GID
- **THEN** canonical paths/bytes plus fresh target vault binding are published by one rename, while state/log bindings and journal converge recoverably
- **AND** the result reports only artifact/archive/manifest digests, opaque source/target identities, target release/protocol/binding, journal outcome, and derived readiness

#### Scenario: Archive authenticity or source identity is not pinned

- **WHEN** artifact reference or expected archive SHA-256 is absent/mismatched, manifest source cell/vault differs, or the unsigned format claims an unsupported signature
- **THEN** restore fails before publication
- **AND** self-consistent attacker-recomputed manifest fields are not treated as source authenticity

#### Scenario: Archive carries source runtime state

- **WHEN** an archive or manifest declares a hosted binding marker, security/credential/JTI state, lifecycle state, lease, idempotency store, transfer temp, log, or other source runtime artifact
- **THEN** restore fails before target publication even when entry and archive digests match
- **AND** no source runtime identity/state is copied into target vault, state, or log roots

#### Scenario: Target is active, non-empty, or source-cell-identical

- **WHEN** restore lacks the target lock, targets existing foreign/non-empty roots, a serving cell, or the source cell identity
- **THEN** it fails closed without overlaying or mutating the target
- **AND** the source archive remains unchanged for a new candidate attempt

#### Scenario: Process crashes around canonical publication

- **WHEN** restore crashes after any root marker, journal transition, staging completion, vault rename, derived rebuild, or proof write
- **THEN** identical retry under the lifetime lock resumes/cleans only its operation-owned state and returns the same canonical outcome
- **AND** a target vault found after a pre-publication journal phase is adopted only when exact target binding and every manifest path/byte digest prove the rename committed

#### Scenario: Derived rebuild fails or changes canonical bytes

- **WHEN** an optional rebuildable index cannot be produced after canonical publication
- **THEN** the candidate remains content-valid but reports a stable degraded derived-state result
- **AND WHEN** rebuild changes a manifest-owned canonical byte/path
- **THEN** the operation restores verified canonical bytes or marks a hard integrity failure before readiness

#### Scenario: Restore command is retried or conflicts

- **WHEN** the identical operation ID/request digest retry after any journal phase
- **THEN** the command resumes or returns the previously verified candidate proof without republishing/reinitializing complete state
- **AND** changed artifact, digest, source/target identity, roots, UID/GID, release/protocol, or credential bootstrap input conflicts rather than adopting prior results
