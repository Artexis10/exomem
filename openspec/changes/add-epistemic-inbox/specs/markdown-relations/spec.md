## ADDED Requirements

### Requirement: Canonical note-level Markdown relations
The system SHALL recognize a canonical `## Relations` section whose list items have the form `- relation_type [[Target]]`, where `relation_type` is lower `snake_case`, the target is one wikilink, and each item represents a directional note-to-note edge. The Markdown SHALL remain valid and useful in Obsidian without Exomem.

#### Scenario: Canonical relation parses
- **WHEN** a note contains `- depends_on [[Architecture Decision]]` under `## Relations`
- **THEN** the parser returns one `depends_on` relation to that target with its source line
- **AND** Obsidian can still render the wikilink normally

#### Scenario: Free-form connection remains generic
- **WHEN** a note contains an inline wikilink or prose such as `This follows [[Earlier Work]]`
- **THEN** it remains a generic `links_to` connection rather than fabricating a typed relation

### Requirement: Governed relation vocabulary and validation
Note-level and semantic-block relations SHALL share one canonical relation vocabulary. Relation labels outside that vocabulary or malformed relation bullets SHALL produce deterministic validation feedback and SHALL NOT become typed graph edges. The vocabulary SHALL include Exomem's existing epistemic relations plus `relates_to`.

#### Scenario: Unknown relation does not fragment the graph
- **WHEN** a canonical Relations section contains `- dependson [[Target]]`
- **THEN** validation reports an unsupported relation with the line number
- **AND** no `dependson` typed edge is indexed

### Requirement: Typed graph indexing without redundant generic edges
The derived epistemic graph SHALL index each valid note-level relation with its declared type, source path, and line anchor. For a canonical relation bullet, it SHALL NOT also emit a redundant `links_to` edge for the same source and target; unrelated inline wikilinks SHALL continue to emit `links_to`.

#### Scenario: Typed relation wins over generic relation
- **WHEN** a page contains one canonical `refines` relation and one ordinary inline wikilink
- **THEN** graph context contains the `refines` edge for the canonical bullet and a `links_to` edge for the inline link
- **AND** it does not contain a second `links_to` edge for the canonical bullet

### Requirement: Block-level epistemic precision remains available
The existing semantic-block metadata syntax (`- relations: kind: [[Target]]`) SHALL continue to attach relations to claim/finding/evidence block nodes and SHALL use the same relation vocabulary as note-level relations.

#### Scenario: Note and block relations coexist
- **WHEN** a note has a note-level `depends_on` relation and a Finding block with `evidenced_by` metadata
- **THEN** the graph contains the note-to-note edge and the block-to-target edge with their distinct source anchors

### Requirement: Authoring feedback exposes relation quality
Compiled-note writes SHALL report counts for typed note relations, typed block relations, generic wikilinks, malformed relations, and unresolved targets. The portable agent contract SHALL direct agents to search and propose relations before writing, accept only meaningful edges, and use canonical `## Relations` syntax for note-level connections.

#### Scenario: Linkless write receives actionable feedback
- **WHEN** an agent writes an active compiled note with no source links, typed relations, or generic wikilinks
- **THEN** write feedback identifies relation debt and recommends `connect_memory` relation/link suggestions
- **AND** the write still succeeds because relation debt is reviewable, not a hard schema failure
