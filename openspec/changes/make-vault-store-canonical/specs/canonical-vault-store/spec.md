## ADDED Requirements

### Requirement: One canonical store per vault
In store mode, each vault SHALL have one SQLite database that is the single source of truth for the vault's text pages and the text files that Exomem writes into the vault. The database SHALL hold each page's exact bytes, its parsed frontmatter, the structured collection tables, the lexical catalogue rows, the epistemic graph rows, an audit trail and a projection manifest. Binary media SHALL stay files that the store references by path and content hash.

#### Scenario: A page has one authoritative copy
- **WHEN** a reader asks for a page in a store-mode vault
- **THEN** Exomem answers from the store's bytes for that page
- **AND** no answer depends on the Markdown file on disk

#### Scenario: Binary media stays on disk
- **WHEN** a governed write preserves a binary Evidence file
- **THEN** the file stays on disk and the store records its path and content hash in the same transaction as the page that cites it

### Requirement: One transaction per governed write
A governed write in store mode SHALL commit every file it changes, the lexical catalogue rows, the graph nodes, edges and dependencies derived from those files, one audit row per file and one projection intent per file in one `BEGIN IMMEDIATE` transaction. The write SHALL acknowledge only after that transaction is durable. Derivation SHALL run from the bytes being written, before the transaction, with the same derivation code that file mode uses.

#### Scenario: Search and graph see the write at acknowledgement
- **WHEN** a governed write that adds a page and a link returns success
- **THEN** a lexical search for a term in the page returns the page
- **AND** the graph returns the new edge from that page
- **AND** neither depends on a background drain, watcher event or repair

#### Scenario: A hard kill leaves no half-applied write
- **WHEN** the process is killed at any point while a governed write runs
- **THEN** after restart every file of that write shows either all of its old state or all of its new state, in page bytes, lexical rows and graph rows together
- **AND** no repair, rebuild or reconcile is needed to make the derived rows match the page bytes

#### Scenario: Derived rows match file mode exactly
- **WHEN** the same sequence of page bytes is written once in file mode and once in store mode from the same starting vault
- **THEN** the lexical page rows, semantic unit rows, graph nodes, graph edges, graph dependencies and parent references are equal, apart from columns that record write time or write order

### Requirement: Markdown is a byte-exact projection
In store mode, Exomem SHALL write each committed file to the vault as a byte-exact projection after the transaction commits. A projector SHALL make the projected file durable before it records the file's hash, size, modification time and revision in the projection manifest. At startup the projector SHALL remove its own unfinished temporary files and SHALL project every intent still queued. Several revisions of one file that queue before projection MAY coalesce into the latest one.

#### Scenario: Projection catches up after a kill
- **WHEN** the process is killed while projection intents are still queued
- **THEN** after restart the projector writes every queued file
- **AND** every projected file's bytes equal the store's bytes for that file
- **AND** no temporary projection file remains

#### Scenario: Exomem's own projection is not an outside edit
- **WHEN** the watcher or reconcile sees a file whose hash equals the manifest hash
- **THEN** Exomem treats the file as unchanged and imports nothing

#### Scenario: Projection lag is visible
- **WHEN** projection intents are queued
- **THEN** status reports the number of queued files and the age of the oldest one

### Requirement: Outside edits return through a base-revision check
In store mode, a vault file whose hash differs from the projection manifest SHALL be treated as an outside edit. When the store revision of that file equals the manifest revision and no projection intent is queued for it, Exomem SHALL import the file as a governed write with outside-editor provenance. Otherwise Exomem SHALL keep the outside bytes as a held correction for review and SHALL NOT overwrite either version. An outside delete SHALL become a held deletion, never a silent delete of canonical content.

#### Scenario: A clean outside edit imports
- **WHEN** a person edits a projected page in Obsidian and no Exomem write to that page is pending
- **THEN** the next watcher event or reconcile imports the new bytes in one transaction with their derived rows
- **AND** the audit row names the outside editor as the origin

#### Scenario: A concurrent outside edit is held
- **WHEN** a person edits a page on disk while a newer store revision of that page is not yet projected
- **THEN** Exomem keeps the person's bytes as a held correction
- **AND** the projector does not overwrite the person's file until the correction is resolved

#### Scenario: An outside delete is held
- **WHEN** a projected page disappears from disk without an Exomem delete
- **THEN** the store keeps the page and records a held deletion for review

### Requirement: Semantic units are first-class store rows
In store mode, each semantic unit of a page SHALL be one store row with a stable identity, its page, its ordinal and exact byte span in the page bytes, its category, kind and text, its provenance, its verification status and its lifecycle. The unit identity SHALL be carried in the page bytes, so the Markdown projection stays complete. Typed edges SHALL be able to start or end at a unit as well as at a page. A governed write that changes a unit SHALL change the page bytes and the unit row in the same transaction.

#### Scenario: A link reaches the exact unit
- **WHEN** a page links to another page's unit by its anchor
- **THEN** the graph edge ends at that unit, not only at its page

#### Scenario: Refuting one claim changes one unit
- **WHEN** a governed write marks one claim on a page as refuted
- **THEN** the page bytes and that unit's verification status commit in one transaction
- **AND** the other units on the page keep their identities and status

#### Scenario: A new unit gets an identity in its page
- **WHEN** a governed write adds a unit without an anchor
- **THEN** the transaction assigns an identity and writes its anchor into the page bytes

### Requirement: Originals record their canonicity
In store mode, every stored file SHALL carry a canonicity record with its origin, content hash, size, format and derivation: either original, or derived from a named content hash by a named transform. A governed write SHALL be able to supersede a file recorded as original with the true original. After it, the earlier file SHALL remain stored as derived from the true original, citations SHALL resolve to the true original, and the audit trail SHALL record the change.

#### Scenario: A compressed copy filed as original is corrected
- **WHEN** a compressed copy was filed as an original and the true original arrives later
- **THEN** one governed write records the true original as original and the copy as derived from it
- **AND** pages that cited the copy resolve to the true original
- **AND** the copy's bytes stay available

#### Scenario: Every original says how it is known to be original
- **WHEN** a reader inspects a stored file
- **THEN** it sees the file's origin, hash, size, format and derivation

### Requirement: Migration into the store is verified and reversible
Exomem SHALL migrate a file-mode vault into store mode offline. The migration SHALL load every page's exact bytes and the current derived rows, and SHALL verify before activation that the derived rows equal a fresh derivation from the loaded bytes and that a projection of every page reproduces the original bytes. Reverting to file mode SHALL need only the projected Markdown: stopping the store and rebuilding the file-mode sidecars from the projection SHALL restore file mode with the same pages.

#### Scenario: Migration preserves every page byte
- **WHEN** a vault is migrated and then fully projected to an empty directory
- **THEN** every projected file is byte-identical to the file it was migrated from

#### Scenario: Reverting needs no store
- **WHEN** an operator reverts a store-mode vault to file mode
- **THEN** file mode rebuilds its lexical catalogue and graph from the projected Markdown
- **AND** search and graph answers match the store's answers for the same pages
