## MODIFIED Requirements

### Requirement: Opaque Planning reference contract
A Record collection manifest MAY store one or more opaque Planning references paired with bounded Records query descriptors. A Record MAY link to a plan, goal, initiative, protocol, project, asset, person, entity, or decision. A Planning reference MAY additionally carry a bounded `join`: one to four pairs mapping a declared record field to a plan field name, where the plan-side name is bounded non-empty text that Records does not check against the target. Records SHALL validate, round-trip, and governance-project these descriptors and the join without resolving Planning, comparing intent with observations, copying record history, inferring progress/completion, or mutating either side. The join is an authored declaration consumed only by the attention surface's `unreflected_outcomes` family; no Records operation SHALL resolve it.

#### Scenario: Planning link round-trips without resolution
- **WHEN** a manifest stores an opaque Planning reference plus a bounded query descriptor
- **THEN** inspection returns the authorized descriptor unchanged and Records performs no Planning lookup or planned-versus-recorded comparison

#### Scenario: Join round-trips without resolution
- **WHEN** a manifest's Planning reference carries a join from a declared record field to a plan field
- **THEN** inspection returns the join unchanged, `describe` documents the shape, and Records performs no lookup of the referenced Planning collection

#### Scenario: Malformed join refuses
- **WHEN** a join names a record field the schema does not declare, has more than four pairs, or has an empty plan-side name
- **THEN** manifest validation refuses before acceptance and names the offending pair

#### Scenario: External software execution truth remains external
- **WHEN** a software initiative links to an accepted OpenSpec change, git state, tests, or deployment result
- **THEN** Records may preserve the observed outcome and Planning may preserve intent, while repository/OpenSpec artifacts remain execution truth
