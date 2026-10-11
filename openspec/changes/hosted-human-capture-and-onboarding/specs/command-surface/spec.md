## ADDED Requirements

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
