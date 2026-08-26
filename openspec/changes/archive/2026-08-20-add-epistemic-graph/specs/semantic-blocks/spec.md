## ADDED Requirements

### Requirement: Structural Semantic Block Extraction
The system SHALL extract semantic block nodes from ordinary Markdown files using deterministic structure, including frontmatter, page type, recognized headings, recognized list labels, wikilinks, and existing media metadata. The initial block kind vocabulary SHALL include `source`, `evidence`, `claim`, `finding`, `decision`, `assumption`, `constraint`, `risk`, `failure`, `experiment`, `result`, `pattern`, `requirement`, `action`, `entity`, `project`, `case`, `timeline_event`, and `media_segment`.

#### Scenario: Recognized sections become typed blocks
- **WHEN** a Markdown note contains frontmatter plus sections named `## Findings`, `## Decision`, `## Risks`, and `## Actions`
- **THEN** semantic block extraction returns `finding`, `decision`, `risk`, and `action` block nodes derived from those sections
- **AND** extraction does not rewrite, move, or delete the source Markdown file

#### Scenario: Existing page type contributes a block kind
- **WHEN** a governed note has frontmatter `type: pattern`
- **THEN** semantic block extraction returns a `pattern` block for the page-level knowledge unit
- **AND** the block remains tied to the source path that produced it

### Requirement: Source-Spanned Block Identity
Each semantic block SHALL carry enough provenance to locate its origin: vault-relative source path, stable block key, block kind, heading or anchor when available, source text excerpt, and a source span or deterministic line-range equivalent when available. The block key SHALL change when the source path or block text identity changes, and SHALL remain stable across repeated extraction of unchanged content.

#### Scenario: Repeated extraction is stable
- **WHEN** semantic block extraction runs twice over unchanged Markdown
- **THEN** the same semantic blocks are returned with the same stable block keys
- **AND** their source paths and anchors still point to the originating note

#### Scenario: Edited block changes identity
- **WHEN** the text of a semantic block changes materially
- **THEN** the extracted block for that content receives a different content identity or source hash
- **AND** stale graph rows for the prior block can be removed during reindex

### Requirement: Unknown Markdown Degrades To Searchable Content
The system SHALL NOT require users to adopt a new authoring syntax before their files remain searchable and indexable. Unrecognized headings, unknown list labels, malformed optional relation syntax, and ordinary prose SHALL be ignored by semantic block typing rather than treated as extraction failures.

#### Scenario: Unknown heading does not fail extraction
- **WHEN** a Markdown note contains `## Strange Local Heading` with prose below it
- **THEN** semantic block extraction completes successfully
- **AND** the prose remains part of the page content available to existing search paths

#### Scenario: Malformed optional relation text is ignored
- **WHEN** a note contains text that resembles an optional relation but cannot be parsed safely
- **THEN** semantic block extraction does not raise an error
- **AND** no typed relation is produced from that malformed text

### Requirement: Optional Model-Backed Block Suggestions Are Default-Off
Any model-backed block classification or extraction path SHALL be disabled by default, SHALL soft-fail when its optional dependencies or models are unavailable, and SHALL return suggested blocks only as proposal output. It MUST NOT mutate Markdown, write durable accepted graph facts, or invoke a generative reasoning model to author note content.

#### Scenario: Default extraction loads no optional model
- **WHEN** semantic block extraction runs with default configuration
- **THEN** it uses deterministic parsing only
- **AND** no optional model dependency is imported or required

#### Scenario: Missing optional model soft-fails
- **WHEN** model-backed block suggestion is explicitly requested but the configured model is unavailable
- **THEN** the operation returns deterministic block output plus an availability warning
- **AND** the source Markdown and graph accepted facts remain unchanged
