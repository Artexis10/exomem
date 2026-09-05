## MODIFIED Requirements

### Requirement: Block-level epistemic precision remains available
The existing semantic-block metadata syntax (`- relations: kind: [[Target]]`) SHALL continue to attach relations to claim/finding/evidence block nodes and SHALL use the same relation vocabulary as note-level relations. A relation target MAY address a unit by carrying a `#fragment`; when that fragment resolves to exactly one addressable unit on the target page the edge's destination SHALL be that unit, and in every other case the edge SHALL remain page-level and the unresolved fragment SHALL be reported to the author rather than discarded silently.

#### Scenario: Note and block relations coexist
- **WHEN** a note has a note-level `depends_on` relation and a Finding block with `evidenced_by` metadata
- **THEN** the graph contains the note-to-note edge and the block-to-target edge with their distinct source anchors

#### Scenario: A relation target addresses one unit
- **WHEN** a relation target carries a `#fragment` that resolves to exactly one addressable unit on the target page
- **THEN** the edge's destination is that unit's node rather than the target page's
- **AND** the edge's source anchor is unchanged

#### Scenario: A fragment that resolves to no unit or to several
- **WHEN** a relation target's `#fragment` matches no addressable unit on the target page, or matches more than one
- **THEN** the relation still produces the page-level edge it produces today
- **AND** the write reports the unresolvable or ambiguous fragment to the author rather than discarding it silently

#### Scenario: A target with no fragment is unaffected
- **WHEN** a relation target carries no `#fragment`
- **THEN** the resulting edge is identical to the one produced before unit-level destinations existed
