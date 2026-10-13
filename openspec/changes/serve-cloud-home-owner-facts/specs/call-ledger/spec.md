# Spec Delta

## ADDED Requirements

### Requirement: Rows Record Access And Item Count Without Content

Ledger rows SHALL use schema version 2 and carry `access` and `item_count`.
`access` SHALL be `read` or `write` as the dispatcher classified the invocation, or null when no classification reached the row.
For a successful read, `item_count` SHALL be the number of distinct items the call's disclosure boundary released, whatever the governance policy.
For a successful write, `item_count` SHALL be the number of distinct pages the commit reported, or, when it reported none, the number of distinct targets the call addressed. It SHALL be null when neither names a page.
Targets SHALL be compared whole; a bound on the paths a row shows MUST NOT merge distinct targets.
For any other outcome, `item_count` SHALL be null. A null count MUST NOT be written as zero.
Both fields SHALL survive content-private logging, because neither carries content.

#### Scenario: A recall records what it released

- **WHEN** a recall on a vault with an empty policy returns five distinct notes
- **THEN** its row records `access: read` and `item_count: 5`

#### Scenario: A save whose destination the server chose

- **WHEN** a write commits a note at a path that the server derived, such as `remember`
- **THEN** its row records `access: write` and `item_count: 1`

#### Scenario: A write that names its page by reference

- **WHEN** an edit addresses a page by its `exomem://` reference and commits that page
- **THEN** its row records `item_count: 1`

#### Scenario: A write that names no page

- **WHEN** a write succeeds whose arguments name no page and whose committed result names none
- **THEN** its row records `access: write` and a null `item_count`

### Requirement: Version 1 Rows Stay Readable And Verifiable

The ledger SHALL keep every version 1 row exactly as written.
`verify` SHALL accept a chain that runs from version 1 rows into version 2 rows.
Every reader SHALL treat the missing `access` and `item_count` of a version 1 row as unknown, and MUST NOT reject the row.

#### Scenario: An upgraded ledger verifies

- **WHEN** a version 2 writer appends to a ledger that holds version 1 rows
- **THEN** its first row chains on the last version 1 row
- **AND** the anchored verification reports no problem

### Requirement: A Failed Append Leaves A Coverage Boundary

When the ledger cannot append a call's row, the call SHALL still succeed.
The failing process SHALL keep the latest failure time in memory, and SHALL mark it beside the ledger when it can.
Every reader of recent activity SHALL treat the latest such time as a boundary, and MUST NOT claim an interval that starts at or before it.

#### Scenario: A recall the ledger could not record

- **WHEN** a recall succeeds but its row cannot be appended
- **THEN** the next activity interval starts strictly after that recall, or the activity section is unavailable
