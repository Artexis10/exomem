## ADDED Requirements

### Requirement: A page marks itself a stance page

A page SHALL become a stance page through the frontmatter key `stance: true`. Every semantic unit of a stance page, rich or compact, SHALL be a stance line: a rule that the owner keeps for every task. A stance line SHALL carry no task scope and no entity scope. A stance page SHALL follow the vault's governance policy for writes, as every page does. The stance mark SHALL change no authority: supersession and `contradicts` relations apply as before.

`stance` SHALL be a boolean. A governed write SHALL refuse any other value, and its error SHALL name the key and the values `true` and `false`. A hand edit that holds any other value SHALL produce a validation finding, and the page SHALL NOT be a stance page.

#### Scenario: A marked page supplies stance lines

- **WHEN** a page carries `stance: true` and holds two compact lines and one rich unit
- **THEN** all three units are stance lines

#### Scenario: A malformed mark is refused

- **WHEN** an agent writes a page with `stance: "yes"`
- **THEN** the write is refused, and the error names `stance` and the values `true` and `false`

### Requirement: Every maps packet carries a stance block

Every maps-mode packet SHALL carry a `stance` block, after the continuity fields and before the map, unless `EXOMEM_ACTIVATION_STANCE` is `off`. The block SHALL hold:

- `state`: `ready` when the block serves at least one line, `empty` when it serves none, and `unavailable` when its compile failed;
- `generation`: the stance generation that the cache requirement defines;
- `items`: the stance lines, each `{ref, text}`, with `review_due` when it applies;
- `omitted`: the count of current lines that did not fit;
- `expired`: the count of lines that currency withheld because their `until` passed.

Lines SHALL follow the stance pages in path order, then the units in page order. The block SHALL keep whole lines only, under the hard character cap `STANCE_MAX_CHARS`. A failed compile SHALL leave the map and the slice served.

The block SHALL be computed over the caller's view. A stance page or unit that is withheld from the caller SHALL count as absent, never as omitted or expired. The packet budget SHALL charge the characters that the block uses to `max_chars` before the map is filled, so the map gives up only what the block uses. An empty block SHALL cost only its keys.

#### Scenario: A vault with no stance pages serves an empty block

- **WHEN** a maps packet is compiled for a vault with no stance page
- **THEN** the packet carries `stance.state = "empty"`, and the map receives the same budget as before this change, less the block's keys

#### Scenario: Overflow is counted, not cut

- **WHEN** the stance pages hold forty current lines and only twelve fit under `STANCE_MAX_CHARS`
- **THEN** the block serves twelve whole lines and reports `omitted: 28`

#### Scenario: A withheld stance page is absent

- **WHEN** a restricted caller cannot see one of two stance pages
- **THEN** its block holds only the visible page's lines, and its counts equal those of a vault without the withheld page

### Requirement: The stance block is cached by generation and by its next date boundary

The stance block SHALL be cached apart from the packet. Its cache key SHALL cover the caller's view, the generation of the stance inputs and the next date boundary that any stance line crosses. The next date boundary SHALL be the earliest day after a line's `until` or `check_by` that is later than today, where today is the calendar day that the due-state projection uses. The block's `generation` SHALL change when any input of the key changes. A packet whose `conversation` bypasses the packet cache SHALL still reuse a cached stance block, because the block holds no conversation field.

#### Scenario: An expiring line leaves the block on time

- **WHEN** a stance line carries `until: 2026-12-01` and the block was cached on 2026-11-30
- **THEN** the first packet compiled on 2026-12-02 misses the stance cache, withholds the line and counts it in `expired`

#### Scenario: A new line changes the generation

- **WHEN** the owner adds a line to a stance page
- **THEN** the next packet carries a new stance `generation` and serves the new line

### Requirement: Stance lines follow typed currency

Every stance line SHALL pass through the packet builder's currency step, and the typed date rules of context-activation SHALL apply in their order. A superseded or archived line SHALL never be served. A line past its `until` SHALL be withheld and counted in `expired`. A line past its `check_by` SHALL stay served and SHALL carry `review_due` with that date, because a date that passes with nothing happening is not evidence that the stance changed. Every comparison SHALL be between typed dates, and no date SHALL be read from prose for these rules.

#### Scenario: A superseded line is never served

- **WHEN** a later stance line supersedes an earlier one
- **THEN** the block serves the later line, and counts the earlier line in neither `omitted` nor `expired`

#### Scenario: A line past its review date is served with a marker

- **WHEN** a stance line carries `check_by: 2026-10-01` and today is 2026-11-02
- **THEN** the line is served with `review_due: "2026-10-01"` and is not withheld

### Requirement: One setting rolls the stance block back

`EXOMEM_ACTIVATION_STANCE` SHALL default to `on`. The value `off` SHALL remove the `stance` block from maps packets and SHALL charge nothing for it to `max_chars`. The block runs no model and adds one cached read.

#### Scenario: Rollback removes the block

- **WHEN** `EXOMEM_ACTIVATION_STANCE=off` and the vault holds stance pages
- **THEN** the packet carries no `stance` block, and no part of `max_chars` is charged to it
