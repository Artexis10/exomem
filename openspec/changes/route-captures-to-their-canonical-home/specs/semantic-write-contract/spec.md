## ADDED Requirements

### Requirement: Compact observations lift source citations to the page

A compact `add_observation` SHALL commit as a compact unit when every relation it carries has the registry `sources` behaviour, a forward page field on its core relation (a vault extension takes its parent's), and a captured-material target. It SHALL add each target to that page field without duplicates, and read only registry attributes, never a relation key.

#### Scenario: An observation cites the Source it came from

- **WHEN** an agent adds a compact observation with a `derived_from` link to a saved Source
- **THEN** the observation commits compact
- **AND** the page's `sources` frontmatter lists the Source once
- **AND** the outcome line and the compact reply's `user_summary` name the Source

#### Scenario: A preview states what a commit will cite

- **WHEN** an agent validates the same observation
- **THEN** the page is unchanged and the outcome line says in the future tense which Source the commit will cite

#### Scenario: A Source the page already cites is not cited twice

- **WHEN** the page's `sources` already lists the same Source as a title link, an aliased path link or a stable memory reference
- **THEN** the page's `sources` is unchanged

### Requirement: Compact observations refuse other relations whole

A compact `add_observation` that carries any relation outside that rule SHALL refuse as a whole with `COMPACT_RELATIONS_REQUIRE_RICH_KIND`, SHALL name the admitted kinds, and SHALL write nothing. An update of a rich unit that omits `kind` keeps its kind and SHALL refuse the same way, so it never moves the unit's relations to the page.

#### Scenario: A relation that is no source citation still needs a rich kind

- **WHEN** a compact observation carries a relation without the `sources` behaviour, such as a supersession, or one whose core relation declares no forward page field
- **THEN** the call refuses with `COMPACT_RELATIONS_REQUIRE_RICH_KIND`, names the admitted kinds, and writes nothing

#### Scenario: A source link to a note that is no captured material still needs a rich kind

- **WHEN** a compact observation carries a `derived_from` link to an ordinary note
- **THEN** the call refuses with `COMPACT_RELATIONS_REQUIRE_RICH_KIND` and writes nothing

#### Scenario: A mixed call does not half-apply

- **WHEN** a compact observation carries one liftable and one non-liftable relation
- **THEN** the call refuses and neither the unit nor the page frontmatter changes

#### Scenario: A rich unit edited without its kind keeps its relations on the unit

- **WHEN** an agent updates a rich unit that carries a `derived_from` relation, sends the relation again and omits `kind`
- **THEN** the call refuses with `COMPACT_RELATIONS_REQUIRE_RICH_KIND` and the page is unchanged
