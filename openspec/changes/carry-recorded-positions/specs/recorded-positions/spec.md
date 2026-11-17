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
When task discovery serves leads for a turn, a maps response SHALL carry a `positions` block. The block SHALL hold position units found on the served leads and on the pages those leads link to that the caller may see. It SHALL serve only current units, under the same admission as the stance and the domain profiles: a unit on a superseded or historical page, an expired unit and a superseded unit are not served. It SHALL rank every current unit in reach by relevance to the turn and then by recency, and give each unit its text, its authored date where present, its page title and its unit reference. The block SHALL have its own bound, the vault budget `positions_max_chars`, and SHALL NOT draw on the turn allowance `max_chars`. Overflow SHALL become references within the same bound, never a cut unit, and a bound of 0 SHALL turn the block off. The block SHALL NOT read a page that supplies the stance or a domain profile, a page marked `stance: true` or a page of a profile note type, whether or not the `stance` or `domain_profiles` block serves that page on the turn. A turn with no served leads, or with no position unit in reach, SHALL report the block's state and carry no items.

#### Scenario: A position late on a linked long page reaches the agent
- **WHEN** a turn's served lead is a short page that links to a long page, and the long page holds a position unit in its last fifth
- **THEN** the `positions` block carries that unit with its date, the long page's title and the unit reference, and the agent does not have to read the long page to see it

#### Scenario: A hidden page contributes nothing
- **WHEN** a served lead links to a page the caller may not see
- **THEN** no unit of that page appears in the `positions` block and the block does not name the page

#### Scenario: A unit already served as session context is not repeated
- **WHEN** a position unit sits on a page that the same packet serves under `stance` or `domain_profiles`, and that unit is among the lines served there, or among the lines the caller holds there from an earlier turn
- **THEN** the `positions` block omits it

#### Scenario: A session page the turn did not select is not read as positions
- **WHEN** a served lead links to a profile page of a domain the turn does not touch, or to a stance page whose line the stance bound left out
- **THEN** no unit of either page appears in the `positions` block

#### Scenario: A position on a superseded page is not served
- **WHEN** a served lead links to a page whose author marked it superseded by a newer page
- **THEN** no unit of the superseded page appears in the `positions` block

#### Scenario: The unit that answers the turn is not crowded out by newer ones
- **WHEN** a served lead links to a page of many newer position units that do not bear on the turn, and to an older page whose last unit does
- **THEN** the unit that bears on the turn is the first item of the block

#### Scenario: The block does not crowd out task leads
- **WHEN** a turn's position units exceed `positions_max_chars`
- **THEN** the block carries whole units up to its bound and lists the rest as references, and the task leads keep the full turn allowance

### Requirement: The Positions Block Frames Units As The Person's Own
The `positions` block SHALL state, in one passage, that its units are the person's own recorded positions. That claim rests on the owner's trait declaration: a category or kind carries the trait `position` because it records the person's own positions. Where the turn bears on one, the agent SHALL say how the new evidence relates to it, attributed to the person, and SHALL judge the evidence independently. The agent SHALL say plainly where the evidence does not support the position or contradicts it. A recorded position SHALL stay the person's for the rest of the conversation. The framing SHALL NOT instruct the agent to agree with a position, and SHALL NOT present a position as established fact. The capture guidance SHALL record the agent's own analysis under a category without the trait `position`.

#### Scenario: New evidence bears on a recorded position
- **WHEN** a turn brings a measurement on a subject where the person recorded a qualified working attribution
- **THEN** the served block presents that attribution as the person's recorded position with its qualifier, alongside the instruction to judge the evidence independently

#### Scenario: The agent's own analysis is captured outside the position categories
- **WHEN** the agent records its own analysis of a subject during capture
- **THEN** it records the unit under a category without the trait `position`, so the positions block does not serve that unit as the person's

#### Scenario: A later contradicting record is served beside the position
- **WHEN** the same page holds the position and a later position unit in which the person withdrew it
- **THEN** both units appear, newest first by date, and the framing asks the agent to judge rather than adopt either

### Requirement: A Position Is Served Whole When It Fits The Block's Bound
The `positions` block SHALL serve a position unit whole whenever its line fits the block's remaining bound, whatever the unit's length. No per-unit length cap SHALL turn a unit into a reference, except that for a caller whose release ceiling serves the unit's page as an excerpt, a unit longer than that excerpt's bound SHALL become a reference. A unit that does not fit SHALL become a reference, never a cut unit. The shipped `positions_max_chars` SHALL leave room for one long position and a short one.

#### Scenario: A long causal attribution is served whole
- **WHEN** a turn reaches a position unit of about 1,500 characters and a short position unit, under the shipped bound
- **THEN** the block carries both units whole, and lists neither as a reference

#### Scenario: A caller served page excerpts gets a long position as a reference
- **WHEN** a caller whose release ceiling serves the position's page as an excerpt reaches a position unit longer than that excerpt's bound, and a short position unit on the same page
- **THEN** the block lists the long unit as a reference, never cut, and carries the short unit whole

### Requirement: The Positions Block Ends The Maps Response And Leads With Its Framing
A maps response SHALL carry the `positions` block as its last field, after every other field it carries, including the fields that terminal egress adds. The block's framing passage `about` SHALL be its first field, followed by `state`, `items`, `overflow_refs`, `omitted` and, where present, `reason`. A client that renders the block SHALL render the framing passage before the items.

#### Scenario: The block is read last, framing first
- **WHEN** a maps response serves a `positions` block with items
- **THEN** `positions` is the response's last field, and `about` is the block's first field

### Requirement: The Positions Projection Proves Its Cost
The projection SHALL add at most one batched semantic-unit query per recall, restricted to the served leads and their listed link targets, and SHALL read only the pages that query names. Its cost SHALL be proven with a before and after on the recall latency instrument before release.

#### Scenario: One bounded query per recall
- **WHEN** a recall serves eight leads that list twenty link targets between them
- **THEN** the projection issues one semantic-unit query over at most those twenty-eight parent pages
