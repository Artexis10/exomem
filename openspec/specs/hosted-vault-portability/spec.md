# hosted-vault-portability Specification

## Purpose

Define quiesced, integrity-verifiable export, staged restore, release, and deletion-preparation hooks for canonical Exomem vault data.

## Requirements

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

### Requirement: Export contains canonical owned vault data
A completed export SHALL preserve the exact bytes and vault-relative paths of canonical user-owned Markdown, source artifacts, evidence artifacts, and other durable vault files. It SHALL include governed Markdown such as `Knowledge Base/log.md` and user-visible media sidecars, while excluding runtime service logs, access and query logs, credentials, encryption keys, temporary or lock files, incomplete staging data, and rebuildable machine-local indexes such as embedding, lexical, graph, freshness, and CLIP databases.

#### Scenario: Canonical Markdown and media are exported
- **WHEN** a quiesced vault contains governed notes, sources, evidence, binary media, and user-visible Markdown sidecars
- **THEN** the export contains each canonical file at its original vault-relative path with byte-identical content

#### Scenario: Derived and secret material exists
- **WHEN** the cell also contains rebuildable SQLite indexes, caches, runtime logs, query records, credentials, keys, locks, and temporary files
- **THEN** none of those runtime or derived artifacts is included in the export
- **AND** their exclusion does not remove canonical Markdown or binary content

#### Scenario: Knowledge Base activity history exists
- **WHEN** the canonical vault contains `Knowledge Base/log.md` or governed archive history under the vault
- **THEN** those vault-history files are included even though runtime service and query logs are excluded

### Requirement: Export manifests are integrity-verifiable and host independent
Every completed export SHALL contain a versioned manifest that identifies each exported file by normalized vault-relative path, byte size, and SHA-256 digest. The manifest and archive MUST NOT contain absolute host paths, private cell addresses, runtime credentials, tenant encryption keys, or another tenant's identifiers, and an export result SHALL be published only after all manifest entries have been verified against the completed archive.

#### Scenario: Export completes successfully
- **WHEN** all canonical files are copied into the export artifact
- **THEN** the manifest lists every included file with its relative path, size, and SHA-256 digest
- **AND** verification of the archive against the manifest succeeds before the hook reports completion

#### Scenario: File changes or disappears during snapshot construction
- **WHEN** an enumerated file no longer matches the bytes, size, or digest observed for the quiesced snapshot
- **THEN** export preparation fails without publishing the artifact as complete

#### Scenario: Manifest is inspected outside the source host
- **WHEN** a valid export is opened on a different machine
- **THEN** every content reference is resolvable from vault-relative paths alone
- **AND** the manifest reveals no source-host path, credential, encryption key, or private cell address

### Requirement: Export artifacts remain internal until control-plane delivery
The Exomem portability hook SHALL return an opaque internal artifact reference and integrity metadata to the authorized control plane. It MUST NOT create a public download URL, persist an export in the control-plane account database, or expose a cell master credential; public delivery, expiry, object-storage policy, and user-session authorization remain control-plane responsibilities.

#### Scenario: Control plane receives a completed export
- **WHEN** export preparation and manifest verification succeed
- **THEN** the hook returns an opaque artifact reference plus format, size, and digest metadata
- **AND** the artifact remains inaccessible without the private control-plane delivery step

#### Scenario: Public caller invokes the operator hook directly
- **WHEN** a request lacks private operator authorization even if it names a valid tenant or artifact reference
- **THEN** the portability hook rejects it without revealing whether the artifact or tenant exists

### Requirement: Restore validates into a new staging root before publication
Restore preparation SHALL accept only a supported versioned export, validate its manifest and every file digest, reject absolute paths, traversal, unsafe links, duplicate normalized paths, cross-platform case collisions, unsupported entry types, and configured resource-limit violations, and extract only into a new empty staging root. It MUST NOT overlay or mutate an active vault. The staged vault SHALL become publishable only after structural validation succeeds, and rebuildable sidecars MUST be regenerated rather than restored from the archive.

#### Scenario: Valid export is prepared for restore
- **WHEN** a supported export has a valid manifest, safe paths, matching digests, and content within configured limits
- **THEN** restore preparation writes its canonical files into a new empty staging root
- **AND** the staged paths and bytes match the export manifest exactly

#### Scenario: Restore targets an active or non-empty vault
- **WHEN** restore preparation is asked to overlay an active cell or a non-empty destination
- **THEN** it fails before writing archive content into that destination

#### Scenario: Archive contains an unsafe path or link
- **WHEN** an entry is absolute, traverses above the vault root, is an unsafe symbolic or hard link, duplicates a normalized path, or collides by case with another path
- **THEN** the entire restore is rejected
- **AND** no prepared vault is published

#### Scenario: Manifest digest does not match content
- **WHEN** any restored file differs from its declared size or SHA-256 digest
- **THEN** restore preparation fails and the staging root is not marked publishable

#### Scenario: Export omits rebuildable indexes
- **WHEN** a valid restored vault has no embedding, lexical, graph, freshness, or CLIP sidecars
- **THEN** hosted readiness treats those indexes as absent rebuildable state rather than data loss
- **AND** no derived sidecar from the source host is required for canonical restore success

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

### Requirement: Restored canonical data round-trips without semantic rewriting
An export followed by restore preparation SHALL preserve canonical file bytes and relative paths without converting Markdown into database rows, renaming user files, changing frontmatter, or recomputing authored history. Any post-restore index build SHALL derive from the restored files and MUST NOT modify their canonical content as part of readiness.

#### Scenario: Vault is exported and restored unchanged
- **WHEN** a quiesced vault is exported and then prepared for restore into a new cell
- **THEN** every manifest-listed canonical file has the same relative path and SHA-256 digest in the staged vault
- **AND** no authored Markdown or binary file is rewritten by portability or index preparation

### Requirement: Quiesced export has an explicit release outcome
After export preparation succeeds or fails, the portability contract SHALL require an explicit release outcome that either resumes mutation admission for the same cell or hands the quiesced checkpoint to another lifecycle operation. Releasing export quiescence MUST NOT occur while snapshot readers still depend on mutable source files, and repeated release requests SHALL be idempotent.

#### Scenario: Export is delivered and the cell resumes
- **WHEN** the control plane confirms that snapshot preparation no longer needs the quiesced source state
- **THEN** an authorized release resumes normal mutation admission
- **AND** a repeated release does not create a second state transition

#### Scenario: Export fails before publication
- **WHEN** export preparation fails and no deletion workflow has claimed the checkpoint
- **THEN** the control plane can explicitly release the cell back to its prior routable state

### Requirement: Deletion preparation seals the cell before external destruction
The deletion-preparation hook SHALL require the control plane to stop public routing, SHALL quiesce the cell, and SHALL return an opaque idempotent deletion checkpoint only after no mutation or transfer remains in flight. Once sealed for deletion, the cell MUST reject reads, writes, uploads, downloads, restore publication, and ordinary readiness until an authorized lifecycle decision changes that state. The Exomem hook MUST NOT itself cancel billing, delete account records, destroy encryption keys, or claim that external live storage and backups have been erased.

#### Scenario: Routed account is prepared for deletion
- **WHEN** deletion preparation is requested while public routing to the cell is still active
- **THEN** the hook refuses to issue deletion clearance

#### Scenario: Unrouted cell drains and seals
- **WHEN** public routing is stopped and all admitted writers and transfers drain successfully
- **THEN** the cell enters its sealed deletion state and returns one opaque deletion checkpoint
- **AND** subsequent data-plane requests are rejected without reopening the cell

#### Scenario: Deletion preparation is retried
- **WHEN** the authorized control plane repeats deletion preparation for an already sealed cell
- **THEN** the hook returns the same deletion checkpoint without repeating a destructive action

#### Scenario: Control plane destroys external resources
- **WHEN** a valid deletion checkpoint is used to destroy cell storage, backups, and keys and to update account or billing state
- **THEN** those external actions remain the control plane's responsibility
- **AND** Exomem reports only the cell-side sealed and quiesced facts it can verify

### Requirement: Portability hooks are private, auditable, and content-minimal
Export, restore, release, and deletion-preparation hooks SHALL require private operator authorization and SHALL emit structured lifecycle records containing operation identity, cell identity, state transition, timestamps, manifest or checkpoint digest, and outcome. Their operational logs MUST NOT contain note bodies, extracted text, query text, credentials, encryption keys, public transfer tokens, or absolute source paths.

#### Scenario: Authorized portability operation completes
- **WHEN** an export, restore, release, or deletion-preparation hook changes lifecycle state
- **THEN** a structured audit record captures the operation and outcome without vault content or secrets

#### Scenario: Unauthorized portability request is attempted
- **WHEN** a caller without private operator authority invokes a portability hook
- **THEN** the request is rejected without changing cell state
- **AND** the rejection log contains no vault content, secret, or existence oracle for another tenant
