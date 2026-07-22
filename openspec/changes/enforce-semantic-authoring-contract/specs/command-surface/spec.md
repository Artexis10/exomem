## ADDED Requirements

### Requirement: Authoring Descriptions Project The Canonical Contract

The single command registry SHALL project the canonical concise semantic-authoring
contract into the descriptions and applicable parameter guidance for `remember`,
`replace_memory`, `observe_memory`, `edit_memory` unit removal/activation, and the
create/overwrite/append behavior of `manage_memory_file`. MCP, REST, CLI help, OpenAPI, and generated capability
documentation SHALL inherit that registry text. No facade SHALL maintain a
separate authoring rule or omit the minimum-unit rule from a path that can create or
activate compiled notes.

#### Scenario: Remember content guidance is exact
- **WHEN** the generated `remember` schema or help is inspected
- **THEN** `content` guidance includes `## Observations`, `- [category] content #tags (context) ^anchor`, the open-category rule, the one-valid-unit active-note minimum, and the non-empty rich alternative

#### Scenario: Tier-2 guidance names shared enforcement
- **WHEN** the generated `manage_memory_file` create/overwrite/append description is inspected
- **THEN** it states that compiled-note destinations receive the same semantic precommit contract and points to the typed writer remediation

#### Scenario: Edit guidance covers removal and activation
- **WHEN** generated `edit_memory` guidance is inspected
- **THEN** it states that an edit cannot remove a post-activation page's final valid unit and that inactive-to-active transitions must satisfy the minimum

#### Scenario: Surface fidelity detects drift
- **WHEN** MCP descriptions, CLI help, REST/OpenAPI schemas, capability docs, and committed schema fixtures are compared with the registry
- **THEN** any missing, stale, or independently edited authoring contract fails the existing surface-fidelity gates

### Requirement: Semantic Authoring Failures Preserve One Envelope

All public facades SHALL preserve semantic-authoring validation failures from the
shared writer using the existing result/error envelope. Stable code,
source-addressed findings where available, canonical remediation, validation-only
state, and mutation status SHALL survive without facade-specific rewriting.

#### Scenario: Missing semantic unit fails identically
- **WHEN** the same active note with no valid compact or rich unit is submitted through MCP, REST, and CLI JSON
- **THEN** each response carries `missing_semantic_unit`, canonical compact/rich remediation, and an explicit non-mutated result

#### Scenario: Empty rich unit fails identically
- **WHEN** an applicable in-process write contains an empty recognized rich block
- **THEN** every facade carries `empty_rich_unit` with its heading location and no facade commits or indexes that unit
