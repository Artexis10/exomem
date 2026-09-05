## ADDED Requirements

### Requirement: Skill-aware session bootstrap

The system SHALL accept the opt-in bootstrap profile `session` for clients that have already loaded the installed Exomem operating rules and present its metadata `skill_contract`. The server SHALL derive that contract as SHA-256 over canonical JSON mapping logical source paths to LF-normalized text for the core skill, `references/*.md`, and standalone workflow `SKILL.md` files discovered from the canonical manifest, with only `metadata.skill_contract` removed before hashing. It SHALL resolve the handshake before constructing compact state: an absent or mismatched contract SHALL return the actual `compact` profile in the same call with a closed unavailable reason. A valid session request SHALL return live policy and capability state without repeating the generic static authoring contract. It SHALL preserve the compact contract's filtered server identity, active capabilities, engagement and delegation envelope, governance, workflow contracts, relation vocabulary currency, active entity registry, source taxonomy, selected knowledge-pack guidance and selection rule, and due state when present. It SHALL retain the compact due-state and family-disposition post-write guidance cluster. The response SHALL identify its prerequisite and generic fallback. Existing `compact`, `full`, and `diagnostics` behavior SHALL remain available.

#### Scenario: Skill-aware client requests current state
- **WHEN** a client calls bootstrap with profile `session`
- **THEN** current policy and active capability state match the compact projection for that caller
- **AND** static semantic authoring recipes and unselected pack instructions are omitted
- **AND** the result says that installed operating rules are required and compact supplies the portable contract

#### Scenario: Stale skill receives portable compact contract
- **WHEN** a client omits `skill_contract` or presents a digest that differs from the server's canonical skill contract
- **THEN** the same call returns `profile="compact"` and a closed unavailable reason
- **AND** compact resolution and due-state delivery execute once

#### Scenario: Reduced adapter and overridden policy
- **WHEN** the active adapter omits a tool or the user lowers a delegation ceiling
- **THEN** the session profile preserves the lowered ceiling and active adapter fingerprint
- **AND** it does not advertise an unavailable operation

#### Scenario: Live workflow and due state
- **WHEN** scoped workflow configuration, vocabulary extensions, selected packs or audience-filtered due state exist
- **THEN** session bootstrap preserves the corresponding compact state and resolution routes
- **AND** it does not substitute static skill defaults or silently treat missing state as empty

## MODIFIED Requirements

### Requirement: Bootstrap Presents Simple Front-Door Actions

Portable generic bootstrap profiles SHALL present the primary user/agent actions as save, adopt/import, ask, prove, review, update, and connect. For each action they SHALL name the preferred tool or composition of tools, the internal typed operation(s) that enforce governance, and any selected-pack routing guidance. They SHALL keep advanced tools visible but secondary. A valid `session` profile MAY omit this static action teaching and the unselected pack catalogue after accepting the matching installed skill contract; it SHALL retain selected pack state and its selection rule.

#### Scenario: Session profile relies on the attested installed skill
- **WHEN** a client presents the matching installed skill contract with `profile="session"`
- **THEN** the response omits static front-door actions and unselected packs
- **AND** it retains selected pack state and the governance-bound selection rule

#### Scenario: Bootstrap exposes available and selected packs
- **WHEN** an agent reads a portable generic bootstrap response
- **THEN** it can list available built-in packs with beginner descriptions
- **AND** it can list selected packs and their agent instructions
- **AND** a missing selection falls back to a default personal-records pack

#### Scenario: Agent can route a proof request
- **WHEN** an agent reads a portable generic bootstrap response and the user asks "prove this" or "save this for my warranty case"
- **THEN** the agent can identify the evidence/proof workflow
- **AND** it can distinguish that workflow from ordinary source capture
- **AND** selected pack guidance can refine the route without exposing internal ontology to the user

#### Scenario: Agent can route an existing-vault request
- **WHEN** an agent reads a portable generic bootstrap response and the user asks to import or adopt an old vault
- **THEN** the agent can identify the scan-first adoption workflow
- **AND** it knows existing files are read-only by default
