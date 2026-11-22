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
When task discovery serves leads for a turn, a maps response SHALL carry a `positions` block. The block SHALL hold position units found on the served leads and on the pages that their served connections name, in either direction, that the caller may see. It SHALL serve only current units, under the same admission as the stance and the domain profiles: a unit on a superseded or historical page, an expired unit and a superseded unit are not served. It SHALL rank every current unit in reach by relevance to the turn and then by recency, and give each unit its text, its authored date where present, its page title and its unit reference. The block SHALL have its own bound, the vault budget `positions_max_chars`, and SHALL NOT draw on the turn allowance `max_chars`. Overflow SHALL become references within the same bound, never a cut unit, except that a caller served only an excerpt of a page has its left-out units counted instead, as the whole-unit requirement below says, and a bound of 0 SHALL turn the block off. The block SHALL NOT read a page that supplies the stance or a domain profile, a page marked `stance: true` or a page of a profile note type, whether or not the `stance` or `domain_profiles` block serves that page on the turn. A turn with no served leads, or with no position unit in reach, SHALL report the block's state and carry no items. When the request deadline skips or stops a stage of task discovery, a discovery lane that decides the served pages fails, or task discovery does not run, the block SHALL name that cause as its `reason`, and SHALL report `unavailable` when it had no page to read and `partial` otherwise, never `empty`.

#### Scenario: A position late on a linked long page reaches the agent
- **WHEN** a turn's served lead is a short page that links to a long page, and the long page holds a position unit in its last fifth
- **THEN** the `positions` block carries that unit with its date, the long page's title and the unit reference, and the agent does not have to read the long page to see it

#### Scenario: A turn whose task discovery ran out of time does not read as having no position
- **WHEN** the request deadline skips task discovery, so no lead is served
- **THEN** the `positions` block reports `unavailable` with reason `budget` and carries no items

#### Scenario: A turn whose task discovery stopped early reports what it read as partial
- **WHEN** leads are served but the request deadline stops a later stage of task discovery, such as the leads' connections
- **THEN** the `positions` block reports `partial` with reason `budget`, whatever units it found on the pages it read

#### Scenario: A turn whose connection lookup failed reports what it read as partial
- **WHEN** leads are served but the lookup of their connections fails
- **THEN** the `positions` block reports `partial` with reason `lane_failed`

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
The `positions` block SHALL state, in one passage, that its units are the person's own recorded positions. That claim rests on the trait declaration: a category or kind carries the trait `position` because it records the person's own positions. The shipped `preference` category carries the trait from the pack, and a vault category carries it from the owner's reviewed registry save. Where the turn bears on one, the agent's first answer SHALL state the position, attributed to the person and dated, before any other reading of the evidence. The agent SHALL NOT answer inside a premise that the position disputes, and SHALL rank no other explanation above the position without naming the evidence that ranks it. The agent SHALL then judge the evidence independently and say plainly whether it supports the position, leaves it open or contradicts it. Where the evidence contradicts the position, the agent SHALL say that it does, and SHALL NOT keep the position alive with a possibility the evidence gives no reason for. A recorded position SHALL stay the person's for the rest of the conversation. The framing SHALL NOT instruct the agent to agree with a position, and SHALL NOT present a position as established fact. The capture guidance SHALL record the agent's own analysis under a category without the trait `position`.

#### Scenario: New evidence bears on a recorded position
- **WHEN** a turn brings a measurement on a subject where the person recorded a qualified working attribution
- **THEN** the served block presents that attribution as the person's recorded position with its qualifier, alongside the instruction to judge the evidence independently

#### Scenario: A question that asks to confirm a cause the position disputes
- **WHEN** the person recorded a dated position that attributes a bloom to the new feeder layout, not the weather, and a turn asks "Is it the weather again?"
- **THEN** the served framing asks the agent to state the recorded position, attributed and dated, before it answers yes or no, not to answer inside the premise that the weather is the cause, and not to rank the weather above the position without naming the evidence that ranks it, and it never asks the agent to agree

#### Scenario: Evidence that contradicts the recorded position
- **WHEN** the person recorded a dated position that attributes a bloom to the new feeder layout, and a turn brings a measurement that the feeder layout cannot explain, such as a bloom that began before the layout changed
- **THEN** the served framing asks the agent to open its first answer with the recorded position, attributed and dated, and then to say plainly that the evidence contradicts it, and not to keep the position alive with a possibility the evidence gives no reason for

#### Scenario: The agent's own analysis is captured outside the position categories
- **WHEN** the agent records its own analysis of a subject during capture
- **THEN** it records the unit under a category without the trait `position`, so the positions block does not serve that unit as the person's

#### Scenario: A later contradicting record is served beside the position
- **WHEN** the same page holds the position and a later position unit in which the person withdrew it
- **THEN** both units appear, newest first by date, and the framing asks the agent to judge rather than adopt either

### Requirement: A Position Is Served Whole When It Fits The Block's Bound
The `positions` block SHALL serve a position unit whole whenever its line fits the block's remaining bound, whatever the unit's length. No per-unit length cap SHALL turn a unit into a reference. A unit that does not fit SHALL become a reference, never a cut unit. The shipped `positions_max_chars` SHALL leave room for one long position and a short one.

For a caller whose release ceiling serves a page only as an excerpt, the units served from that page SHALL together stay within the excerpt's bound. That caller cannot read a unit by reference, so a unit that does not fit SHALL be counted in `omitted` and never named. A unit on a page of which the caller may read no text SHALL be neither served, named nor counted. Terminal egress SHALL decide these page bounds again, for the pages of the units it only counts as well as the pages of the units it serves, so a policy that tightens after the block is compiled yields an unavailable block, not lines beyond the new bound and not a count of units on a page the caller may now read no text of.

#### Scenario: A long causal attribution is served whole
- **WHEN** a turn reaches a position unit of about 1,500 characters and a short position unit, under the shipped bound
- **THEN** the block carries both units whole, and lists neither as a reference

#### Scenario: A caller served page excerpts has a long position counted, not named
- **WHEN** a caller whose release ceiling serves the position's page as an excerpt reaches a position unit longer than that excerpt's bound, and a short position unit on the same page
- **THEN** the block carries the short unit whole, names no reference for the long unit and counts it in `omitted`

#### Scenario: Short positions on an excerpted page share its excerpt bound
- **WHEN** a caller whose release ceiling serves a page as an excerpt reaches eight position units on that page, each within the excerpt bound and together beyond it
- **THEN** the block carries units up to the excerpt bound in total, counts the rest in `omitted` and names no reference

#### Scenario: A page the caller may read no text of lists no unit
- **WHEN** the page text bound of a reached page is 0 for the caller
- **THEN** the block carries no line, reference or count for that page's units

#### Scenario: A policy that tightens before terminal egress
- **WHEN** a caller released a whole page when the block was compiled and only an excerpt of it at terminal egress
- **THEN** the block is reported unavailable and carries no unit of that page

### Requirement: The Positions Block Leads The Maps Response And Opens With Its Framing
A maps response SHALL carry the `positions` block as its first field, before the map and every other field, including the fields that terminal egress adds. The block's framing passage `about` SHALL be its first field, followed by `state`, `items`, `overflow_refs`, `omitted` and, where present, `reason`. A client that renders the block SHALL render it before the map and the task leads, and the framing passage before the items. A client that renders a `partial` or `unavailable` block SHALL say that the block is not the whole, with its reason, even when the block carries no item. The response's `budget.serialized_chars` SHALL stay the length of the response as sent.

The block leads because a block placed last, or mid-packet, did not reliably open the first answer on a leading yes/no question, where one placed first did.

#### Scenario: The block is read first, framing first
- **WHEN** a maps response serves a `positions` block with items
- **THEN** `positions` is the response's first field, `about` is the block's first field, and `budget.serialized_chars` equals the length of the response without the `serialized_chars` field

#### Scenario: A rendered block says when it is not the whole
- **WHEN** a client renders a `positions` block reported `unavailable` or `partial`
- **THEN** the rendered text names the state and the reason and says that a recorded position may exist that is not shown

#### Scenario: A client renders the block before the map and the leads
- **WHEN** a client renders a response that carries a map, task leads and a `positions` block
- **THEN** the rendered block comes before the rendered map and the rendered task leads

### Requirement: The Positions Projection Proves Its Cost
The projection SHALL add at most one batched semantic-unit query per recall, restricted to the served leads and the pages their served connections name, and SHALL read only the pages that query names. Its cost SHALL be proven with a before and after on the recall latency instrument before release.

#### Scenario: One bounded query per recall
- **WHEN** a recall serves eight leads whose served connections name twenty pages between them
- **THEN** the projection issues one semantic-unit query over at most those twenty-eight parent pages
