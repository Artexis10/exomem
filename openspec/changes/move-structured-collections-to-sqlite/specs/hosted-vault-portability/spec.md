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

### Requirement: Readable collection exports use portable ZIP bundles

User-facing vault exports SHALL use ZIP with ZIP64 support. Compressible content SHALL use standard DEFLATE; already compressed content MAY remain stored. This format SHALL support ordinary extraction on Windows, macOS and Linux. The separate encrypted operator recovery procedure SHALL NOT determine the user-facing archive format.

A complete readable export SHALL include each collection's descriptor, declared schema and all current rows from the same consistent snapshot. JSON SHALL preserve typed values, authored row bodies, nulls, nested values, stable identities, row versions and provenance. CSV SHALL provide the complete tabular view alongside JSON. The canonical SQLite snapshot SHALL retain the audit and restore state. Readable files SHALL NOT replace that snapshot or change collection ownership.

Generation SHALL stream rows in bounded batches through the existing archive owner. Row count SHALL NOT require whole-collection buffering or per-row Markdown. Existing archive integrity, authorization, portable paths and resource limits SHALL apply. Incomplete generation SHALL NOT report a complete artifact. This follow-up SHALL NOT gate the confirmed collection-store GA scope.

#### Scenario: A summary collection exports every current row

- **GIVEN** a collection has 100,000 rows and publishes only a bounded Markdown overview
- **WHEN** its complete readable export succeeds
- **THEN** JSON and CSV cover every current row from the declared snapshot, beyond ordinary query pagination
- **AND** the descriptor and schema explain the collection without requiring generated item notes

#### Scenario: Nested values retain their types

- **WHEN** exported rows contain nulls, empty strings, numbers, lists or objects
- **THEN** JSON preserves their types and distinctions with the declared schema
- **AND** CSV provides a tabular view while JSON remains the typed representation

#### Scenario: A large collection stays within the export budget

- **WHEN** row data exceeds ordinary query pagination and archive size requires ZIP64
- **THEN** generation streams bounded batches within the configured resource budget
- **AND** the completed ZIP retains verified manifest entries and portable member paths

#### Scenario: Generation stops before all rows are written

- **WHEN** generation fails after writing only part of a collection
- **THEN** the operation reports failure without publishing that archive as complete
- **AND** existing canonical data remains unchanged
