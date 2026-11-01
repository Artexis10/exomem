# Extracted text windows delta

## ADDED Requirements

### Requirement: Long extracted text reads in bounded windows

`read_memory` SHALL accept an optional `offset`, a character offset into a Source's extracted text. For a Source released in full whose extracted text is longer than one window, the response SHALL carry the Source's path, title, stable identifier and metadata, and one window of the extracted text from `offset`, 0 by default, as `body`. The window SHALL be at most 32 KiB of characters and SHALL end at a paragraph or line break. The response SHALL carry `window` with `offset`, `next_offset`, `total_chars`, `part` and `parts`, and `next_offset` SHALL be null at the end of the text. Following `next_offset` from 0 to null SHALL return the whole extracted text in order, exactly. A read of a part page SHALL return the same response under its Source page's identity, under the access decision of the Source page. An offset outside the text, or on a page with no long extracted text, SHALL be refused. A page that is not a Source with long extracted text SHALL read as before.

#### Scenario: Walking the windows returns the whole text
- **WHEN** a caller reads a multi-megabyte Source from offset 0 and follows `next_offset` until it is null
- **THEN** the joined windows equal the extracted text exactly

#### Scenario: A part page reads as its Source
- **WHEN** a caller reads the path of a part page
- **THEN** the response carries the Source page's path and stable identifier
