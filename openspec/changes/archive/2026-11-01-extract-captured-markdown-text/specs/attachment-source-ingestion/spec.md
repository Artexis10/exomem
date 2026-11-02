## ADDED Requirements

### Requirement: A Captured Markdown Original Has Searchable Text

A captured original with the suffix `.md` SHALL be stored with its exact bytes under the suffix `.md.txt`, outside the graph input set. Its Source page SHALL declare the `text` media type and mark extraction as pending, so that its text is extracted in the encoding its bytes show.

#### Scenario: A captured UTF-16 Markdown file

- **WHEN** a client captures a `.md` original that holds UTF-16 text with a byte-order mark
- **THEN** the stored artifact's bytes equal the original and its name ends with `.md.txt`
- **AND** its Source page declares the `text` media type with extraction pending
- **AND** the graph does not read the artifact as a page
