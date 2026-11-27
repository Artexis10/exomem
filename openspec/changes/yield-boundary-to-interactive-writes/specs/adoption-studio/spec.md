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

### Requirement: Every Import Step Yields For A Waiting Interactive Write And Releases The Boundary Before It Is Overdue

`plan`, `finish` and each step of a `file` call SHALL end their call at the next item or step boundary when an interactive writer waits for the vault mutation boundary and the call has handled one item or step. A call SHALL release the boundary before its hold turns overdue, with or without a request budget. A `plan` or `finish` call that ends for a waiting writer SHALL carry `stop_reason: interactive_write_waiting`, and the next call SHALL resume without repeating an item.

#### Scenario: An agent write arrives during a slow plan

- **WHEN** a `plan` call for a page runs over many items
- **AND** an interactive write, from the agent's own session or from the CLI, waits for the boundary while an item is planned
- **THEN** the write commits once and is not refused `MUTATION_BUSY`
- **AND** the plan call returns the progress so far, a `continuation` and `stop_reason: interactive_write_waiting`

#### Scenario: A second file call arrives during a slow plan

- **WHEN** a `file` call runs the `plan` step of a page and a second `file` call waits for the boundary
- **THEN** the second call is served and is not refused `MUTATION_BUSY`
- **AND** the first call returns `filing: working` with its continuation, and later calls plan each remaining item once

#### Scenario: A file call meets a waiting write between steps

- **WHEN** a `file` call has run one step and an interactive writer waits
- **THEN** the call returns `filing: working` before it starts the next step
- **AND** the next call carries on from the saved phase
- **AND** a default `file` call on a stage that the cell's filer owns answers as before

#### Scenario: The first read of many Sources ends for a waiting write

- **WHEN** a `plan` call has to read the earlier imports of a vault with many Sources that the process has not read
- **AND** an interactive writer waits, or the hold nears the overdue limit
- **THEN** the call ends before it plans an item, with a `continuation`, and the writer commits
- **AND** the next call carries on from the pages already read, and the plan covers every item once

#### Scenario: A finish call yields and resumes

- **WHEN** a `finish` call has removed some staged copies and an interactive writer waits
- **THEN** the call returns with the run still in its phase and `stop_reason: interactive_write_waiting`
- **AND** a later `finish` removes the remaining copies, each once, and completes the run

#### Scenario: No writer waits and no budget is bound

- **WHEN** no interactive writer waits during an import step and the call binds no request budget
- **THEN** the call stops starting items before its hold reaches the long-holder limit of the mutation lock
- **AND** the reply carries no `stop_reason: interactive_write_waiting`
