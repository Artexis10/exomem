## ADDED Requirements

### Requirement: Planning items may declare motivating knowledge

A Planning item MAY declare an optional `motivation` property: a list of at
most 16 `exomem://memory/<uuid>` references to the knowledge that motivates
the item. Each entry SHALL be validated the same way a `progress_evidence`
collection reference is validated — parsed and refused if malformed — without
resolving the referenced page or checking that it exists. A non-list value, a
list of more than 16 entries, or any entry that is not a well-formed
`exomem://memory/` reference SHALL be refused. Absence of `motivation` SHALL
behave exactly as before this field existed: no default is applied, and no
existing Planning item's validity changes because it omits the field. A
collection MAY accept `motivation` only after declaring it in its manifest
`item_schema.fields`, the same precondition `progress_evidence` and
`execution` already require.

#### Scenario: Valid motivation list is accepted and round-trips
- **WHEN** `add` receives an item with a `motivation` list of one or more
  well-formed `exomem://memory/` references, on a collection whose manifest
  declares `motivation`
- **THEN** the item is captured, the references serialize unchanged into the
  item's frontmatter, and a subsequent query returns the same list

#### Scenario: More than sixteen motivations is refused
- **WHEN** an `add` or `update` request supplies a `motivation` list with
  more than 16 entries
- **THEN** validation refuses before canonical publication

#### Scenario: Malformed motivation reference is refused
- **WHEN** a `motivation` entry is not a well-formed `exomem://memory/<uuid>`
  reference — including an `exomem://plan/...` reference to another Planning
  item
- **THEN** validation refuses the same way an invalid `progress_evidence`
  collection reference is refused

#### Scenario: Non-list motivation is refused
- **WHEN** `motivation` is supplied as a value other than a list
- **THEN** validation refuses before it reaches generic schema type-checking

#### Scenario: Absence of motivation behaves exactly as before
- **WHEN** an item omits `motivation` entirely
- **THEN** capture, update, triage, and query behave exactly as they did
  before this field existed, with no key defaulted in

### Requirement: Motivation is queryable and never becomes a relation or recall edge

Planning queries SHALL support selecting items by a motivating reference
through the existing generic filter mechanism: a filter on the `motivation`
column selects every item whose `motivation` list contains the given
reference, on any collection whose manifest declares `motivation`. This
capability SHALL NOT require a new top-level query parameter, since the
existing `filters` argument already expresses it. `motivation` SHALL NOT
participate in Planning's parent/area relation graph — it SHALL NOT satisfy a
required `parent` or `area` relation, and it SHALL NOT be read when computing
hierarchy edges. Because raw Planning items never enter ordinary semantic
recall or the relation graph regardless of field content, a plan carrying
`motivation` SHALL NOT appear as a memory hit and SHALL NOT create a graph
edge toward the memory it cites. Plans cite knowledge; knowledge never cites
plans back through this field.

#### Scenario: Motivation query filter selects the referencing items
- **WHEN** a query supplies a filter selecting Planning items whose
  `motivation` contains a given `exomem://memory/` reference
- **THEN** only items whose `motivation` list contains that reference are
  returned

#### Scenario: Motivation does not satisfy a required relation
- **WHEN** a committed initiative or work item supplies `motivation` but
  omits the `parent` its commitment requires
- **THEN** validation refuses the missing relation exactly as it would
  without `motivation` present

#### Scenario: Motivated plans remain outside recall and the graph
- **WHEN** a Planning item declares one or more `motivation` references
- **THEN** the item remains excluded from ordinary semantic recall and from
  the relation graph exactly as every other raw Planning item is, and no new
  graph edge is created toward the referenced memory
