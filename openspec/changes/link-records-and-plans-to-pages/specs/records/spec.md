# records

## ADDED Requirements

### Requirement: Row creation returns the pages about the row's subject

A Records write that creates rows SHALL return `related_pages` for each new row. The list SHALL hold the visible entity and note pages whose title or entity alias matches the row's natural-key values or its presentation title field. It SHALL hold at most three pages per row and 30 per reply, each with its title and canonical ref. An update of an existing row SHALL NOT compute it.

#### Scenario: Seeding a service row shows the pages about the service

- **WHEN** an agent appends a row whose natural key names a service, and three visible pages are about that service
- **THEN** the reply lists those pages under `related_pages` for that row

### Requirement: Related pages are batched, visible-only and judgement-free

A bulk upsert or import SHALL resolve all its new rows' related pages in one batched lookup, never one per row. A withheld page SHALL NOT appear, and its absence SHALL read exactly as no match. The server SHALL NOT decide whether a written value means "unknown".

#### Scenario: A bulk seed resolves in one lookup

- **WHEN** a bulk upsert creates 40 rows
- **THEN** one batched lookup computes every row's `related_pages`, and the reply holds at most 30 pages in total
