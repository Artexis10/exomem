# hosted-vault-portability Specification

## Purpose

Define quiesced, integrity-verifiable export, staged restore, release, and deletion-preparation hooks for canonical Exomem vault data.

## Requirements

### Requirement: Portability operations use an explicit quiescence boundary
Snapshot export, restore preparation, and deletion preparation SHALL run only after the hosted cell enters an explicit quiescing state at the same mutation boundary used by command and transfer writes. Quiescence MUST stop admission of new mutations, wait for in-flight mutations and durable background writers to finish, and fail without producing a success artifact or deletion clearance when a bounded drain cannot complete. Read admission during quiescence SHALL be explicitly reported by the hook and MUST NOT weaken mutation exclusion.

#### Scenario: Cell reaches export quiescence
- **WHEN** the private operator requests export preparation for a ready hosted cell
- **THEN** the cell stops admitting new mutations and drains all admitted command, upload, and background writes
- **AND** snapshot enumeration begins only after the mutation boundary reports no in-flight writer

#### Scenario: Mutation arrives while export is quiesced
- **WHEN** a new MCP, REST, CLI, upload, or background mutation reaches a cell after export quiescence begins
- **THEN** the mutation is rejected or held outside the snapshot generation according to the documented hook state
- **AND** it cannot partially enter the exported snapshot

#### Scenario: Cell cannot drain within its bound
- **WHEN** an admitted writer or background job does not finish before the configured quiescence deadline
- **THEN** the operation fails with a stable quiescence error
- **AND** no export is reported complete and no deletion clearance is issued

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
