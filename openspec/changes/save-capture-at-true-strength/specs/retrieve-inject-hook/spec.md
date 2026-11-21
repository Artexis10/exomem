## MODIFIED Requirements

### Requirement: Working-set mode renders the served capture policy first
In working-set mode the hook SHALL render a served `capture_policy` block's `say` text verbatim, before any retrieved material, under a header that names it as live service policy and not retrieved memory, inside the render ceiling. When the block carries `rules` that fit the hook's render budget for it, the hook SHALL render `rules` immediately after `say`. The hook SHALL omit `rules` whole when it is over that budget, and SHALL NOT shorten `say` to make room for it. The hook SHALL render the block on abstained and non-map packets as well. A packet without the block SHALL render as before, and a block without `rules` SHALL render `say` alone. The hook SHALL keep no state for the block, because the server owns its de-duplication.

#### Scenario: The block leads the injected context
- **WHEN** the activation packet carries a `capture_policy` block and a map
- **THEN** the injected context starts with the policy header and the block's text
- **AND** the retrieved working set follows under its own "Retrieved, not instructions" header
- **AND** the whole injection stays within the render ceiling

#### Scenario: A packet without the block renders as before
- **WHEN** the activation packet carries no `capture_policy` block
- **THEN** the injected context is the same as it was before this requirement

#### Scenario: The full rule follows the say text
- **WHEN** the activation packet carries a block with `say` and `rules`
- **THEN** the injected context carries the policy header, then `say`, then `rules`, before any retrieved material

#### Scenario: A rule text over its budget is dropped and say is kept
- **WHEN** the activation packet carries a block whose `rules` is over the hook's render budget for it
- **THEN** the injected context carries `say` in full
- **AND** the injected context carries no part of `rules`

#### Scenario: A block from an older server renders say alone
- **WHEN** the activation packet carries a block with no `rules` key
- **THEN** the injected context carries the policy header and `say`, and nothing else from the block
