## MODIFIED Requirements

### Requirement: Export contains canonical owned vault data
A completed export SHALL preserve the exact bytes and vault-relative paths of canonical user-owned Markdown, source artifacts, evidence artifacts, and other durable vault files. It SHALL include governed Markdown such as `Knowledge Base/log.md`, user-visible media sidecars, and structured-collection views. It SHALL include a consistent snapshot of the cell's structured-collection store, taken after quiescence through the online backup API and integrity-checked, at the vault replica path. It SHALL exclude runtime service logs, access and query logs, credentials, encryption keys, temporary or lock files, incomplete staging data, SQLite write-ahead and shared-memory files, and rebuildable machine-local indexes such as embedding, lexical, graph, freshness, and CLIP databases.

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
