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

#### Scenario: Declared companion execution truth remains external
- **WHEN** an initiative links to an artifact whose type the resolved workflow contract assigns to a companion
- **THEN** Records may preserve an observed outcome and Planning may preserve intent, while the companion artifact remains opaque external execution truth

#### Scenario: Pointer kind alone declares no authority
- **WHEN** a Record or linked plan contains an OpenSpec, repository, issue, calendar, or unfamiliar companion pointer without a resolved workflow contract
- **THEN** Records treats the pointer as opaque data and does not privilege that system, infer its state, or choose it as execution authority

## ADDED Requirements

### Requirement: Observed outcomes close a contract-aware Planning feedback loop

When a resolved workflow contract and prominence policy permit observed-outcome capture, a sufficiently identified report that work was produced, delivered, approved, published, failed, cancelled, or otherwise happened SHALL route to one compatible Records collection without requiring a magic log verb. The Record MAY carry opaque Planning and companion references. Records SHALL remain neutral observed state; it SHALL NOT infer success, completion, health, authority, or a Planning transition.

#### Scenario: Shipped companion work becomes one observed event
- **WHEN** a user reports that work represented by a plan and companion artifact shipped
- **THEN** the agent appends one compatible Record linked opaquely to both references and does not copy the companion artifact into Records

#### Scenario: Outcome prompts rather than invents a transition
- **WHEN** an observed outcome joins to an open plan and the contract posture is `propose-after-outcome`
- **THEN** the review surface may prompt an explicit Planning transition while Records and Planning remain unchanged until the user decides

#### Scenario: Explicit user closure has two governed consequences
- **WHEN** the user explicitly states both the observed outcome and that the intended work is complete
- **THEN** the agent records the outcome and performs one guarded Planning transition, reporting both consequences without treating either store as a mirror of the other

### Requirement: External execution truth is declared rather than hard-coded

Records SHALL treat every companion artifact as an opaque external reference governed by the resolved workflow contract. The product SHALL NOT privilege OpenSpec, git, an issue tracker, calendar, or any other companion in Records semantics. A companion declaration SHALL NOT make its reported state an observed Record until the user or an authorized tool interaction supplies a concrete event in the active task context.

#### Scenario: Different tools preserve the same Records semantics
- **WHEN** two scopes use different companion tools for the same declared execution ownership
- **THEN** observed outcomes use the same neutral Record schema and differ only in their opaque companion references
