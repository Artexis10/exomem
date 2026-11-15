# Spec Delta

## Purpose

Share canonical original bytes within a vault while each filing retains its own identity, authorization, custody, and compatible export.

## ADDED Requirements

### Requirement: Physical reuse preserves independent filings

Physical original reuse SHALL preserve each filing's identity, logical address, provenance, governance, and grants. Existing logical duplicate outcomes SHALL remain unchanged. Reuse SHALL NOT merge filings or broaden the caller's visible results.

#### Scenario: Governed filings contain identical bytes

- **WHEN** two separately governed filings reference identical original bytes through a supported shared backend
- **THEN** both retain their existing identity, provenance, permissions, and logical original address
- **AND** a reader can access only the filings and original bytes that their authorization permits

#### Scenario: An ordinary logical duplicate is retried

- **WHEN** the existing duplicate policy resolves a retry to an authorized stored filing
- **THEN** preservation retains its existing `already_stored` outcome
- **AND** physical sharing does not create another filing merely to record reuse

### Requirement: Original resolution authorizes the logical filing

Each read, preview, download, export, and processing request SHALL authorize its logical filing before resolving shared bytes. A digest SHALL NOT grant access. Results SHALL contain no hidden filing identity, pool-hit indication, or saved-byte figure. Canonical-store existence SHALL remain authoritative for store-owned pages.

#### Scenario: An ungranted reader requests a protected dataset

- **WHEN** an owner has warmed a shared original and an ungranted reader requests its protected dataset
- **THEN** the reader receives no protected metadata, rows, preview, stream, or original bytes
- **AND** the owner can repeat the authorized request without changing the original or sidecar

#### Scenario: The disk projection disagrees with the canonical store

- **WHEN** a store-owned filing has a missing or stale disk projection
- **THEN** original resolution follows the canonical filing state and its authorization
- **AND** a physical payload does not make a deleted or unauthorized filing readable

### Requirement: Pooled layouts retain one canonical local object

A pooled archive SHALL store identical original bytes once in its managed canonical pool, identified by SHA-256 and size. Separate filings SHALL resolve their logical addresses through that pool. Temporary staging, recovery replicas, and backup copies SHALL remain separately identified. Missing or corrupt bytes SHALL remain explicit failures.

#### Scenario: Independent filings preserve the same bytes

- **WHEN** two distinct filings in a pooled archive preserve identical bytes
- **THEN** one canonical local object supplies both logical addresses
- **AND** each filing keeps its own page, name, permissions, and grant-bound metadata

#### Scenario: A shared object is corrupt

- **WHEN** the stored object fails its bound digest or size check
- **THEN** original reads report corruption or unavailable bytes
- **AND** they do not silently select another version or retire recovery copies

### Requirement: Physical reuse preserves resource admission

Physical reuse SHALL NOT waive existing declared-byte intake admission or session-held charges for a new filing. The supplied bytes SHALL pass destination staging before a physical match releases allocation. Logical duplicate reuse SHALL retain its current contract. This requirement does not hide later shared-capacity changes or revise existing Cloud offload policy.

#### Scenario: A restricted writer submits bytes held by a hidden filing

- **WHEN** equal-capacity archives contain hidden bytes of equal size and only one matches a restricted writer's new filing
- **THEN** intake, staging, and the filing outcome obey the same admission rules in both archives
- **AND** no result names the hidden filing or reports physical reuse
- **AND** only owner inspection may establish the different physical allocation after commit

### Requirement: Publication and eviction preserve every reader

The system SHALL confirm durable original bytes before committing a filing that depends on them. A shared object SHALL remain available while any authorized reader, writer, processing job, or export pins it. Failure SHALL preserve confirmed copies and report an incomplete operation rather than claiming successful publication or removal.

#### Scenario: Publication is interrupted

- **WHEN** publication stops before its new filing commits
- **THEN** existing filings and their originals remain readable
- **AND** recovery retains uncertain or unreferenced bytes until their custody is proved

#### Scenario: Eviction races a reader of another filing

- **WHEN** one filing's original is being read while another filing requests eviction of the same object
- **THEN** the object remains available until the reader releases its pin
- **AND** other filings do not lose their remote custody or permissions

#### Scenario: A resident dataset shares a Source's bytes

- **WHEN** a resident-only dataset shares an object with an idle Source that has a confirmed remote reference
- **THEN** eviction retains the local object for the dataset
- **AND** the Source's recovery reference does not broaden the dataset's offload policy

### Requirement: Existing archives convert explicitly and remain recoverable

New archives SHALL use the pooled format. Existing archives SHALL retain their layout until an owner with whole-namespace authority requests conversion. Conversion SHALL declare compatibility before relocation, preserve companion bytes and grants, and resume safely across mixed layouts. Retained or re-enrolled compatibility state SHALL prevent unsupported managed downgrade.

#### Scenario: Ordinary deployment reaches an existing archive

- **WHEN** a capable release starts without a conversion request
- **THEN** existing original paths remain unchanged and usable

#### Scenario: Conversion stops between batches

- **WHEN** a conversion stops after some originals reach the pool
- **THEN** supported readers resolve both verified legacy and pooled originals
- **AND** a later conversion continues without rewriting grant-bound companions or losing bytes

#### Scenario: An unsupported managed downgrade is attempted

- **WHEN** a runtime lacks the converted archive's format support and its compatibility requirement remains enrolled
- **THEN** managed admission refuses that archive and identifies compatible recovery or export
- **AND** it does not treat pooled originals as deleted files

#### Scenario: A bare pooled copy reaches a pre-feature runtime

- **WHEN** a bare pooled copy has no external compatibility state and the reader predates its canonical format marker
- **THEN** that reader is outside the supported restore path, including under a pre-feature manager
- **AND** compatible restore or a verified ordinary-file export is required before older-reader access

### Requirement: Export and restore preserve ordinary original access

A compatibility export SHALL materialize verified originals at their logical filenames in a separate archive, preserving page bytes and identity. Backup and restore SHALL retain pooled bytes with their canonical bindings and format declaration. An incomplete export or restore SHALL remain visible and SHALL leave the source archive recoverable.

#### Scenario: An older supported reader consumes an export

- **WHEN** an owner creates a compatibility export for a declared pre-feature runtime
- **THEN** that runtime reads ordinary filenames with the same bytes and pages
- **AND** the pooled source remains intact

#### Scenario: A pooled archive is restored without derived indexes

- **WHEN** a backup restores canonical bindings, the pool, and its format declaration
- **THEN** a capable runtime rebuilds derived indexes and enrols compatibility before serving
- **AND** authorized originals and their grant checks match the source archive

### Requirement: Shared Cloud originals remain within one cell

Cloud reuse SHALL retain existing per-cell credentials, object prefixes, random object names, encryption, and pinned versions. Reuse SHALL require compatible verified byte identity and encryption context. A new protection record SHALL retain the naming rules of `cloud-media-collection`; reuse of a live version SHALL copy that version's reference without uploading another version. Sharing SHALL NOT cross cells.

#### Scenario: Matching bytes belong to different cells

- **WHEN** two cells preserve identical original bytes
- **THEN** neither cell references an object under the other's prefix or credentials
- **AND** deleting one cell cannot remove the other cell's originals

#### Scenario: The matching version uses an incompatible encryption context

- **WHEN** a matching digest belongs to a version outside the current compatible encryption context
- **THEN** preservation keeps the required encryption boundary
- **AND** it reports no unsupported physical-sharing guarantee

### Requirement: Every retained owner protects a shared original

Removing one filing's reference SHALL NOT make a shared original collectible while another retained owner requires it. Cloud release, protection records, backup manifests, running exports, and collection SHALL retain their existing contracts. A Cloud reference SHALL retire only after its durable release record. Running services SHALL gain no Cloud version-deletion capability.

#### Scenario: One filing releases its shared version

- **WHEN** one filing retires its reference and another live filing or retained backup still references the version
- **THEN** collection leaves the version recoverable
- **AND** the remaining filing retains its existing authorized access

#### Scenario: A crash follows a release record

- **WHEN** the service crashes after recording a release and before retiring the reference
- **THEN** recovery retains the reference and the release record
- **AND** collection does not mistake that record for proof that every owner released the version

### Requirement: Storage reports distinguish references from physical versions

The owner allowance SHALL count each referenced plaintext version once, including references in Trash under the existing allowance contract. Provider metering SHALL separately report physical versions and retained storage within the cell prefix. An unreadable ownership census SHALL remain unknown. Releasing one shared reference SHALL NOT erase remaining usage or charge the shared version twice.

#### Scenario: Several filings reference one version

- **WHEN** several live or Trash filings reference the same stored version
- **THEN** the owner allowance counts that version once
- **AND** releasing one reference leaves its charge while another counted reference remains

#### Scenario: A retained backup is the only owner

- **WHEN** no counted filing references a version but a retained backup still requires it
- **THEN** owner allowance and provider physical storage remain separately named
- **AND** the provider figure still includes the retained version
