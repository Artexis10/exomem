## ADDED Requirements

### Requirement: Markdown Semantic Block Parsing

The system SHALL parse semantic blocks from ordinary ATX Markdown headings whose
normalized heading text matches a supported block type. The supported block
types SHALL include `claim`, `finding`, `evidence`, `decision`, `assumption`,
`inference`, `constraint`, `risk`, `open_question`, `hypothesis`, `result`,
`metric`, `failure`, `pattern`, `record`, `case`, `timeline_event`,
`requirement`, `action`, `definition`, and `procedure`.

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

### Requirement: Plain Metadata And Typed Relations

The system SHALL parse optional leading metadata bullets under a semantic block
heading using `- key: value` syntax and SHALL parse relation metadata from a
`relations` key containing comma-separated `relation: target` entries. Relation
names SHALL be limited to `supports`, `contradicts`, `refines`, `supersedes`,
`derived_from`, `depends_on`, `evidenced_by`, `used_for`, `mitigates`,
`causes`, `blocks`, `resolves`, `cites`, `implements`, `tests`, and `owns`.

#### Scenario: Relation metadata is parsed

- **WHEN** a semantic block contains
  `- relations: supports: [[A]], evidenced_by: [[Source]]`
- **THEN** the parser returns two relations named `supports` and `evidenced_by`
  with their Markdown targets preserved

#### Scenario: Invalid relation names fail validation

- **WHEN** a semantic block contains `- relations: agrees_with: [[A]]`
- **THEN** validation reports an error for unsupported relation `agrees_with`

#### Scenario: Malformed relation entries fail validation

- **WHEN** a semantic block contains `- relations: supports [[A]]`
- **THEN** validation reports a malformed relation entry error

### Requirement: Markdown Compatibility

The system SHALL keep semantic block syntax compatible with plain Markdown. The
parser MUST ignore headings and metadata-looking text inside fenced code blocks,
MUST preserve non-metadata body text as Markdown, and MUST NOT require custom
fences, directives, comments, or inline markers.

#### Scenario: Fenced code is ignored

- **WHEN** a fenced code block contains a line `## Claim`
- **THEN** that line is not parsed as a semantic block heading

#### Scenario: Body remains Markdown

- **WHEN** a semantic block body contains paragraphs, bullets, and wikilinks
- **THEN** those body lines are preserved as the block body after leading
  metadata bullets are removed

### Requirement: Validation Result Shape

The system SHALL expose validation results with separate `errors` and
`warnings`. Duplicate semantic block IDs SHALL be warnings, while unsupported
relations and malformed relation entries SHALL be errors.

#### Scenario: Duplicate IDs warn without blocking parsing

- **WHEN** two semantic blocks use `- id: same-id`
- **THEN** validation returns a duplicate ID warning
- **AND** the parsed blocks remain available to callers

### Requirement: Claim And Context Pack Reuse

The system SHALL let existing claim extraction prefer parsed semantic `claim`
blocks and SHALL let context pack assembly include parsed semantic blocks when
present, without changing existing find ordering, pack caps, or mutation
behavior.

#### Scenario: Claim extraction prefers semantic claim block

- **WHEN** a note has a semantic `## Claim` block and another legacy claim-like
  section later in the note
- **THEN** claim extraction uses the semantic claim block body first

#### Scenario: Context packs include semantic blocks additively

- **WHEN** `assemble_pack` packs a page containing semantic blocks
- **THEN** the returned pack includes semantic block data for that page
- **AND** the existing `claims`, `neighborhood`, `contradictions`,
  `embeddings_available`, and `truncation` fields remain present
