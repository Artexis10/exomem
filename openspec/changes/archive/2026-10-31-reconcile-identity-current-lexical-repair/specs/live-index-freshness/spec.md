## ADDED Requirements

### Requirement: Full Lexical Repair Reuses An Identity-Current Catalogue

A full lexical repair SHALL reuse the live catalogue when it exists, is neither
retired nor bound for quarantine, passes SQLite's quick check, and carries the
current schema, the catalogue identity the repair captured before the copy,
and the declarations a fresh build creates. The
repair SHALL take a consistent copy of that catalogue and reconcile the copy
against the corpus walk a fresh build uses, so that it publishes what a fresh
build of the same walk publishes. In every other case, and when the copy or
the reconcile fails, the repair SHALL parse the whole corpus. Each pass SHALL
log its route with page counts and elapsed milliseconds.

#### Scenario: Unchanged pages are not re-parsed

- **WHEN** a full repair runs over an identity-current live catalogue
- **AND** since that catalogue was built one page was edited, one deleted and
  one added
- **THEN** the repair parses only the edited and the added page
- **AND** removes the deleted page's rows
- **AND** the published catalogue answers the same searches and holds the same
  page rows as a fresh build of the same vault

#### Scenario: A stored page is kept only while all of its inputs match

- **WHEN** the reconcile compares a stored page with the walk
- **THEN** it keeps the page only when the stored content hash equals the hash
  of the page's current bytes, the stored mtime and scope membership equal the
  walk's, and the stored semantic generation equals the generation the current
  parser and registries give those bytes
- **AND** it re-parses a page whose stored content hash is NULL

#### Scenario: A replacement that keeps its mtime is re-parsed

- **WHEN** a page's bytes change and its mtime is restored to the old value
- **THEN** the repair re-parses that page because its content hash differs
- **AND** a search for the new text finds the page

#### Scenario: An identity change rebuilds the whole corpus

- **WHEN** the catalogue identity differs from the identity stored in the live
  catalogue
- **THEN** the repair parses every walked page into an empty replacement

#### Scenario: Delta replay and publication are unchanged

- **WHEN** a reconciled replacement is complete
- **THEN** the repair replays the deltas from its start checkpoints, persists
  the target checkpoints, folds the WAL and publishes under the same guards and
  retries as a whole-corpus build
