## ADDED Requirements

### Requirement: Default Audit Output Is Action-First

Audit product commands SHALL default to an action-first projection. Current non-grandfathered blockers MUST be ordered first, malformed and unregistered semantic work next, other current findings after that, and grandfathered `RELATION_DISPOSITION_MISSING` debt SHALL be represented as grouped backlog information rather than hundreds of actionable errors.

#### Scenario: Current blockers coexist with legacy backlog
- **WHEN** an audit observes a small number of current blockers and hundreds of grandfathered missing-disposition findings
- **THEN** every current blocker appears before the grouped backlog
- **AND** the backlog reports observed count, omissions, completion state, and deterministic representative samples

#### Scenario: Grandfathered missing disposition does not block current work
- **WHEN** a missing-disposition finding is marked grandfathered and is not a current mutation precondition
- **THEN** its audit presentation severity is info/backlog
- **AND** current semantic write enforcement is unchanged

### Requirement: Audit Prioritizes Before Bounding

For the default bounded semantic posthoc projection, the system SHALL prioritize current actionable findings before applying finding-count or byte bounds. Summary and omission counters MUST describe the complete evaluated batch even when individual findings are omitted.

#### Scenario: Legacy findings exceed the default cap
- **WHEN** grandfathered findings alone exceed the default semantic finding cap and current blockers occur later in path order
- **THEN** current blockers remain in the retained projection
- **AND** omitted legacy findings are reflected in grouped and truncation metadata

### Requirement: Full Audit Enumeration Is Explicit

`detail="full"` SHALL request the full raw finding enumeration for audit, including original categories, metadata, and truncation/omission facts. `review_memory(mode="audit")` and `maintain_memory(mode="audit")` SHALL forward the same detail and sampling controls and remain read-only.

#### Scenario: Caller asks for full detail
- **WHEN** audit is called with `detail="full"`
- **THEN** individual grandfathered findings are returned rather than only representative samples
- **AND** no repair or mutation occurs

#### Scenario: Diagnostics contend with a writer
- **WHEN** a full audit overlaps a live mutation
- **THEN** the audit does not acquire or retain the mutation boundary
- **AND** it cannot cause a post-commit mutation to return `MUTATION_BUSY`
