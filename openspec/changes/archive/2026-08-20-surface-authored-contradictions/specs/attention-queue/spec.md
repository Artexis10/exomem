## ADDED Requirements

### Requirement: Asserted Contradictions Rank Above Proximity In Attention

The ranked review surface SHALL preserve the `corpus_contradictions` queue's
asserted-before-proximity emission order as intra-queue rank, so an authored
`contradicts` pair receives a better rank — and therefore a larger Reciprocal Rank
Fusion vote — than any proximity pair from the same queue. The composition SHALL
require no separate ranking rule for asserted entries: emission order remains rank,
exactly as it is for every other queue.

Each surfaced item's reasons SHALL carry the contributing finding's
`meta.provenance` unchanged, so a reader can tell an asserted conflict from a
measured adjacency without leaving the review surface. Asserted entries SHALL carry
the same stable review reference, fingerprint, and triage contract as every other
attention item, and SHALL be dismissable, snoozable, and reopenable through the same
path.

An asserted entry SHALL NOT be mistaken for the contradiction queue's trailing
upstream-cap summary finding: only a finding carrying an upstream truncation count is
folded into `upstream_truncated`.

#### Scenario: Asserted contradiction outranks a proximity contradiction

- **WHEN** the contradiction queue contributes one asserted pair and one proximity
  pair over otherwise unflagged anchors
- **THEN** the asserted pair's item scores above the proximity pair's item
- **AND** its contradiction reason carries `meta.provenance: "asserted"`

#### Scenario: Asserted entries are triageable like any item

- **WHEN** an asserted contradiction item is dismissed through the normal triage path
- **THEN** it leaves the default open view and resurfaces only when either endpoint's
  content changes

### Requirement: Competing Is A Review State Honored By Every Queue

`competing` SHALL be a valid review action and a valid review view alongside `open`,
`all`, `snoozed`, and `dismissed`. An item whose effective state is `competing` SHALL
be excluded from the default open view by the same state filter that excludes
dismissed and snoozed items, so every queue built on the review-state store honors it
without queue-specific code. The reported state summary SHALL count `competing`
alongside the existing states.

For a contradiction item, an item-level review decision SHALL take precedence over
the pair stance; the pair stance SHALL be consulted only when no item-level decision
applies. An item carrying several contradiction pairs SHALL resolve to `competing`
only when EVERY one of its pairs is stanced, because one un-stanced rival is still
open review work. `reopen` on such an item SHALL clear both the item-level record and
the stance on every pair it carries.

Each contradiction reason SHALL carry a reference addressing its own pair's stance,
and a reason whose pair carries a stance SHALL be marked as such, so a partially
stanced item cannot present as an ordinary open item while one of its conflicts is
already dispositioned and suppressing a write-time warning. These annotations MUST
NOT alter the item's review identity or signal fingerprint.

#### Scenario: A competing item leaves the open view

- **WHEN** a contradiction pair carries a `competing` stance
- **THEN** the default open attention view omits it and the state summary counts it
  under `competing`
- **AND** requesting the `competing` view returns it

#### Scenario: Item-level dismissal takes precedence

- **WHEN** a contradiction item carries both a matching item-level dismissal and a
  matching pair stance
- **THEN** its effective state is `dismissed`

#### Scenario: One un-stanced pair keeps the item open

- **WHEN** an item carries two contradiction pairs and only one of them is stanced
- **THEN** the item remains in the default open view
- **AND** the stanced reason is marked as stanced while the other is not
- **AND** every contradiction reason carries a reference to its own pair's stance

#### Scenario: Reopen restores the item to the open view

- **WHEN** `reopen` is applied to a contradiction item that carries a pair stance
- **THEN** the item returns to the default open view
