# context-roles Specification

## Purpose
Define the versioned registry of context roles (identity, preferences, constraints, resources, current state and the rest) that the context compiler selects per turn from anchor-kind defaults and deterministic turn cues, shipped as a scaffold file with per-vault override, so that which durable context a packet carries is declared and reviewable rather than implied by code.

## Requirements

### Requirement: Versioned context-role registry
The product SHALL ship a versioned context-role registry (`context-roles.yaml`) in the
skill scaffold and the Claude Code plugin, loaded by the server, declaring for each
role an identifier, a description, the retrieval lane it maps to, the semantic-unit
category set it selects, and the anchor kinds for which it is a default. The initial
vocabulary SHALL be `identity, preferences, constraints, resources, current_state,
recent_change, active_plans, methods, precedents, people, location, baseline,
evidence, open_questions`. A vault override MAY add roles, categories or cues and MAY
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

### Requirement: Deterministic role selection
Roles for a turn SHALL be selected as the union of the defaults for each resolved
anchor's kind and the roles whose cue patterns match the turn, evaluated with the same
normalisation the anchor resolver uses, with no model call and no randomness; the
selected set SHALL be bounded (at most 6 roles) in the registry's priority order, and
the packet SHALL list the selected roles and, for each, whether it came from an anchor
default or a turn cue.

#### Scenario: Planning verbs add plan and method roles
- **WHEN** a turn contains a planning cue ("I'm planning to", "how should I") and a
  resource anchor resolves
- **THEN** the selected roles include `resources`, `current_state`, `constraints`
  (anchor defaults) and `active_plans`, `methods` (cues), in registry order

#### Scenario: Same turn, same roles
- **WHEN** the same turn is activated twice against the same index generation and
  registry hash
- **THEN** the selected roles and their attributions are identical

### Requirement: Review-gated evolution
Changes to the role vocabulary, category sets or cue table SHALL happen only through
a shipped registry revision or an explicit vault override authored by the owner; no
server component SHALL mutate the registry, and any future proposal to extend it
(for example from a consolidation sensor) SHALL enter a review queue rather than the
registry. The registry hash SHALL be part of the packet's `generation` block so a
change in roles is visible in every packet built from it.

#### Scenario: Registry hash changes with the registry
- **WHEN** a vault override adds a cue pattern
- **THEN** the next packet's `generation.roles_hash` differs from the previous one
