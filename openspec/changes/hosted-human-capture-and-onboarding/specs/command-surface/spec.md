## ADDED Requirements

### Requirement: Contract Failures Keep Their Code And Remediation Across Surfaces

A semantic-contract failure carries a machine-readable code and, where the evaluator produced one, a
remediation string describing what would make the write succeed. The command surface SHALL preserve
both to the caller. It MUST NOT flatten a structured contract error into a message-only exception,
because a downstream translator that inspects `.code` will then fall through to a generic code and
report `remediation: null` for every distinct cause.

Where a stable public code is required at a boundary, the surface SHALL derive it from the original
error rather than substituting a catch-all, and SHALL carry the remediation alongside it.

#### Scenario: A blocked write reports the specific cause

- **WHEN** a write is refused because the page has no valid semantic unit
- **THEN** the caller receives the code `missing_semantic_unit`, not a generic creation-failed code
- **AND** the response carries the evaluator's remediation text
- **AND** the same holds for a refusal caused by a missing relation disposition, with its own
  distinct code

#### Scenario: An unrecognised failure stays honest

- **WHEN** a write fails for a reason the evaluator did not classify
- **THEN** the caller receives the generic code
- **AND** the response states that no remediation is available rather than implying none was produced

### Requirement: Human Capture Is Served By The Capture Lane

Exomem SHALL provide a lane for raw human capture that accepts ordinary prose without requiring
semantic units, typed relations, or a reviewed relation disposition, and material written to it SHALL
be retrievable by ordinary recall. `capture_source` is that lane.

The governed-conclusion lane (`remember`) SHALL continue to enforce the semantic contract unchanged.
A conclusion that other conclusions may cite, supersede, or contradict must be well-formed; this
requirement does not relax that, it prevents the contract being applied to material that never
claimed to be a conclusion.

Surfaces offering an unstructured "save this thought" affordance to a human SHALL route it to the
capture lane.

#### Scenario: Consecutive ordinary sentences all save

- **WHEN** a person saves three ordinary sentences in a row through the capture lane, none containing
  a heading or a semantic unit
- **THEN** all three succeed
- **AND** each is retrievable by keyword recall afterwards

#### Scenario: The governed lane is unchanged

- **WHEN** a second governed conclusion is written with no qualifying typed relation and no reviewed
  disposition
- **THEN** it is still refused, with its specific code and remediation
