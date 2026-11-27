## ADDED Requirements

### Requirement: Records import reads one table of a preserved Markdown page
`records.import` SHALL accept `format: "markdown-table"` and read the one table of the page that `source_ref` names which `mapping.table` picks, as a 0-based index in document order, default 0. Header cells SHALL be the column names and `mapping` SHALL map them by the csv rules. `members` SHALL be refused for this format.

#### Scenario: One table of a page with two tables imports
- **WHEN** the owner imports table 1 of a preserved page whose first table is an index, with a mapping that names the second table's columns
- **THEN** the job reads only the second table's rows, commits them citing the Evidence file, and resumes across batches without losing or repeating a row

#### Scenario: A short row reads the missing cells as empty
- **WHEN** a row has fewer cells than the header
- **THEN** the page is read as GitHub-flavoured Markdown reads it, and the missing cells are empty

#### Scenario: A table the page cannot give stops the read
- **WHEN** preview names `table: 7` on a page with two tables
- **THEN** preview reports `IMPORT_TABLE_NOT_FOUND` at `mapping.table`, and an unmapped preview lists each table's index, columns and row count in `source.tables`

#### Scenario: A page the reader cannot take stops the read
- **WHEN** the header repeats a name or has an empty cell, or the page is not UTF-8 or exceeds 262,144 bytes
- **THEN** the read stops with a typed code and no row is committed

### Requirement: Markdown import keeps the preserved-source contract
Only a preserved Sources or Evidence file SHALL be a source of `records.import`. A `source_ref` that is not one SHALL be refused with one text, whether the ref is absent, unpreserved or withheld. That text SHALL tell the agent to preserve the file's bytes with `save_evidence` and pass the returned Evidence path as `source_ref`.

#### Scenario: A page that is not preserved is refused with the route that works
- **WHEN** an agent passes a note page's path as `source_ref`
- **THEN** the refusal is the one `IMPORT_SOURCE_NOT_FOUND` text, which names `save_evidence` and `source_ref`, and it does not say whether the path exists

### Requirement: A bad number cell rejects its row
A cell for a `number`, `integer` or `boolean` field of a Markdown table that does not parse SHALL reject its row as `IMPORT_VALUE_INVALID` and SHALL NOT be stored as null. An empty cell SHALL be a missing value.

#### Scenario: An unparseable number rejects its row
- **WHEN** a table's amount cell reads `about 60` and the mapping types the column `number`
- **THEN** that row is rejected `IMPORT_VALUE_INVALID` under `on_invalid: skip`, and no row is stored with a null amount

### Requirement: A bad date cell is missing and keeps its text
A cell for a `date` field of a Markdown table that is not `YYYY-MM-DD` SHALL leave the field missing and SHALL count as unparsed. A string field that the mapping maps to the same column SHALL keep the cell's original text. The `date` cell type SHALL exist for `markdown-table` only, and csv behaviour SHALL NOT change.

#### Scenario: A lost date keeps its original text
- **WHEN** a date cell reads `2025-??-??`, and the mapping types the column `date` and also maps it to a string field
- **THEN** the row imports with no date value, and the string field holds `2025-??-??`

### Requirement: Preview counts the cells of each mapped column
A `markdown-table` preview SHALL report, per mapped column, the cells that parsed, were missing, were unparsed as a date and were rejected, and SHALL name the row, column and original text of each rejected cell within the first 20 of its code. The counts SHALL be `null` unless `coverage` is `full`; a partial read SHALL report its counts only as `partial_counts`.

#### Scenario: Preview names the rejected cell
- **WHEN** preview reads a five-row ledger table whose one amount cell does not parse
- **THEN** it reports that cell's row, column and original text, and the parsed, missing, unparsed and rejected cells of every mapped column

#### Scenario: A partial read gives no column counts
- **WHEN** the preview budget ends the read before the table's last row
- **THEN** the per-column counts are `null` and appear only as `partial_counts`

### Requirement: Preview checks a sum over the rows it would import
`preview` SHALL accept `check_sum: {field, group_by?}` and report the sum of that `number` or `integer` target field over the rows it counts as importing, per `group_by` value when given, with the row count, the rows that carry no number and the group count. A row that a later row of the same source replaces SHALL NOT count twice. The report SHALL be `null` unless `coverage` is `full`. Other modes SHALL refuse `check_sum`.

#### Scenario: Preview reports the total a person can check
- **WHEN** preview reads a five-row ledger table whose one amount cell does not parse, with `check_sum: {field: "amount", group_by: "counterparty"}`
- **THEN** it reports four rows and the amount's total and per-counterparty sums over those four rows

#### Scenario: A field that is not a number is refused
- **WHEN** `check_sum.field` names a string field or a name the collection lacks, or `group_by` names a name the collection lacks
- **THEN** preview refuses with `IMPORT_REQUEST_INVALID` naming the accepted fields

### Requirement: Read instructions teach the records import contract
`read_instructions(topic="records.import")` SHALL return the contract of `records.import`: its modes, the `source_ref` rule and the `save_evidence` route, every format including `markdown-table`, `table`, `mapping`, `check_sum`, `reimport`, `continuation` for status and cancel, the states and limits, and a worked preview call and start call. The contract SHALL come from the importer's own contract text.

#### Scenario: An agent learns the Markdown route from one read
- **WHEN** an agent reads the `records.import` topic
- **THEN** the reply names `markdown-table`, `table`, `check_sum`, `source_ref` and `save_evidence`, and the taught preview call runs against a preserved page without a mapping finding
