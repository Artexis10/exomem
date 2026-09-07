## ADDED Requirements

### Requirement: Optional graph expansion does not veto admitted ordinary recall
Ordinary recall SHALL continue over its admitted retrieval projection and exact pending-write overlay when optional graph expansion cannot obtain a current resolver. The response SHALL disclose omitted graph work. Policy, pending-visibility, catalogue and graph-dependent relation/filter proofs SHALL remain mandatory; unproven graph edges SHALL NOT be represented as current.

#### Scenario: Resolver lags an ordinary write
- **WHEN** a managed hybrid lookup has valid catalogue/pending visibility but its optional resolver checkpoint is stale
- **THEN** it returns the eligible direct matches with graph warming disclosed and performs no whole-vault resolver scan on the reader

#### Scenario: Relation semantics need the pending graph
- **WHEN** the caller requests a relation-dependent predicate whose graph proof is unavailable
- **THEN** retrieval retains its explicit warming/refusal rather than returning an incomplete relation answer

#### Scenario: Pending coverage is unprovable
- **WHEN** a committed write cannot be represented by a valid bounded pending overlay
- **THEN** ordinary managed retrieval still refuses before consulting a stale catalogue

### Requirement: Published catalogue admission survives strict readiness demotion
After managed warm-up, ordinary recall SHALL re-prove a policy-compatible published catalogue even when strict live-projection readiness has been demoted. Every catalogue read in that request SHALL use the admitted checkpoint, with exact pending custody overlaid. A stale projection SHALL load its paths from the matching catalogue rather than walking current canonical files. Request-local admission SHALL NOT promote strict health readiness or escape into another request, vault or catalogue scope.

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
