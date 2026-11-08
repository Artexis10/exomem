## ADDED Requirements

### Requirement: Relation families carry behaviours and a dimension
Every relation family in the registry SHALL name zero or more behaviours from the closed behaviour set that the registry schema declares, and exactly one dimension key from the dimension registry. Code SHALL decide what a relation does only from its family's behaviours, never from a relation, family or dimension key. Validation SHALL refuse a family that names an undeclared behaviour or no dimension, and the error SHALL name the family and the missing field. The shipped pack SHALL reproduce the behaviour that hard-coded family sets give today.

#### Scenario: A new family works through a known behaviour
- **WHEN** a vault adds relation `qualifies` in a new family `qualification` that names behaviour `narrows` and dimension `correction`
- **THEN** code treats a `qualifies` edge as narrowing its target with no code change
- **AND** a report that groups by dimension files it under `correction`

#### Scenario: A family without a dimension is refused
- **WHEN** a registry save adds a family that names no dimension
- **THEN** validation refuses the save and names the family and the missing `dimension` field

#### Scenario: Shipped behaviour is unchanged
- **WHEN** the shipped pack replaces the hard-coded lift and membership sets
- **THEN** lift proposals and membership answers over a fixture vault are identical before and after

### Requirement: Dimensions are registry data
The system SHALL keep epistemic dimensions in a vocabulary registry with shipped defaults and vault additions. Each dimension SHALL carry a key, labels per language, a description and zero or more behaviours from the closed behaviour set. Retrieval, the context compiler, reviews and reports SHALL filter, group and queue by any dimension key, and SHALL apply a dimension's behaviours to the words in it. Relation families, unit kinds and verdicts SHALL each name one dimension. Reports and retrieval profiles SHALL read dimensions from the registry and SHALL NOT hold a dimension list in code.

#### Scenario: A vault adds a dimension
- **WHEN** a vault adds dimension `regulation` and moves family `constraint` into it
- **THEN** a per-dimension report shows a `regulation` row with no code change

#### Scenario: An added dimension borrows a shipped behaviour
- **WHEN** a vault adds dimension `regulation` that names behaviour `cites`
- **THEN** the context compiler carries the source with every claim in that dimension, with no code change
