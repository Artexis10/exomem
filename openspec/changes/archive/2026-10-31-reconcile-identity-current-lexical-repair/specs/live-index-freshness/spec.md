## ADDED Requirements

### Requirement: Full Lexical Repair Reuses An Identity-Current Catalogue

A full lexical repair SHALL reuse the live catalogue when it exists, is neither
retired nor bound for quarantine, passes SQLite's quick check, and carries the
current schema, the stamp of the running release, the catalogue identity the
repair captured before the copy, the declarations a fresh build creates, and a
full-text row for every page and semantic unit. The repair SHALL take a
consistent copy of that catalogue and reconcile the copy against the corpus
walk a fresh build uses. For an undamaged catalogue that the running release
built, the repair SHALL publish what a fresh build of the same walk publishes.
In every other case, and when the copy or the reconcile fails, the repair
SHALL parse the whole corpus. Lock contention and a busy or locked database
SHALL decline the pass so that the worker retries it. Each pass SHALL log its
route with page counts and elapsed milliseconds.

The reconcile cannot see damage that leaves a page's inputs intact, such as a
changed column, a full-text row with the wrong text, or a deleted semantic
unit. Such a row SHALL stay until a write
to that page re-parses it, or until a whole-corpus build replaces it after an
upgrade or a refusal above. Custody verification of a governed write flags a
changed eligibility column, and that write's targeted re-parse heals the row; a
flagged row never requests a full repair.

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

#### Scenario: A catalogue from another release is rebuilt whole

- **WHEN** the release stamp in the live catalogue differs from the running
  release, or is missing
- **AND** the running release derives different rows from the same bytes
- **THEN** the repair parses every walked page into an empty replacement
- **AND** the published catalogue holds the rows the running release derives

#### Scenario: A missing full-text row rebuilds the whole corpus

- **WHEN** a page or semantic unit in the live catalogue has no full-text row
- **THEN** the repair parses every walked page into an empty replacement
- **AND** a search finds that page again

#### Scenario: Delta replay and publication are unchanged

- **WHEN** a reconciled replacement is complete
- **THEN** the repair replays the deltas from its start checkpoints, persists
  the target checkpoints, folds the WAL and publishes under the same guards and
  retries as a whole-corpus build
