## ADDED Requirements

### Requirement: Source coverage requires a live derivative

The `unprocessed_source` family SHALL select Sources by the note-type registry's `source` role. It SHALL count a Source as compiled only when at least one page named by the Source's `ingested_into` exists, is not itself a Source or Evidence page, has a status whose registry class is `live`, and cites the Source in its own `sources:` claim as resolved by source closure. Every other Source SHALL remain in the family. Its finding SHALL list each derivative that fails the rule with one reason: `pending`, `retired`, `missing` or `claim_unresolved`. The rule SHALL branch only on the registry's closed `class` and `role` attributes. The finding's fingerprint SHALL cover the Source's content hash and each derivative's reference, status class and claim result.

#### Scenario: A draft derivative keeps its Source outstanding

- **WHEN** a draft note cites a Source and the Source's `ingested_into` names that note
- **THEN** the Source remains an `unprocessed_source` finding that lists the note with reason `pending`

#### Scenario: Promoting the draft completes the Source

- **WHEN** that note's status changes to one whose class is `live` and its claim still resolves
- **THEN** the Source leaves the family

#### Scenario: A stale back-reference does not count

- **WHEN** a Source's `ingested_into` names a page that no longer exists
- **THEN** the Source remains a finding that lists the name with reason `missing`

#### Scenario: A changed derivative state resurfaces a dismissed finding

- **WHEN** an `unprocessed_source` finding is dismissed and its derivative later changes from draft to retired
- **THEN** the fingerprint changes and the finding is open again

### Requirement: Unprocessed sources are part of the due-state projection

The due-state projection SHALL include `unprocessed_source`, due from the Source's capture date. A governed write that creates, edits, supersedes or deletes a page SHALL refresh the projection rows of every Source that the page cites before or after the write. Out-of-band edits SHALL be healed by the existing reconcile path. The bound on categories that a post-write block may carry SHALL follow the projection's category list.

#### Scenario: Compiling a Source updates the projection without a reconcile

- **WHEN** a live note citing an outstanding Source is created
- **THEN** the next served projection no longer counts that Source, with no reconcile run in between

#### Scenario: A draft citation leaves the count unchanged

- **WHEN** a draft note citing an outstanding Source is created
- **THEN** the served `unprocessed_source` count is unchanged

### Requirement: Due-state blocks report outstanding beside visible counts

The served due-state block SHALL keep `total` and `categories` as the counts the audience sees after review state, and SHALL add `outstanding_total` and `outstanding`, the same rows counted before dismissal and snoozing. Egress rules SHALL apply to both counts alike.

#### Scenario: Dismissal lowers the visible count only

- **WHEN** an agent dismisses three outstanding items of one category
- **THEN** `categories` for that category falls by three and `outstanding` for it is unchanged

### Requirement: Unprocessed-source batches carry what an agent needs to act

Each `unprocessed_source` finding SHALL carry the Source's title, a bounded excerpt, its recorded grouping metadata (`import_collection`, the folder of its `collection_path`, `source_kind`, `domain` and capture date, each only when recorded) and its derivatives with their states. `review_memory(mode="compilation", sources=[...])` SHALL return, beside its scaffold, a bounded excerpt of each Source within a total character bound. The server SHALL NOT group Sources by topic.

#### Scenario: A batch of imported Sources arrives ready to group

- **WHEN** `review_memory(mode="unprocessed-sources", limit=10)` runs after a collection import
- **THEN** each of at most ten findings carries a title, an excerpt within its bound, the import collection and folder, and its derivatives

#### Scenario: A compilation scaffold includes the Sources' text

- **WHEN** `review_memory(mode="compilation", sources=[a, b, c])` runs
- **THEN** the result carries an excerpt of each of the three Sources within the bound
