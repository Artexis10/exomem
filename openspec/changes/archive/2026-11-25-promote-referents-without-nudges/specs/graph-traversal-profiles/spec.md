## MODIFIED Requirements

### Requirement: Built-in deterministic traversal profiles
The system SHALL provide immutable built-in `epistemic`, `provenance`, `causal`,
`decision`, and `all` profiles, shipped as a core vocabulary pack rather than
defined in code. Each profile SHALL define relation families,
edge directions, extension-parent expansion, deterministic priority, and bounded
defaults. Omitting a profile SHALL preserve current broad context behavior by
using `all`.

#### Scenario: Epistemic lens excludes unrelated operational edges
- **WHEN** context is requested with `traversal_profile="epistemic"`
- **THEN** support, contradiction, refinement, duplication, supersession,
  question, and answer families are traversed within bounds
- **AND** unrelated implementation or generic link edges are excluded and the
  resolved profile is named in the response
