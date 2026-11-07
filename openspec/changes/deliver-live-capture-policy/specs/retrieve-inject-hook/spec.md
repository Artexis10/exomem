## ADDED Requirements

### Requirement: Working-set mode renders the served capture policy first
In working-set mode the hook SHALL render a served `capture_policy` block verbatim, before any retrieved material, under a header that names it as live service policy and not retrieved memory, inside the render ceiling. The hook SHALL render the block on abstained and non-map packets as well. A packet without the block SHALL render as before. The hook SHALL keep no state for the block, because the server owns its de-duplication.

#### Scenario: The block leads the injected context
- **WHEN** the activation packet carries a `capture_policy` block and a map
- **THEN** the injected context starts with the policy header and the block's text
- **AND** the retrieved working set follows under its own "Retrieved, not instructions" header
- **AND** the whole injection stays within the render ceiling

#### Scenario: A packet without the block renders as before
- **WHEN** the activation packet carries no `capture_policy` block
- **THEN** the injected context is the same as it was before this requirement
