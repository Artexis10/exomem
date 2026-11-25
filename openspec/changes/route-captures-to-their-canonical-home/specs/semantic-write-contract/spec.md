## ADDED Requirements

### Requirement: Compact observations lift provenance relations to the page

A compact `add_observation` whose relations all declare a forward `frontmatter_fields` mapping in the relation registry SHALL commit as a compact unit. It SHALL add each target, in canonical link form and without duplicates, to the page field the mapping names, and its outcome line SHALL name each cited page. The decision SHALL read only that registry attribute, never a relation key.

#### Scenario: An observation cites the Source it came from

- **WHEN** an agent adds a compact observation with a `derived_from` link to a saved Source
- **THEN** the observation commits compact
- **AND** the page's `sources` frontmatter lists the Source once
- **AND** the outcome line names the Source

### Requirement: Compact observations refuse other relations whole

A compact `add_observation` that carries any relation without a forward `frontmatter_fields` mapping SHALL refuse as a whole with `COMPACT_RELATIONS_REQUIRE_RICH_KIND`, SHALL name the admitted kinds, and SHALL write nothing.

#### Scenario: A non-provenance relation still needs a rich kind

- **WHEN** a compact observation carries a relation whose registry entry declares no forward frontmatter field
- **THEN** the call refuses with `COMPACT_RELATIONS_REQUIRE_RICH_KIND`, names the admitted kinds, and writes nothing

#### Scenario: A mixed call does not half-apply

- **WHEN** a compact observation carries one liftable and one non-liftable relation
- **THEN** the call refuses and neither the unit nor the page frontmatter changes
