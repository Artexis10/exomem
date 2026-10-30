## MODIFIED Requirements

### Requirement: Published catalogue admission survives strict readiness demotion
After managed warm-up, ordinary recall SHALL re-prove a policy-compatible published catalogue even when strict live-projection readiness has been demoted. Every catalogue read in that request SHALL use the admitted checkpoint, with exact pending custody overlaid. A stale projection SHALL load its paths from the matching catalogue rather than walking current canonical files. Request-local admission SHALL NOT promote strict health readiness or escape into another request, vault or catalogue scope. Strict health retrieval readiness (`ready`, `admitted=true`) SHALL stay strict; only the serving status MAY follow a servable lag, after this process has made a strict proof, as `install-readiness` defines.

#### Scenario: Repair demotes readiness while a pending edit is durable
- **WHEN** a compatible published catalogue exists, a committed edit has exact pending custody, and repair or health has demoted strict readiness
- **THEN** keyword and hybrid recall return the committed edit with projection lag disclosed, without walking the corpus

#### Scenario: Identity changes after request admission
- **WHEN** access policy, semantic catalogue identity or the published content checkpoint changes before an admitted catalogue read
- **THEN** that read refuses rather than serving rows under the previous proof

#### Scenario: Nested recall or an exception ends a request
- **WHEN** another recall begins inside a request or a request exits through an exception
- **THEN** its catalogue binding is isolated and the prior scope is restored without leaking admission to later unscoped queries

#### Scenario: Requested outside-KB widening needs a live catalogue
- **WHEN** ordinary KB recall is admitted but the vault catalogue used by an explicitly requested widening is lagging
- **THEN** widening declines without a scan and preserves the KB results; a cached result from an earlier live widening cannot bypass that decline

#### Scenario: A served lag does not promote strict readiness
- **WHEN** a proven process serves recall from a published catalogue that trails a live projection and health reports retrieval as `lagging`
- **THEN** health reports `admitted=false` for that retrieval state
- **AND** the request still discloses the projection lag it serves
