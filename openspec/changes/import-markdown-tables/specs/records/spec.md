## ADDED Requirements

### Requirement: Records import reads one table of a preserved Markdown page
`records.import` SHALL accept `format: "markdown-table"` and read the one table of the page that `source_ref` names which `mapping.table` picks, as a 0-based index in document order, default 0. Header cells SHALL be the column names and `mapping` SHALL map them by the csv rules. `members` SHALL be refused for this format. A derived target collection SHALL be refused at the first request, with a text that names csv or ndjson with `members`.

#### Scenario: One table of a page with two tables imports
- **WHEN** the owner imports table 1 of a preserved page whose first table is an index, with a mapping that names the second table's columns
- **THEN** the job reads only the second table's rows, commits them citing the Evidence file, and resumes across batches without losing or repeating a row

#### Scenario: A short row reads the missing cells as empty
- **WHEN** a row has fewer cells than the header
- **THEN** the page is read as GitHub-flavoured Markdown reads it, and the missing cells are empty

#### Scenario: A derived collection is refused once with the way out
- **WHEN** an agent previews a `markdown-table` import into a derived collection without `members`
- **THEN** the refusal says this format cannot import into a derived collection and names csv or ndjson with `members`, so the agent is not sent to add `members` and refused again

#### Scenario: A table the page cannot give stops the read
- **WHEN** preview names `table: 7` on a page with two tables
- **THEN** preview reports `IMPORT_TABLE_NOT_FOUND` at `mapping.table`, and an unmapped preview lists the first 20 tables' index, columns and row count in `source.tables` and the page's table count in `source.tables_total`

#### Scenario: A page the reader cannot take stops the read
- **WHEN** the header repeats a name or has an empty cell, or the page is not UTF-8
- **THEN** the read stops with a typed code and no row is committed, and a header fault names its first bad cell, counted from 0, in `at`

### Requirement: Markdown import parses a page within a bound
The importer SHALL bound the parse of a Markdown page by a byte cap and a token cap, and SHALL NOT charge the parse's cost to a batch's clock. A per-process cache of recent parses is an optimisation and no guarantee. It SHALL refuse a page over 262,144 bytes or 150,000 parser tokens as `IMPORT_SOURCE_TOO_LARGE`, with an `at` that says to import csv. A table SHALL never be read cut short by the parser's autofill limit.

#### Scenario: A slow parse does not shrink the first batch
- **WHEN** a job's first batch parses a page for longer than the batch's clock
- **THEN** that batch still reads its full rows, because the parse's cost is not charged to its clock

#### Scenario: A page too big or too wide is refused with the way out
- **WHEN** a page is over 262,144 bytes, or is a wide sparse table that the parser would cut short
- **THEN** preview reports `IMPORT_SOURCE_TOO_LARGE` with an `at` that names csv, and the table is not read

### Requirement: A Markdown row never loses a cell silently
A `markdown-table` row with text in a cell beyond the header's width SHALL be rejected as `IMPORT_ROW_MALFORMED` in preview and in start, with `at` reading `row has N cells, header has M`. The reader SHALL take the cells beyond the header from the Markdown parser's own cell splitter, inside the row's container, and SHALL NOT count pipes itself. An extra cell with no text SHALL NOT reject its row.

#### Scenario: A row with a fourth cell under a three-column header is rejected
- **WHEN** a page's row has a text cell beyond its three-column header and other rows do not
- **THEN** preview and the job reject that row with its row number and `row has 4 cells, header has 3`, and the other rows import

#### Scenario: An empty extra cell does not reject its row
- **WHEN** a row ends with an extra cell that holds no text
- **THEN** the row imports as it would without that cell

### Requirement: Markdown import keeps the preserved-source contract
Only a preserved Sources or Evidence file SHALL be a source of `records.import`. A `source_ref` that is not one SHALL be refused with one text, whether the ref is absent, unpreserved or withheld. That text SHALL tell the agent to preserve the file's bytes with `save_evidence` and pass the returned Evidence path as `source_ref`.

#### Scenario: A page that is not preserved is refused with the route that works
- **WHEN** an agent passes a note page's path as `source_ref`
- **THEN** the refusal is the one `IMPORT_SOURCE_NOT_FOUND` text, which names `save_evidence` and `source_ref`, and it does not say whether the path exists

### Requirement: A bad number cell rejects its row
A cell for a `number`, `integer` or `boolean` field of a Markdown table that does not parse SHALL reject its row as `IMPORT_VALUE_INVALID` and SHALL NOT be stored as null. A leading `+` or `-` SHALL read as a sign. A currency symbol, a thousands separator or parentheses SHALL reject the row. An empty cell SHALL be a missing value.

#### Scenario: An unparseable number rejects its row
- **WHEN** a table's amount cell reads `about 60`, `$1,200.00` or `(12.00)` and the mapping types the column `number`
- **THEN** that row is rejected `IMPORT_VALUE_INVALID` under `on_invalid: skip`, and no row is stored with a null amount

#### Scenario: A signed amount parses
- **WHEN** amount cells read `+85.30` and `-40.00`
- **THEN** the rows store 85.3 and -40

### Requirement: A bad date cell is missing and keeps its text
A cell for a `date` field of a Markdown table or a csv file that is not `YYYY-MM-DD` SHALL leave the field missing and SHALL count as unparsed. A `scale` on a `date`, `string` or `boolean` cell, a csv string default included, SHALL be refused as `IMPORT_MAPPING_INVALID` when the mapping is read. A string field that the mapping maps to the same column SHALL keep the cell's original text. The `date` cell type SHALL be opt-in: a csv mapping that does not ask for it SHALL behave as before.

#### Scenario: A loosely written date does not cost the row
- **WHEN** a hand-kept ledger writes a date loosely, such as `2025-??-??`, and rejecting the row would drop its amount, which is the measure
- **THEN** the row imports with the date field missing, and after the job commits the owner finds those rows by querying for a missing date

#### Scenario: A lost date keeps its original text
- **WHEN** a date cell reads `2025-??-??`, and the mapping types the column `date` and also maps it to a string field
- **THEN** the row imports with no date value, and the string field holds `2025-??-??`

### Requirement: Preview and status count the cells of each mapped column
A `markdown-table` preview, or a csv preview whose mapping asks for a `date`, SHALL report per mapped column the cells that parsed, were missing, were unparsed as a date and were rejected, and the rows refused before a cell was read (`malformed`). The counts SHALL add up to the rows read, and be `null` unless `coverage` is `full`. A job's status SHALL report the same counts over the rows read so far. Preview SHALL name the first rejected cell of each of the first 20 rejected rows.

#### Scenario: Preview names the rejected cell
- **WHEN** preview reads a ledger table whose amount cells `about 60`, `$1,200.00` and `(12.00)` do not parse
- **THEN** it reports the first rejected cell of each of the first 20 rejected rows, with its row, column and original text, and the parsed, missing, unparsed, rejected and malformed counts of every mapped column

#### Scenario: Status reports the same counts after the job
- **WHEN** a job over that table completes, across several batches
- **THEN** its status reports the same per-column counts as the preview

#### Scenario: A streamed csv export reports the same counts
- **WHEN** a csv import reads an export's members and its mapping asks for a `date`
- **THEN** its status reports the per-column counts that preview reported

#### Scenario: A partial read gives no column counts
- **WHEN** the preview budget ends the read before the table's last row
- **THEN** the per-column counts are `null` and appear only as `partial_counts`

### Requirement: Preview lists the dates it could not read
A preview that reports per-column counts SHALL list in `unparsed_cells` the first 20 cells that were present but not read as a `date`, each with its row, column and original text. The list SHALL be `null` unless `coverage` is `full`.

#### Scenario: Preview names the dates it left missing
- **WHEN** a table's date cells read `2025-??-??` and `? (reported 2026-03-12)` and the mapping types the column `date`
- **THEN** `unparsed_cells` gives each cell's row, column and text

### Requirement: Preview checks a sum over the rows it would import
`preview` SHALL accept `check_sum: {field, group_by?}` and report the sum of that `number` or `integer` target field over the rows it counts as importing, per `group_by` value when given, with the row count, the rows that carry no number, each group's own missing count and the group count. A total or group sum over no number SHALL be `null`, never 0. A replaced row SHALL NOT count twice. The report SHALL be `null` unless `coverage` is `full`. Other modes SHALL refuse `check_sum`.

#### Scenario: Preview reports the total a person can check
- **WHEN** preview reads a five-row ledger table whose one amount cell does not parse, with `check_sum: {field: "amount", group_by: "counterparty"}`
- **THEN** it reports four rows and the amount's total and per-counterparty sums over those four rows

#### Scenario: A group with no number reports null
- **WHEN** every amount cell of one counterparty's rows is empty
- **THEN** that group reports `sum: null` and its own `missing` count, and the other groups' sums are unchanged

#### Scenario: A field that is not a number is refused
- **WHEN** `check_sum.field` names a string field or a name the collection lacks, or `group_by` names a name the collection lacks
- **THEN** preview refuses with `IMPORT_REQUEST_INVALID` naming the accepted fields

### Requirement: Read instructions teach the records import contract
`read_instructions(topic="records.import")` SHALL return the contract of `records.import`: its modes, the `source_ref` rule and the `save_evidence` route, every format including `markdown-table`, `table`, `mapping`, `check_sum`, `reimport`, `continuation` for status and cancel, the states and limits, and a worked preview call and start call. For a partial preview it SHALL say to split the page or export it as csv, then preview again. The contract SHALL come from the importer's own contract text.

#### Scenario: An agent learns the Markdown route from one read
- **WHEN** an agent reads the `records.import` topic
- **THEN** the reply names `markdown-table`, `table`, `check_sum`, `source_ref` and `save_evidence`, and the taught preview call runs against a preserved page without a mapping finding
