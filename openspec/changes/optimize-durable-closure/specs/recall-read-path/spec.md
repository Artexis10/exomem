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
