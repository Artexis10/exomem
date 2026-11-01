# Matched passage delta

## ADDED Requirements

### Requirement: A hit on a long Source is one hit with its matched passage

A search hit on a Source whose extracted text continues on part pages SHALL be one hit on the Source page, however many of its parts match. The hit SHALL name the best-ranked part as `matched_part` and SHALL carry `passage` with `text`, `part`, `offset` and `length`. `text` SHALL be the matched passage, with a short window of surrounding text. `offset` SHALL be the character offset of the start of `text` in the whole extracted text. The passage SHALL be the matched embedding chunk for a semantic hit, else `MAX_WORDS_PER_CHUNK` words around the first keyword match. A part page whose Source page is not admitted SHALL NOT appear as a hit.

#### Scenario: A phrase near the end of a long document
- **WHEN** a unique phrase is in the last part of a multi-megabyte document and a search names it
- **THEN** exactly one hit is returned, on the Source page
- **AND** `matched_part` is the last part and `passage.text` contains the phrase

#### Scenario: The offset opens the passage
- **WHEN** `read_memory` is called on the hit with `offset` set to `passage.offset`
- **THEN** the returned window contains `passage.text`

### Requirement: A long paragraph is indexed in full

A paragraph of more than `MAX_WORDS_PER_CHUNK` words SHALL be indexed as consecutive chunks of at most that many words. No word of the paragraph SHALL be left out of semantic search.

#### Scenario: A thousand words without a blank line
- **WHEN** a page holds one paragraph of 1,000 words
- **THEN** it embeds as three chunks, and their words in order are the paragraph's words
