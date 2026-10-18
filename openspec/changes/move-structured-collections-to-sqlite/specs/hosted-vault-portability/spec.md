## MODIFIED Requirements

### Requirement: Export contains canonical owned vault data
A completed export SHALL preserve the exact bytes and vault-relative paths of canonical user-owned Markdown, source artifacts, evidence artifacts, and other durable vault files. It SHALL include governed Markdown such as `Knowledge Base/log.md`, user-visible media sidecars, and structured-collection views. It SHALL include a consistent snapshot of the cell's structured-collection store, taken after quiescence through the online backup API and integrity-checked, at the vault replica path, and SHALL NEVER include the live store's database, write-ahead or shared-memory files. It SHALL exclude runtime service logs, access and query logs, credentials, encryption keys, temporary or lock files, incomplete staging data, SQLite write-ahead and shared-memory files, and rebuildable machine-local indexes such as embedding, lexical, graph, freshness, and CLIP databases.

#### Scenario: Canonical Markdown and media are exported
- **WHEN** a quiesced vault contains governed notes, sources, evidence, binary media, and user-visible Markdown sidecars
- **THEN** the export contains each canonical file at its original vault-relative path with byte-identical content

#### Scenario: Collection store is exported as canonical data
- **WHEN** a quiesced cell holds Records and Planning collections in its collection store
- **THEN** the export contains one integrity-checked single-file snapshot of the store that reflects the last committed transaction, and no write-ahead or shared-memory file

#### Scenario: Derived and secret material exists
- **WHEN** the cell also contains rebuildable SQLite indexes, caches, runtime logs, query records, credentials, keys, locks, and temporary files
- **THEN** none of those runtime or derived artifacts is included in the export
- **AND** their exclusion does not remove canonical Markdown, binary content, or the collection store snapshot

#### Scenario: Knowledge Base activity history exists
- **WHEN** the canonical vault contains `Knowledge Base/log.md` or governed archive history under the vault
- **THEN** those vault-history files are included even though runtime service and query logs are excluded

#### Scenario: Operator export preserves rows ahead of a lagging replica
- **GIVEN** a selected backup contains a published replica and newer committed rows in its canonical database and WAL
- **WHEN** the operator prepares its exclusively restored scratch vault for export
- **THEN** one validated SQLite backup preserves those rows, their history, marker bindings and lineage at the replica path
- **AND** the source backup remains unchanged and no host state or SQLite companion enters the existing tar artifact

#### Scenario: Interrupted publication work is excluded
- **WHEN** restored scratch contains publication workspaces and snapshot or SQLite companion files
- **THEN** the existing portability classifier excludes them without removing canonical user files or the prepared replica

## ADDED Requirements

### Requirement: Cloud restore proves collection data before publication
Cloud restore SHALL validate the replica's integrity, supported schema, chain, marker bindings and pure adoptability conditions in private staging. It SHALL import and prove file-routed collections through the existing preflight helpers before any destination rename. Validation SHALL leave archived bytes unchanged and SHALL NOT restamp an instance or grant destination writer authority. Generic ZIP restore SHALL retain its existing invalid-file preservation. Cloud restore SHALL recognize the existing single `vault/` wrapper and refuse mixed roots. Its receipt SHALL describe restored bytes; it SHALL NOT claim collection admission without separate proof.

#### Scenario: Invalid collection proof leaves the destination untouched
- **WHEN** an incoming Cloud vault has invalid canonical history or fails its file collection proof
- **THEN** restoration refuses before moving the existing vault or state

#### Scenario: A supported older schema remains byte-preserved
- **WHEN** staging validates a supported earlier store schema
- **THEN** validation migrates only its private checking copy and leaves the archived replica unchanged

#### Scenario: Restoration does not reset a coordinator fence
- **WHEN** a generation-2 or later marker meets a fresh coordinator
- **THEN** the existing fence refusal remains and restoration changes no source marker to manufacture admission

#### Scenario: An older ancestry witness predates a later collection
- **GIVEN** an older replica has a valid historical pending-create target and lacks a later acknowledged collection
- **WHEN** the operator proves its head and lineage are ancestors of the validated canonical snapshot
- **THEN** replacement may proceed after structural, identity, overlapping ownership and departure checks
- **AND** the older witness grants no restore or adoption authority

#### Scenario: A canonical replacement carries an obsolete target
- **WHEN** the replacement snapshot's pending-create target differs from the current authority marker
- **THEN** preparation refuses before replacing the previous replica
