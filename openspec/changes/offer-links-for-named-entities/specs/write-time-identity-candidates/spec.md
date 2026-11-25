## ADDED Requirements

### Requirement: A write offers links for the existing Entities it names

Every committed durable page write SHALL carry an `unlinked_mentions` block in its receipt when its body newly names an active, writer-visible Entity by the Entity's exact title or alias without linking it.
The block SHALL list at most 5 Entities in all, grouped by page, each with the name as written and the Entity's link target.
The server SHALL NOT write the link itself.

#### Scenario: A note that names two Entities in prose is offered both links

- **WHEN** an agent writes a note whose body names `Marin Holt` and `Harbour Supply Co`, each the exact title of an active Entity, without linking either
- **THEN** the receipt's `unlinked_mentions` block lists both Entities with their link targets
- **AND** the note is committed exactly as the agent wrote it

#### Scenario: An alias counts as a name

- **WHEN** a note names `the Kestrel`, and an active Entity titled `Kestrel (sailing boat)` lists `the Kestrel` as an alias
- **THEN** the block lists that Entity

### Requirement: Only an unambiguous, unlinked, new name is offered

A name SHALL match after `entity_candidates.identity_key` normalisation, on word boundaries, taking the longest match, and never inside frontmatter, code, links, images, HTML, headings or bare URLs.
A name that two or more writer-visible Entities answer to, the page's own Entity, and an Entity the page already links SHALL NOT be offered.
An Entity SHALL be offered only when the write raised the page's count of unlinked mentions of it, compared with the pre-write state.

#### Scenario: An ambiguous name is not offered

- **WHEN** a note names `Marin`, and two active Entities list `Marin` as their title or alias
- **THEN** the block does not list either Entity for that name

#### Scenario: A linked name and the page's own Entity are not offered

- **WHEN** an Entity page names itself in its body, and a note names an Entity that it already links elsewhere in its body
- **THEN** neither write's block lists those Entities

#### Scenario: Names in code and links are ignored

- **WHEN** an Entity's title appears only inside a code span, a URL or the display text of an existing link
- **THEN** the block does not list that Entity

#### Scenario: Editing an old note does not repeat old offers

- **WHEN** a note that already named an Entity without a link is edited, and the edit adds no new unlinked mention of it
- **THEN** the receipt's block does not list that Entity

### Requirement: The offer carries one ready call

The block SHALL carry, for each page, one `edit_note` `replace_text` call with one `edits` item per Entity it lists for that page.
Each item SHALL replace the Entity's first unlinked mention with a wikilink to the Entity that shows the name as written, and its `old_text` SHALL occur exactly once in the post-write body.

#### Scenario: The ready call links the names and nothing else

- **WHEN** the agent sends the block's ready call unchanged
- **THEN** the note gains one wikilink for each listed Entity and no other change

#### Scenario: A repeated name gets a unique anchor

- **WHEN** a listed Entity's name occurs twice in the note without a link
- **THEN** the call's `old_text` for it is a longer span that occurs once, and the call links the first mention

#### Scenario: The ready call works where links are shown by title

- **WHEN** the note links a page whose title holds a listed Entity's name, and the agent sends the block's ready call on a surface that shows links by title
- **THEN** the call's `old_text` occurs once there as well, and the call links the note's own mention

#### Scenario: A one-word name matches only as it is written

- **WHEN** an Entity's title is one word and the note uses that word in another case
- **THEN** the note is not offered that Entity

### Requirement: The offer follows the write-advisory rules

The block SHALL be withheld when `structural_suggestions` is `off` or `proactive_capture` resolves to `off`, and the `unlinked_mentions` family SHALL be quietable.
A write-advisory fingerprint SHALL dismiss an Entity for a page.
Pages committed within one mutation batch SHALL list an Entity once per page.
The compact response projection SHALL carry the block.

#### Scenario: A quieted family stays quiet

- **WHEN** the owner quiets the `unlinked_mentions` family, or `structural_suggestions` is `off`
- **THEN** no receipt carries the block

#### Scenario: The agent declines

- **WHEN** the agent dismisses an Entity listed for a page, and a later write to that page adds another unlinked mention of it
- **THEN** that write's block does not list the Entity

#### Scenario: A hookless client receives the block

- **WHEN** a hookless client calls a durable writer over MCP with compact response detail, and the write names an existing Entity without linking it
- **THEN** the compact response that passed MCP egress carries the block and its ready call

### Requirement: The offer is cheap and discloses nothing

The block SHALL read the Entity registry entries that the write's preflight corpus yields, with no vault walk and no model call.
A write whose corpus yields no registry SHALL carry no block, and a fault SHALL never fail the write.
A name that matches only an Entity the writer may not see SHALL NOT be offered.

#### Scenario: A withheld Entity is indistinguishable from an absent one

- **WHEN** a restricted writer's note names a title that only an Entity it may not see answers to
- **THEN** the receipt is the same as for a vault without that Entity

#### Scenario: A fault costs the write nothing

- **WHEN** the name matcher raises while a write commits
- **THEN** the write succeeds without the block
