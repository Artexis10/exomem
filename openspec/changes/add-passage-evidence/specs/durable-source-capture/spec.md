## ADDED Requirements

### Requirement: A capture may declare passages with caller keys

`capture_source` SHALL accept optional `passages`: an ordered list of objects, each with `key` and `text`.
`passages` and `content` SHALL exclude each other.
The capture SHALL render the Source body as the passage texts joined by one blank line, so that the Source reads as an ordinary text capture.
The capture SHALL store each passage's text byte for byte as the caller sent it.
The capture SHALL refuse, before any write:

- more than 2,000 passages;
- a key that is not a string of 1 to 128 bytes as UTF-8, or that repeats within the capture;
- passage text that is empty, contains a blank line or a carriage return, or has white space at its start or end.

Each refusal SHALL name the passage ordinal, the field and the limit or the correction.

#### Scenario: Passages replace content

- **WHEN** a caller supplies `passages` and no `content`
- **THEN** the capture creates one Source whose body holds the passage texts in order

#### Scenario: Both inputs are refused

- **WHEN** a caller supplies `passages` and `content` together
- **THEN** the capture refuses before any write and names both fields

#### Scenario: A repeated key is refused

- **WHEN** two passages of one capture have the same key
- **THEN** the capture refuses before any write
- **AND** the error names both passage ordinals and the field

#### Scenario: Text that capture would have to change is refused

- **WHEN** one passage's text ends with white space
- **THEN** the capture refuses before any write
- **AND** the error names the passage ordinal and tells the caller to trim the text

### Requirement: Declared passages are original authored inputs

The capture input digest SHALL cover each declared passage's key and text, in order.
A repeated capture with the same identity and the same passages SHALL reuse the Source.
A repeated capture with the same identity and different keys or texts SHALL be the same conflict as different text.

#### Scenario: A retry reuses the Source

- **WHEN** a caller repeats a capture with the same identity, keys and texts
- **THEN** the capture returns the existing Source with `capture_outcome: reused`

#### Scenario: A changed key conflicts

- **WHEN** a caller repeats a capture with the same identity and one changed key
- **THEN** the capture reports the identity conflict and changes nothing
