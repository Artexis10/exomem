# Spec Delta

## ADDED Requirements

### Requirement: Physical reuse preserves independent filings

Physical original reuse SHALL preserve each filing's identity, logical address, provenance, governance, and grants. Existing logical duplicate outcomes SHALL remain unchanged. Reuse SHALL NOT merge filings or broaden the caller's visible results. This requirement defines no local filesystem conversion; the local compatibility decision remains open in `design.md`.

#### Scenario: Governed filings contain identical bytes

- **WHEN** two separately governed filings reference identical original bytes through a supported shared backend
- **THEN** both retain their existing identity, provenance, permissions, and logical original address
- **AND** a reader can access only the filings and original bytes that their authorization permits

#### Scenario: An ordinary logical duplicate is retried

- **WHEN** the existing duplicate policy resolves a retry to an authorized stored filing
- **THEN** preservation retains its existing `already_stored` outcome
- **AND** physical sharing does not create another filing merely to record reuse

### Requirement: Original resolution authorizes the logical filing

Each read, preview, download, export, and processing request SHALL authorize its logical filing before resolving shared original bytes. A physical object identifier or digest SHALL NOT grant access. Reuse SHALL NOT disclose another filing's existence, path, protection class, or storage use. Canonical-store existence SHALL remain authoritative for store-owned pages.

#### Scenario: An ungranted reader requests a protected dataset

- **WHEN** an owner has warmed a shared original and an ungranted reader requests its protected dataset
- **THEN** the reader receives no protected metadata, rows, preview, stream, or original bytes
- **AND** the owner can repeat the authorized request without changing the original or sidecar

#### Scenario: The disk projection disagrees with the canonical store

- **WHEN** a store-owned filing has a missing or stale disk projection
- **THEN** original resolution follows the canonical filing state and its authorization
- **AND** a physical payload does not make a deleted or unauthorized filing readable

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
