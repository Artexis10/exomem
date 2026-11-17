## ADDED Requirements

### Requirement: Position Units Are Selected By A Declared Trait
The system SHALL treat a semantic unit as a position unit only when its resolved category, or its kind, carries the trait `position` in the shipped pack or in a reviewed vault registry save. It SHALL NOT select position units by a category name, a word list or the unit's text.

#### Scenario: A declared category marks its units as positions
- **WHEN** a reviewed vault registry save declares category `causal_attribution` with trait `position`, and a page holds the unit `- [causal_attribution] 2026-03-02: His working model is that the new feeder layout, not the weather, explains the bloom; a hypothesis, not proven.`
- **THEN** recall treats that unit as a position unit

#### Scenario: An undeclared category is not a position
- **WHEN** a page holds a unit of a category that no registry entry gives the trait `position`, even if its text says "my position"
- **THEN** recall does not treat that unit as a position unit

### Requirement: Recall Serves The Positions That Bear On A Turn
When task discovery serves leads for a turn, a maps response SHALL carry a `positions` block. The block SHALL hold position units found on the served leads and on the pages those leads link to that the caller may see. It SHALL rank them by relevance to the turn and then by recency, and give each unit its text, its authored date where present, its page title and its unit reference. The block SHALL have its own bound, the vault budget `positions_max_chars`, and SHALL NOT draw on the turn allowance `max_chars`. Overflow SHALL become references, never a cut unit. The block SHALL omit a unit that the same response serves under `stance` or `domain_profiles`. A turn with no served leads, or with no position unit in reach, SHALL report the block's state and carry no items.

#### Scenario: A position late on a linked long page reaches the agent
- **WHEN** a turn's served lead is a short page that links to a long page, and the long page holds a position unit in its last fifth
- **THEN** the `positions` block carries that unit with its date, the long page's title and the unit reference, and the agent does not have to read the long page to see it

#### Scenario: A hidden page contributes nothing
- **WHEN** a served lead links to a page the caller may not see
- **THEN** no unit of that page appears in the `positions` block and the block does not name the page

#### Scenario: A unit already served as session context is not repeated
- **WHEN** a position unit sits on a page that the same packet serves under `stance` or `domain_profiles`, and that unit is among the lines served there
- **THEN** the `positions` block omits it

#### Scenario: The block does not crowd out task leads
- **WHEN** a turn's position units exceed `positions_max_chars`
- **THEN** the block carries whole units up to its bound and lists the rest as references, and the task leads keep the full turn allowance

### Requirement: The Positions Block Frames Units As The Person's Own
The `positions` block SHALL state, in one passage, that its units are the person's own recorded positions. Where the turn bears on one, the agent SHALL say how the new evidence relates to it, attributed to the person, and SHALL judge the evidence independently. The agent SHALL say plainly where the evidence does not support the position or contradicts it. A recorded position SHALL stay the person's for the rest of the conversation. The framing SHALL NOT instruct the agent to agree with a position, and SHALL NOT present a position as established fact.

#### Scenario: New evidence bears on a recorded position
- **WHEN** a turn brings a measurement on a subject where the person recorded a qualified working attribution
- **THEN** the served block presents that attribution as the person's recorded position with its qualifier, alongside the instruction to judge the evidence independently

#### Scenario: A later contradicting record is served beside the position
- **WHEN** the same page holds the position and a later position unit in which the person withdrew it
- **THEN** both units appear, newest first by date, and the framing asks the agent to judge rather than adopt either

### Requirement: The Positions Projection Proves Its Cost
The projection SHALL add at most one batched semantic-unit query per recall, restricted to the served leads and their listed link targets. Its cost SHALL be proven with a before and after on the recall latency instrument before release.

#### Scenario: One bounded query per recall
- **WHEN** a recall serves eight leads that list twenty link targets between them
- **THEN** the projection issues one semantic-unit query over at most those twenty-eight parent pages
