## MODIFIED Requirements

### Requirement: Versioned context-role registry
The product SHALL ship a versioned context-role registry (`context-roles.yaml`) in the
skill scaffold and the Claude Code plugin, loaded by the server, declaring for each
role an identifier, a description, the retrieval lane it maps to, the semantic-unit
category set it selects, and the anchor kinds for which it is a default. The initial
vocabulary SHALL be `identity, preferences, constraints, resources, current_state,
recent_change, active_plans, methods, precedents, people, location, baseline,
evidence, open_questions, connections`, where `connections` maps to the `graph` lane and
is a default for every anchor kind. A vault override MAY add roles, categories or cues and MAY
narrow defaults, but SHALL NOT remove or rename a shipped role. A registry that fails
to load SHALL fall back to the shipped registry and report the failure in the packet's
`generation` block.

#### Scenario: Override adds a role without removing one
- **WHEN** a vault override declares a new role `logistics` and omits `people`
- **THEN** the effective registry contains both `logistics` and `people`

#### Scenario: Broken override falls back
- **WHEN** the vault override is not valid YAML
- **THEN** activation uses the shipped registry and the packet reports
  `generation.roles_source = "shipped"` with a load warning

#### Scenario: The connections role ships on the graph lane
- **WHEN** a vault has no override
- **THEN** the effective registry contains `connections` with lane `graph`, and a resolved anchor
  of any kind selects it

#### Scenario: The connections role takes no lens slot
- **WHEN** a resolved anchor's defaults and a carried page's `material` together fill the six
  lenses that spend the unit allowance
- **THEN** `connections` is still selected beside them, because it serves only bounded pointers,
  and no lens is reported as `role_limit` on its account
