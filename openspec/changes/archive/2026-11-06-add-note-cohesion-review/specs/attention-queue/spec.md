## ADDED Requirements

### Requirement: Note cohesion is a size-stepped review family judged by the agent

The audit registry SHALL add the category `note_cohesion`. Its review items have the standard reference, fingerprint, dismiss, snooze and reopen semantics.

A page SHALL be eligible when its note type has role `compiled`, its lifecycle status is live, it lies in a read-write tree, it is recall-eligible, it does not carry a breadth tag (exactly `hub` or `snapshot`), and its file name is not `index.md` or `log.md`. Superseded, archived, dropped and draft pages SHALL NOT be asked. An eligible page's size step SHALL be the largest value of the sequence 9, 14, 21, 32, 48, … that its semantic-unit count reaches, where each value is the previous value multiplied by 1.5 and rounded up. An eligible page that reaches a step SHALL produce exactly one `info` item.

The item SHALL carry the page's unit count, its step and its next step. Its detail SHALL state the question: does this page answer more than one independent question? The item SHALL NOT claim that the page is overloaded, and SHALL carry no strength, score or terms.

The item's identity SHALL be the page and the family: one item per page, whose review identity is distinct from every other family's item on the same page. Its signal version SHALL be the step value, so that a decision binds to the step. A dismissal SHALL hold while the page stays within that step, and the item SHALL reopen when the page reaches the next step. A page that falls below the base step, or gains a breadth tag, SHALL leave the family without any recorded decision.

Because its step sequence is the system's choice and not an authored date, the category SHALL be registered and selectable but SHALL NOT join the default attention union.

#### Scenario: A page reaching the base step opens one item

- **WHEN** a compiled page reaches 9 semantic units and no decision is recorded for it
- **THEN** one open `note_cohesion` item exists for that page with step 9 and next step 14
- **AND** the item's detail asks whether the page answers more than one independent question

#### Scenario: A dismissal holds within a step and reopens at the next

- **WHEN** the item of a page at 14 units is dismissed, and the page later grows to 20 units
- **THEN** the item stays dismissed
- **AND** when the page reaches 21 units, the item is open again with a new fingerprint

#### Scenario: A split below the base step clears the item without a decision

- **WHEN** a curation split leaves a page with 6 semantic units
- **THEN** no `note_cohesion` item exists for that page
- **AND** no review decision was recorded to clear it

#### Scenario: A hub is never asked

- **WHEN** a compiled page with 40 semantic units carries the `hub` tag
- **THEN** no `note_cohesion` item exists for that page

#### Scenario: A decision on another family leaves the question open

- **WHEN** a page holds an open `note_cohesion` item and an open `relation_debt` item, and the `relation_debt` item is dismissed by its ref
- **THEN** the `note_cohesion` item stays open and the next compiled write to the page still carries the question

#### Scenario: A superseded page is never asked

- **WHEN** a compiled page with 20 semantic units has the status `superseded`
- **THEN** no `note_cohesion` item exists for that page

#### Scenario: A tag that only contains a breadth word does not exempt the page

- **WHEN** a compiled page with 20 semantic units carries the tag `snapshot-testing`
- **THEN** the page has an open `note_cohesion` item

#### Scenario: Sources are never asked

- **WHEN** a Source page holds 50 semantic units
- **THEN** no `note_cohesion` item exists for that page

### Requirement: Note cohesion items join the due-state projection as a backlog

The due-state projection SHALL maintain `note_cohesion`, and a page write SHALL settle the written page's item in full. A page that crosses a step SHALL therefore open its item at write time, and a page that falls below the base step SHALL drop it.

An item's due date SHALL be the page's authored `updated` or `created` date. A page with neither SHALL use the date of computation. In the served order the family SHALL sort after every dated obligation and before the Source backlog. Its counts SHALL be exact and never capped.

#### Scenario: A backlog never displaces a due prediction

- **WHEN** 200 pages hold open `note_cohesion` items and one prediction window is due
- **THEN** the served top rows list the due prediction before any `note_cohesion` row
- **AND** the served category count for `note_cohesion` is 200

#### Scenario: The family sorts ahead of the Source backlog

- **WHEN** both `note_cohesion` items and unprocessed Sources are open, and no dated obligation is due
- **THEN** the served top rows list `note_cohesion` rows before `unprocessed_source` rows

### Requirement: Note cohesion answers use the existing review and curation surfaces

The served guidance for `note_cohesion` SHALL direct the agent to read the whole page and name the independent questions it answers. A page that answers one question, however long, SHALL be kept whole by dismissing the item with an `intentional:` reason that names that question. A page that answers several questions with their own material SHALL be split only through the curation lane, which requires the user's exact plan confirmation. A page that is a hub by design SHALL receive a breadth tag through a governed edit.

The item SHALL be complete without any sensor reading. A future sensor MAY attach readings to an item as `evidence_refs`, each with a reading id and the content hash of the page it read. A reading whose content hash no longer matches the page SHALL be ignored.

#### Scenario: Keeping a coherent page whole needs one call

- **WHEN** an agent judges that a page with an open item answers one question
- **THEN** one `triage_memory` dismissal with the item's ref, fingerprint and an `intentional:` reason settles the item

#### Scenario: A split never happens without the user's confirmation

- **WHEN** an agent judges that a page answers several independent questions
- **THEN** the page changes only through a curation plan that the user confirms by its exact plan id and fingerprint

#### Scenario: The item stands without a sensor

- **WHEN** sensing is off for a vault
- **THEN** `note_cohesion` items are produced, served and answerable exactly as when sensing is on
