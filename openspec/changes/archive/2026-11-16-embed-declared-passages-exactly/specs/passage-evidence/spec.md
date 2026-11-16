## ADDED Requirements

### Requirement: A declared passage is embedded exactly as declared

The embedded text of a declared passage SHALL be exactly its declared text.
When a passage splits at the chunk cap, the embedded texts of its parts SHALL join to exactly its declared text.
The cell SHALL NOT add the Source title or any other text to a declared passage.
A Source captured without declared passages SHALL keep its title in its embedded text.

#### Scenario: The title does not change a passage score

- **WHEN** a caller captures the same declared passage under two different titles
- **THEN** the two chunks have equal embedded text and equal vectors
- **AND** a query ranks the two passages with equal scores

#### Scenario: A split passage holds only its own text

- **WHEN** a declared passage exceeds the chunk cap
- **THEN** the embedded texts of its parts join to exactly the declared text

#### Scenario: Other Sources keep their title

- **WHEN** an agent saves a Source without declared passages
- **THEN** the embedded text of its chunks starts with the Source title
