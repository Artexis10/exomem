## ADDED Requirements

### Requirement: A directly named note leads incidental task matches

With full source access, the default task projection SHALL prefer a compiled note when the current turn reaches its released title by the existing title evidence rule. Routed collections SHALL retain precedence. Prior order SHALL hold within each group. The released-corpus lane SHALL retain its order. An enabled context-fit interpreter MAY reorder the result.

#### Scenario: A named note and a peripheral share a request

- **WHEN** a compound turn names an admitted compiled note and another subject
- **AND** an incidental candidate would rank above the note by discovery score
- **THEN** the default task block puts the named note ahead of the incidental match

#### Scenario: Only incidental text matches

- **WHEN** the turn names no admitted note by title
- **THEN** task discovery retains its existing order
