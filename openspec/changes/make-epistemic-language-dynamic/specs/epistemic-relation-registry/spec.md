## ADDED Requirements

### Requirement: Core relations carry closed behaviours
A core relation entry SHALL carry a `behaviours` list whose values come from the closed relation-behaviour set that the registry schema declares and code implements. A namespaced extension SHALL inherit its core parent's behaviours and SHALL NOT declare its own. The relation sets this change replaces SHALL become behaviour reads, and any remaining branch on a relation key or family value SHALL be named as debt. `presentation_role` stays renderer metadata for how a viewer draws an edge, and behaviours decide what code does with it. A pack or extension that names an undeclared behaviour, or an extension that sets `behaviours`, SHALL fail validation with a finding that names the relation and the field. The shipped pack SHALL reproduce the behaviour of the hard-coded relation sets it replaces.

#### Scenario: An extension inherits its parent's behaviours
- **WHEN** a valid extension `legal.qualifies` declares parent `refines`, and `refines` carries `lifts`
- **THEN** a `legal.qualifies` unit relation may become a page proposal, with no code change

#### Scenario: An extension cannot declare behaviours
- **WHEN** a proposed extension sets `behaviours: [settles]`
- **THEN** validation refuses the save with a finding that names the extension and its `behaviours` field
- **AND** the current registry stays unchanged

#### Scenario: Shipped behaviour is unchanged for core relations
- **WHEN** behaviour reads replace the hard-coded lift, membership, resolution, prediction-evidence and provenance sets
- **THEN** lift proposals, membership answers, prediction and question audits, resolutions and activation counts for core relations over a fixture vault are identical before and after
- **AND** an extension of `supports` in the fixture now counts toward settling a prediction, as its parent does

### Requirement: Precedes and follows order events
The core registry SHALL include `precedes` and `follows` as one inverse pair in family `order`. `precedes` SHALL carry the `earlier_than_target` behaviour and `follows` SHALL carry the `later_than_target` behaviour. The `order_in_time` display behaviour SHALL read edges with either behaviour and `occurred_at` values; no other rule orders events.

#### Scenario: Stated order survives compilation
- **WHEN** context is grouped by dimension `order`, and one timeline event `precedes` another
- **THEN** compiled context presents the first event before the second

#### Scenario: A follows edge orders the same way
- **WHEN** context is grouped by dimension `order`, and event B `follows` event A
- **THEN** compiled context presents A before B

### Requirement: Epistemic dimensions are registry data
The system SHALL keep epistemic dimensions in a vocabulary registry with shipped defaults and vault additions. A dimension entry SHALL carry a key, labels per language, a description, its members and its display behaviours. Members SHALL be relation families, unit kinds and governed metadata keys, and one member MAY belong to several dimensions. A unit SHALL belong to a dimension when its kind is a member, when it carries a member key, or when it is an endpoint of an edge whose family is a member. Retrieval SHALL filter by any dimension, the context compiler SHALL group by any dimension, and reviews and reports SHALL queue by any dimension. A display behaviour SHALL apply only within its own dimension's group, and a unit that belongs to several requested dimensions SHALL appear in full in the first requested dimension and as a ref in the others. Display behaviours SHALL come from a closed set that the registry schema declares: `lead_with_current`, `show_origin`, `flag_open` and `order_in_time`. A display behaviour SHALL change only the order and annotation of compiled context. It SHALL NOT hide a unit, change a unit's standing or change what a word means. Code SHALL NOT hold a dimension list or branch on a dimension key.

#### Scenario: A vault adds a dimension
- **WHEN** a reviewed registry save adds dimension `regulation` with kinds `constraint` and `requirement` as members
- **THEN** retrieval can filter by `regulation`, and a per-dimension report shows a `regulation` row, with no code change

#### Scenario: An added dimension borrows a display behaviour
- **WHEN** dimension `regulation` names `show_origin`
- **THEN** compiled context shows where each `regulation` member came from, with no code change

#### Scenario: Leading with the current view hides nothing
- **WHEN** dimension `currency` names `lead_with_current`, and one decision supersedes another
- **THEN** compiled context places the current decision first
- **AND** the earlier decision stays in the context, marked as replaced, or as a ref when the budget is short

#### Scenario: An undeclared display behaviour is refused
- **WHEN** a registry save gives a dimension the display behaviour `hide_refuted`
- **THEN** validation refuses the save with a finding that names the dimension and the value
