## MODIFIED Requirements

### Requirement: A hit on a long Source is one hit with its matched passage

A search hit on a Source whose extracted text continues on part pages SHALL be one hit on the Source page, however many of its parts match. The hit SHALL name the best-ranked part as `matched_part` and SHALL carry `passage` with `text`, `part`, `offset` and `length`. `text` SHALL be the matched passage, with a short window of surrounding text, and SHALL withhold what the excerpt of the hit withholds from the caller. `offset` SHALL be the character offset of the start of the passage in the whole stored extracted text, and `length` SHALL count the stored text of the passage. The passage SHALL be the matched embedding chunk for a semantic hit, else `MAX_WORDS_PER_CHUNK` words around the first keyword match. A passage SHALL show at most `MAX_PASSAGE_CHARS` characters; a longer one SHALL be cut to that length so that it keeps the first place where the query matches in it, opening one context window before that place unless the passage ends sooner, and `offset` and `length` SHALL describe the passage as shown. A part page whose Source page is not admitted SHALL NOT appear as a hit. A part page past the number of parts that its Source page lists SHALL NOT fold into a hit.

#### Scenario: A phrase near the end of a long document
- **WHEN** a unique phrase is in the last part of a multi-megabyte document and a search names it
- **THEN** exactly one hit is returned, on the Source page
- **AND** `matched_part` is the last part and `passage.text` contains the phrase

#### Scenario: The offset opens the passage
- **WHEN** `read_memory` is called on the hit with `offset` set to `passage.offset`
- **THEN** the returned window contains `passage.text`

#### Scenario: A log without word breaks
- **WHEN** a Source holds a log with one compact JSON record per line and a search names a phrase on one of its lines
- **THEN** `passage.text` holds at most `MAX_PASSAGE_CHARS` characters and contains the phrase
- **AND** `read_memory` at `passage.offset` returns a window that contains the phrase
