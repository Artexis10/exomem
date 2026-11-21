## ADDED Requirements

### Requirement: A page hit's excerpt says what the page holds

When `find` builds a page hit's excerpt from the page's best matching chunk, it SHALL use that chunk only when the chunk
holds at least 40 characters of text outside its Markdown heading lines. A chunk with less SHALL give way to the page's
keyword excerpt. When the page has no keyword excerpt, the chunk SHALL still be served. The excerpt SHALL pass
through the same release projection as before.

#### Scenario: A heading chunk gives way to the keyword excerpt

- **WHEN** a page hit's best chunk is a heading line such as "## Relations" and the page has a keyword excerpt
- **THEN** the hit's excerpt is the keyword excerpt

#### Scenario: A chunk with prose is served

- **WHEN** a page hit's best chunk holds a heading and a sentence of at least 40 characters
- **THEN** the hit's excerpt is that chunk, cut as before

#### Scenario: A chunk with no alternative is still served

- **WHEN** a page hit's best chunk is a heading line and the page has no keyword excerpt
- **THEN** the hit's excerpt is that chunk
