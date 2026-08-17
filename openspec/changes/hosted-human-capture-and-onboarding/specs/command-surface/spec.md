## ADDED Requirements

### Requirement: Hosted Refusals Carry Actionable Guidance

A refusal returned across the hosted cell boundary SHALL carry a message describing the
refusal and, where one is defined for its code, a remediation string describing what would
make the request succeed. It MUST NOT report `remediation: null` for a code that has a
defined remediation.

The boundary redacts exception-derived text, and that guarantee is unchanged: the message
and remediation SHALL be resolved from a static table keyed on the refusal code, never
copied from the raised exception. A code with no table entry SHALL degrade to the existing
generic message rather than passing exception text through.

Where the semantic authoring contract already defines remediation for a code, the hosted
entry SHALL be derived from that definition rather than duplicating its text, so the two
cannot drift.

#### Scenario: A blocked write reports remediation the caller can act on

- **WHEN** a hosted write is refused because the page has no valid semantic unit
- **THEN** the response carries the code `missing_semantic_unit`
- **AND** the response carries a non-null remediation describing what to add
- **AND** the message is specific to the refusal rather than the generic hosted-failure text

#### Scenario: A relation-disposition refusal is equally actionable

- **WHEN** a hosted write is refused because the page needs a qualifying relation or an
  explicit current review
- **THEN** the response carries a non-null remediation

#### Scenario: An unrecognised failure stays redacted

- **WHEN** a hosted request fails with a code that has no table entry
- **THEN** the response carries the generic hosted-failure message and `remediation: null`
- **AND** no text derived from the raised exception appears in the response

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
