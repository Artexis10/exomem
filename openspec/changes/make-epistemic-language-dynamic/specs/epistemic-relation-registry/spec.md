## ADDED Requirements

### Requirement: Core relations carry closed behaviours
A core relation entry SHALL carry a `behaviours` list whose values come from the closed relation-behaviour set that the registry schema declares and code implements. A namespaced extension SHALL inherit its core parent's behaviours and SHALL NOT declare its own. Code SHALL decide what a relation does only from its behaviours, never from a relation key or a family value. A pack or extension that names an undeclared behaviour, or an extension that sets `behaviours`, SHALL fail validation with a finding that names the relation and the field. The shipped pack SHALL reproduce the behaviour of the hard-coded relation sets it replaces.

#### Scenario: An extension inherits its parent's behaviours
- **WHEN** a valid extension `legal.qualifies` declares parent `refines`, and `refines` carries `lifts`
- **THEN** a `legal.qualifies` unit relation may become a page proposal, with no code change

#### Scenario: An extension cannot declare behaviours
- **WHEN** a proposed extension sets `behaviours: [settles]`
- **THEN** validation refuses the save with a finding that names the extension and its `behaviours` field
- **AND** the current registry stays unchanged

#### Scenario: Shipped behaviour is unchanged
- **WHEN** behaviour reads replace the hard-coded lift, membership, resolution, prediction-evidence and provenance sets
- **THEN** lift proposals, membership answers, prediction and question audits, resolutions and activation counts over a fixture vault are identical before and after

### Requirement: Precedes and follows order events
The core registry SHALL include `precedes` and `follows` as one inverse pair in family `order`, each carrying the `orders` behaviour. The context compiler SHALL present units joined by these relations in their stated order.

#### Scenario: Stated order survives compilation
- **WHEN** one timeline event `precedes` another
- **THEN** compiled context presents the first event before the second

### Requirement: Epistemic dimensions are registry data
The system SHALL keep epistemic dimensions in a vocabulary registry with shipped defaults and vault additions. A dimension entry SHALL carry a key, labels per language, a description, its members and its display behaviours. Members SHALL be relation families, unit kinds and governed metadata keys, and one member MAY belong to several dimensions. Retrieval SHALL filter by any dimension, the context compiler SHALL group by any dimension, and reviews and reports SHALL queue by any dimension. Display behaviours SHALL come from a closed set that the registry schema declares: `lead_with_current`, `show_origin`, `flag_open` and `order_in_time`. A display behaviour SHALL change only the order and annotation of compiled context. It SHALL NOT hide a unit, change a unit's standing or change what a word means. Code SHALL NOT hold a dimension list or branch on a dimension key.

#### Scenario: A vault adds a dimension
- **WHEN** a reviewed registry save adds dimension `regulation` with kinds `constraint` and `requirement` as members
- **THEN** retrieval can filter by `regulation`, and a per-dimension report shows a `regulation` row, with no code change

#### Scenario: An added dimension borrows a display behaviour
- **WHEN** dimension `regulation` names `show_origin`
- **THEN** compiled context shows where each `regulation` member came from, with no code change

#### Scenario: Leading with the current view hides nothing
- **WHEN** dimension `currency` names `lead_with_current`, and one decision supersedes another
- **THEN** compiled context places the current decision first and marks the earlier one as replaced
- **AND** the earlier decision is still present in the context

#### Scenario: An undeclared display behaviour is refused
- **WHEN** a registry save gives a dimension the display behaviour `hide_refuted`
- **THEN** validation refuses the save with a finding that names the dimension and the value
