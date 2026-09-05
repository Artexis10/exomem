## ADDED Requirements

### Requirement: The dispositions view carries the envelope beside the families

The dispositions review mode SHALL list the envelope's action classes in a
block structurally separate from the signal-family rows, so the two
vocabularies never share a column and the word `off` is never ambiguous
between them: a family `off` is annotated review-state, an envelope `off` is
"the agent does not initiate this class". Each envelope row SHALL carry the
class, its ceiling, its disposition or governance-owned marker, and whether the
disposition is fixed, derived, or overridden. If landing this changes a
recorded response contract or moves the packaged tool-surface digest, the
documented two-phase rollout SHALL be followed; no tool schema changes.

#### Scenario: One view, two clearly separated vocabularies

- **WHEN** the dispositions view is read while one family is `quiet` and one
  envelope class is overridden
- **THEN** the family appears in the family block with its review-state
  disposition, the class appears in the envelope block with its ceiling and
  override marker, and neither block contains rows of the other kind
