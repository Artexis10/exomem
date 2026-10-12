# Spec Delta

## ADDED Requirements

### Requirement: Rows Record Access And Item Count Without Content

Ledger rows SHALL use schema version 2 and carry `access` and `item_count`.
`access` SHALL be `read` or `write` as the dispatcher classified the invocation, or null when no classification reached the row.
For a successful read, `item_count` SHALL be the number of distinct items the call's disclosure boundary released, whatever the governance policy.
For a successful write, `item_count` SHALL be the number of distinct targets the call addressed or reported committing, or null when none is known.
For any other outcome, `item_count` SHALL be null. A null count MUST NOT be written as zero.
Both fields SHALL survive content-private logging, because neither carries content.

#### Scenario: A recall records what it released

- **WHEN** a recall on a vault with an empty policy returns five distinct notes
- **THEN** its row records `access: read` and `item_count: 5`

#### Scenario: A write of unreported destination

- **WHEN** a write succeeds whose destination the server derived and did not report
- **THEN** its row records `access: write` and a null `item_count`

### Requirement: Version 1 Rows Stay Readable And Verifiable

The ledger SHALL keep every version 1 row exactly as written.
`verify` SHALL accept a chain that runs from version 1 rows into version 2 rows.
Every reader SHALL treat the missing `access` and `item_count` of a version 1 row as unknown, and MUST NOT reject the row.

#### Scenario: An upgraded ledger verifies

- **WHEN** a version 2 writer appends to a ledger that holds version 1 rows
- **THEN** its first row chains on the last version 1 row
- **AND** the anchored verification reports no problem
