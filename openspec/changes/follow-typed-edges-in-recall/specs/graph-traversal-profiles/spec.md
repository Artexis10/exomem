## ADDED Requirements

### Requirement: Shipped recall profile
The core vocabulary pack SHALL ship a `recall` traversal profile whose families are those of
`epistemic` plus the link, entity, derivation, evidence and citation families. Activation SHALL
use it for anchor neighbourhoods. A vault overlay MAY extend it like any shipped profile, and it
SHALL obey the existing bounds, determinism and read-only rules.

#### Scenario: Recall follows a note to its source
- **WHEN** context is requested with `traversal_profile="recall"` from a note that authors
  `derived_from` to a source and `links_to` to another note
- **THEN** both edges are traversed within bounds and the response names the `recall` profile

#### Scenario: Recall still excludes operational edges
- **WHEN** context is requested with `traversal_profile="recall"` from a page whose only edge is
  `implements`
- **THEN** that edge is not traversed
