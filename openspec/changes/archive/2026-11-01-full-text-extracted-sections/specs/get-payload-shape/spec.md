# Extracted text windows delta

## ADDED Requirements

### Requirement: Long extracted text reads in bounded windows

`read_memory` SHALL accept an optional `offset`, a character offset into a Source's extracted text. For a Source released in full whose extracted text is longer than one window, the response SHALL carry the Source's path, title, stable identifier and metadata, and one window of the extracted text from `offset`, 0 by default, as `body`. The window SHALL be at most 32 KiB of characters and SHALL end at a paragraph or line break. The response SHALL carry `window` with `offset`, `next_offset`, `total_chars`, `part` and `parts`. `next_offset` SHALL be greater than `offset`, and null at the end of the text. Offsets SHALL be measured from the current content of the Source page and its parts. So following `next_offset` from 0 to null SHALL return the whole extracted text as it is during the walk, in order, exactly. A window SHALL withhold what the read withholds from the caller. A window SHALL carry `body_truncated: true` and no `content_hash` or `evidence_version`, so it cannot guard a write of the whole page. A read of a part page SHALL return the same response under its Source page's identity, under the access decision of the Source page. An offset that is not a whole number, an offset outside the text, or an offset on a page with no long extracted text SHALL be refused. A page that is not a Source with long extracted text SHALL read as before.

#### Scenario: Walking the windows returns the whole text
- **WHEN** a caller reads a multi-megabyte Source from offset 0 and follows `next_offset` until it is null
- **THEN** the joined windows equal the extracted text exactly

#### Scenario: A walk after an edit reads the text as it is now
- **WHEN** the Source page is edited and a caller then walks its windows
- **THEN** every `next_offset` is past its `offset`
- **AND** the joined windows equal the extracted text as it is after the edit

#### Scenario: A window cannot guard a write
- **WHEN** a caller reads one window of a long Source
- **THEN** the response carries `body_truncated: true` and no `content_hash`

#### Scenario: A part page reads as its Source
- **WHEN** a caller reads the path of a part page
- **THEN** the response carries the Source page's path and stable identifier
