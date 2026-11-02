## MODIFIED Requirements

### Requirement: Read-only inspection operations

`govern_memory` `list`, `explain`, and `simulate` SHALL be read-only and SHALL NOT
write policy or state. `explain` SHALL resolve one exact canonical path and an
explicit audience, then show its effective policy and participating rule chain.
`simulate` SHALL resolve explicit canonical paths and an explicit audience; neither
operation may silently ignore audience behavior. Toward an audience below an
item's ceiling, these operations SHALL return counts and rule ids only, never
titles or excerpts of restricted items.

`list` SHALL return rule ids and grant ids to the owner only; a non-owner receives empty lists. `coordination_status` SHALL return mutation-lock contention to the owner only.

#### Scenario: Explain shows the effective chain

- **WHEN** `explain` is called for an item and audience
- **THEN** it returns the effective ceiling and the ordered participating rules
  without leaking restricted content

#### Scenario: A non-owner lists no policy ids

- **WHEN** a non-owner calls `govern_memory` `list` on a governed vault
- **THEN** the response carries empty `rules` and `grants` lists and no fingerprint or scopes

#### Scenario: A non-owner sees no lock contention

- **WHEN** a non-owner calls `coordination_status`
- **THEN** the mutation boundary carries no `contention` block
