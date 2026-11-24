# records

## ADDED Requirements

### Requirement: Row creation returns the pages about the row's subject

A Records write that creates rows SHALL return `related_pages` for each new row that has at least one match. The list SHALL hold the visible entity and note pages whose title or alias matches the row's natural-key values or its presentation title field. It SHALL hold at most three pages per row and 30 per reply, each with its title and canonical ref. An update of an existing row SHALL NOT compute it. A Planning write SHALL NOT compute it, because a plan item names work, not a subject.

#### Scenario: Seeding a service row shows the pages about the service

- **WHEN** an agent appends a row whose natural key names a service, and three visible pages are about that service
- **THEN** the reply lists those pages under `related_pages` for that row

### Requirement: Related pages are batched, visible-only and judgement-free

A bulk upsert SHALL resolve all its new rows' related pages in one batched lookup, never one per row. A withheld page SHALL NOT appear, and its absence SHALL read exactly as no match. The server SHALL NOT decide whether a written value means "unknown". An import job's receipt reports the job, not each batch, so it SHALL NOT carry `related_pages`.

#### Scenario: A bulk seed resolves in one lookup

- **WHEN** a bulk upsert creates 40 rows
- **THEN** one batched lookup computes every row's `related_pages`, and the reply holds at most 30 pages in total

### Requirement: An unreadable page index is reported, not read as no pages

The lookup SHALL read the page index as stored and never refresh it. When that index cannot be read, the reply SHALL carry a `RELATED_PAGES_UNAVAILABLE` warning instead of an empty list.

#### Scenario: A never-built index is reported

- **WHEN** an agent appends a row while the page index has not been built
- **THEN** the reply carries a `RELATED_PAGES_UNAVAILABLE` warning and no `related_pages`
