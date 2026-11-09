## ADDED Requirements

### Requirement: Units Filter By Epistemic Dimension
The structured-filter field registry SHALL accept `unit.dimension` as a closed field that takes a dimension key or a label in any language, with `$eq` for belonging to a dimension, `$in` for belonging to one of several, and `$ne` for not belonging to one. It SHALL reject every other operator and every operand that is not a string. A name that no dimension entry holds SHALL be rejected with the keys that exist. Membership SHALL come from the one membership rule of the dimension registry, so a dimension that a vault adds is filterable with no code change. A dimension whose membership depends on typed edges SHALL read those edges once for the whole request, SHALL admit an edge only when the caller may read both of its pages, and SHALL raise the typed warming outcome while the graph is missing or stale, never an empty answer. A served reader that cannot evaluate the field SHALL refuse it by field name with `UNSUPPORTED_FILTER_FIELD`, as it refuses a frontmatter pointer.

#### Scenario: Filtering by a dimension a vault added
- **WHEN** a vault dimension `regulation` has the kinds `constraint` and `requirement` as members, and a request filters units by `{"unit.dimension": {"$eq": "regulation"}}`
- **THEN** the request returns the units of those kinds and no others, with no code change

#### Scenario: A label resolves to its dimension
- **WHEN** a request filters by the label `Regulation` of that dimension
- **THEN** it returns the same units as the key does

#### Scenario: An unknown dimension is refused
- **WHEN** a request filters by a name that no dimension entry holds
- **THEN** the request fails with an invalid-value error that lists the existing dimension keys

#### Scenario: A missing graph never reads as no edges
- **WHEN** a request filters by a dimension that has a family member, and the graph sidecar is missing
- **THEN** the request raises the typed warming outcome and returns no hits as an answer

#### Scenario: A served reader refuses the field
- **WHEN** a served request names `unit.dimension` in a filter
- **THEN** the request is refused with `UNSUPPORTED_FILTER_FIELD`, and no corpus scan runs
