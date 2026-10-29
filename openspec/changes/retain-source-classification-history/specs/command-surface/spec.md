## ADDED Requirements

### Requirement: Source classification events survive later corrections and reversals

Every successful reclassification and reversal SHALL retain its reason, timestamp, and before/after path, kind, and domain in Source frontmatter.
Later operations SHALL preserve retained events, including unchanged classifications.
The undo stack SHALL retain its existing semantics.

#### Scenario: Two corrections and two reversals remain readable

- **WHEN** a Source is corrected twice and reversed twice
- **THEN** its current classification returns to its original state
- **AND** its governed frontmatter read retains all four events and reasons in order
- **AND** its raw bytes, identity, body, and capture provenance remain unchanged

#### Scenario: Review confirms the existing classification

- **WHEN** a reclassification confirms the existing classification
- **THEN** its reason becomes a retained event without adding an undo entry

### Requirement: Classification history preserves available legacy evidence honestly

Before overwriting legacy correction metadata, a correction SHALL retain its existing values once.
History SHALL distinguish retained evidence from complete historical coverage and SHALL NOT invent missing reasons or their attribution.
Malformed history SHALL refuse a correction without changing the Source.

#### Scenario: A legacy undo stack has only one known reason

- **WHEN** a Source with earlier classifications and one stored reason receives its first retained event
- **THEN** it preserves the original stack, stored reason, and correction date as legacy evidence
- **AND** it does not assign that reason to a particular earlier transition

### Requirement: Classification history commits against the source snapshot it describes

A correction SHALL commit its classification event with the metadata, companion move, and reference changes in the existing transaction.
A changed source snapshot or failed transaction SHALL NOT commit an event or overwrite the competing edit.

#### Scenario: An external metadata edit arrives during correction

- **WHEN** the Source changes after the correction reads it and before the transaction commits
- **THEN** the correction refuses and preserves the external edit
- **AND** it commits no classification event, relocation, or reference rewrite

#### Scenario: A transaction fails after preparation

- **WHEN** a correction fails while applying its transaction
- **THEN** the original source classification, history, bytes, and references remain intact
