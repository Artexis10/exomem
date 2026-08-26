## MODIFIED Requirements

### Requirement: Markdown Semantic Block Parsing

The system SHALL parse semantic blocks from ordinary ATX Markdown headings at
level 2 or deeper whose normalized heading text matches a supported block type.
A level-1 heading is the page title and SHALL NOT start a semantic block,
whatever its text, because a level-1 block has no closing heading and would
absorb every block on the page. The supported block types SHALL include `claim`,
`finding`, `evidence`, `decision`, `assumption`, `inference`, `constraint`,
`risk`, `open_question`, `hypothesis`, `result`, `metric`, `failure`, `pattern`,
`record`, `case`, `timeline_event`, `requirement`, `action`, `definition`, and
`procedure`.

#### Scenario: Required block types parse from headings

- **WHEN** a Markdown page contains headings such as `## Claim`,
  `## Open Question`, and `## Timeline Event`
- **THEN** the parser returns semantic blocks with normalized types `claim`,
  `open_question`, and `timeline_event`
- **AND** each block preserves its heading title, heading level, source line,
  and Markdown body

#### Scenario: Unknown headings stay ordinary Markdown

- **WHEN** a Markdown page contains `## Background` and no supported semantic
  heading at that section
- **THEN** the parser does not emit a semantic block for `Background`
- **AND** validation does not report an error for that ordinary heading

#### Scenario: A title that names a block type does not swallow the page

- **WHEN** a page is titled `# Source`, `# Decision`, or `# Open Question` and contains a `## Decision` section
- **THEN** the parser emits the `decision` block for that section
- **AND** emits no block for the title heading

#### Scenario: A title heading still closes an open block

- **WHEN** a `## Claim` section is followed later in the file by a level-1 heading
- **THEN** the claim block ends at that heading rather than continuing past it
