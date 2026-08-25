## ADDED Requirements

### Requirement: Runtime readiness includes retrieval admission

The `/health/ready` response SHALL include a content-free `retrieval` block describing whether the process can admit ordinary maintained-catalog lexical recall. Overall runtime status SHALL be `not_ready` while startup catalog warm-up is active but incomplete or has failed, and SHALL include a stable retrieval reason. The response MUST NOT expose vault paths, queries, corpus counts, note metadata, or catalog contents. `/health` liveness SHALL remain independent.

#### Scenario: Catalog warming withholds readiness

- **WHEN** the transport is live and the maintained lexical catalog startup phase has not completed
- **THEN** `/health` returns its normal live response
- **AND** `/health/ready` reports `not_ready` with retrieval state `warming`
- **AND** no vault content or identity is exposed

#### Scenario: Catalog readiness admits the runtime

- **WHEN** maintained lexical catalogs for both recall scopes are current
- **THEN** the retrieval block reports `ready` and read admission is true
- **AND** retrieval contributes no not-ready reason

#### Scenario: Explicitly lazy startup is truthful

- **WHEN** startup warm-up is explicitly disabled and no maintained-catalog readiness has been established
- **THEN** retrieval reports `unverified`
- **AND** overall readiness does not claim ordinary retrieval admission

#### Scenario: Later catalog staleness revokes admission

- **WHEN** a catalog previously marked ready is later proven stale or fatally unavailable
- **THEN** retrieval admission is revoked before background repair is scheduled
- **AND** readiness remains withheld until a successful repair proves both recall scopes current again
