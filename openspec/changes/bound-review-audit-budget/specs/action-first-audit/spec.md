## MODIFIED Requirements

### Requirement: Audit Prioritizes Before Bounding

For the default bounded semantic posthoc projection, the system SHALL prioritize current actionable findings before applying finding-count or byte bounds. Summary and omission counters MUST describe the complete evaluated batch even when individual findings are omitted. A deadline-limited report SHALL identify its completed checks and incomplete coverage; its counters SHALL NOT imply that unevaluated checks found nothing.

#### Scenario: Legacy findings exceed the default cap
- **WHEN** grandfathered findings alone exceed the default semantic finding cap and current blockers occur later in path order
- **THEN** current blockers remain in the retained projection
- **AND** omitted legacy findings are reflected in grouped and truncation metadata

#### Scenario: The deadline interrupts a check
- **WHEN** an audit completes one check and interrupts another
- **THEN** the retained findings and counters describe the completed check
- **AND** coverage identifies the interrupted check as incomplete rather than reporting zero findings for it

## ADDED Requirements

### Requirement: Budgeted Audit Coverage Is Explicit

A deadline-limited audit SHALL expose incomplete coverage through its existing coverage carrier and name skipped or truncated stages in its budget block. It SHALL return only findings from completed checks with complete prerequisites. Unknown counts SHALL remain unknown. A complete report that fits its allowance SHALL retain its existing findings and presentation.

#### Scenario: Parsing stops before the corpus is complete
- **WHEN** the audit reads only a prefix of the eligible corpus before its allowance expires
- **THEN** coverage states that the corpus is incomplete
- **AND** no corpus-dependent orphan, absence or link census is reported from that prefix

#### Scenario: Completed findings remain actionable
- **WHEN** a report retains several completed checks and skips later checks
- **THEN** the existing action-first order applies to the completed findings
- **AND** the response does not claim a complete audit of the vault

#### Scenario: The full-detail request runs out of time
- **WHEN** a caller requests full detail and the request allowance expires
- **THEN** full detail does not extend the request deadline
- **AND** the response reports its incomplete coverage or the final release budget error

#### Scenario: An interrupted semantic check leaves no metadata
- **WHEN** a requested semantic check is interrupted before producing its metadata
- **THEN** actionable and full presentation both report its upstream findings as incomplete
- **AND** observation and legacy-backlog completion fields cannot imply that the missing work finished
