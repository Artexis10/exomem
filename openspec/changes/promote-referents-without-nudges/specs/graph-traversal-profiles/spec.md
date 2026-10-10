## MODIFIED Requirements

### Requirement: Built-in deterministic traversal profiles
The system SHALL provide immutable built-in `epistemic`, `provenance`, `causal`,
`decision`, `entity` and `all` profiles, shipped as a core vocabulary pack rather
than defined in code. Each profile SHALL define relation families, edge
directions, extension-parent expansion, deterministic priority, and bounded
defaults, and MAY declare the activation anchor kinds it serves from the closed
anchor-kind set. The `entity` profile SHALL extend `epistemic` and add the core
families that relate entities, including ownership, membership, location,
kinship and affiliation. A traversal SHALL pass the selected profile's allowed
relation keys to its neighbour-edge query, so that the row bound applies only to
edges the profile can use. Omitting a profile SHALL preserve current broad
context behavior by using `all`.

#### Scenario: Epistemic lens excludes unrelated operational edges
- **WHEN** context is requested with `traversal_profile="epistemic"`
- **THEN** support, contradiction, refinement, duplication, supersession,
  question, and answer families are traversed within bounds
- **AND** unrelated implementation or generic link edges are excluded and the
  resolved profile is named in the response

#### Scenario: Entity lens follows ownership and its extensions
- **WHEN** context is requested with `traversal_profile="entity"` from a person
  entity that `owns` one entity and reaches another through a vault extension
  whose parent is `owns`
- **THEN** both edges are traversed within bounds and the response names the
  `entity` profile

#### Scenario: A busy page keeps its typed edges
- **WHEN** a person entity has more than 256 incident edges, most of them
  outside the `entity` profile, and one `owns` edge
- **THEN** context with `traversal_profile="entity"` still traverses the `owns`
  edge

#### Scenario: Pack and former code agree
- **WHEN** the pack-defined built-in profiles are loaded on an unchanged vault
- **THEN** every built-in profile that existed before resolves to the same
  families, direction, priority and bounds as before
