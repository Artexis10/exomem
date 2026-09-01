## ADDED Requirements

### Requirement: Compact responses may carry one bounded advisory due-state block

Default compact mutating responses and recall responses MAY carry one `due_state` block: per-category open counts and a bounded list of top item references drawn from the maintained projection for the requesting audience. The block SHALL follow the established advisory posture: validated and projected from the leaf, bounded in size, never a key a client branches on for mutation outcome, and absent — never null or empty — when there is nothing to report. It SHALL NOT alter `status`, `mutated`, `path`, `warnings_count`, mutation identity, or replay behaviour. The legacy response detail SHALL omit the block. Tool input schemas SHALL NOT change.

#### Scenario: A due prediction reaches the agent on an unrelated write

- **WHEN** a prediction became due earlier in the session and any compiled write commits
- **THEN** the default compact response carries a `due_state` block naming the category count and the item reference
- **AND** the mutation outcome keys are byte-identical to a projection-free response apart from the advisory block

#### Scenario: Recall responses carry deltas only

- **WHEN** consecutive recall calls execute with no change in the projection between them
- **THEN** at most the first response carries the `due_state` block
- **AND** a later call after the projection changes carries the block again

#### Scenario: The legacy detail omits the block

- **WHEN** a mutation is invoked with the legacy response detail
- **THEN** the response carries no `due_state` block and is otherwise unchanged

### Requirement: Emission is governed so the carrier cannot nag

The block SHALL be emitted on change of count, or on the first qualifying response of a session; identical totals SHALL NOT be repeated on consecutive responses. A bulk or batch operation SHALL emit at most one block, at its end, regardless of how many writes it contains. Emission governance SHALL be deterministic and testable without an agent.

#### Scenario: A bulk import does not emit forty blocks

- **WHEN** a batch operation commits forty compiled writes while the projection's totals change once
- **THEN** at most one `due_state` block is emitted for the batch

#### Scenario: An unchanged total goes quiet

- **WHEN** three consecutive writes commit with no change in any category count
- **THEN** at most the first of the three responses carries the block
