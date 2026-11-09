## ADDED Requirements

### Requirement: An Import Apply Ends Its Call For A Waiting Interactive Write

`adoption_studio(action="apply")` and `import_memory` apply SHALL end their call between chunks when an interactive writer waits for the vault mutation boundary and the call has committed at least one chunk. The reply SHALL carry `progress`, the `continuation` of the next chunk and `stop_reason: interactive_write_waiting`. The call SHALL NOT release the boundary in the middle of a chunk or of a call, and a run SHALL NOT take a lease. A call that has committed no chunk SHALL commit its first chunk before it checks, so every call progresses. A call that stops for another reason SHALL NOT carry this `stop_reason`. A call passing the returned cursor SHALL commit only the remaining items, each once, as for a request-budget stop.

#### Scenario: An agent write arrives during an apply

- **WHEN** an apply runs over several chunks
- **AND** an interactive write, from the agent's own session or from the CLI, waits for the boundary while a chunk runs
- **THEN** the write commits once and is not refused `MUTATION_BUSY`
- **AND** the apply call returns the items committed so far, a `continuation` and `stop_reason: interactive_write_waiting`

#### Scenario: The next call finishes the import

- **WHEN** a call that stopped for a waiting write is followed by calls that pass each returned cursor
- **THEN** every planned item is applied exactly once
- **AND** the run becomes `applied` when every item has an outcome

#### Scenario: No writer waits

- **WHEN** no interactive writer waits during an apply
- **THEN** the call runs until its request budget or the end of the plan, as before
- **AND** the reply carries no `stop_reason`
