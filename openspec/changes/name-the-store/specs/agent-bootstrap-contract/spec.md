## MODIFIED Requirements

### Requirement: Agent Bootstrap Contract
The system SHALL expose a read-only `bootstrap` operation that returns a versioned operating contract for agents using Exomem without a native skill. The contract MUST be deterministic, structured JSON and MUST NOT inspect or summarize private vault content. Its entity-capture section SHALL list the active vault-aware entity type IDs and explain that unknown recurring types are registered only through the governed save leaf with a rationale. The complete compact reference payload (`section=all`) and the `full` and `diagnostics` profiles SHALL carry `store`, an object whose `kind` is `personal`, `hosted` or `cloud` and whose `name` is the label a person uses for that deployment (`personal Exomem`, `Exomem Hosted` or `Exomem Cloud`), because two connectors can present the same display name. The `diagnostics_reading` section serves it.

#### Scenario: Compact bootstrap returns the operating contract
- **WHEN** `bootstrap` is called with default arguments
- **THEN** the response includes `contract_version`, `server`, `workflow`, `tool_defaults`, `performance_profiles`, `search_guidance`, and `common_tools`
- **AND** the response identifies the current compute policy
- **AND** the response does not include note bodies, excerpts, paths from the user's vault contents, or private project names

#### Scenario: Bootstrap names the store it reaches
- **WHEN** `bootstrap(section="all")` is called on a personal deployment, and again on an Exomem Cloud cell
- **THEN** `store.kind` is `personal` with `store.name` "personal Exomem" in the first response, and `cloud` with "Exomem Cloud" in the second
- **AND** the response still carries no vault content

#### Scenario: Entity capture exposes active extension types
- **WHEN** a vault defines active entity type `place` and calls bootstrap
- **THEN** `entity_capture.types` contains `place` and every core type
- **AND** its guidance routes unknown recurring types to `save-entity-types` with `why`

#### Scenario: Invalid bootstrap profile is rejected
- **WHEN** `bootstrap(profile="invalid")` is called
- **THEN** the operation fails with a validation error naming the accepted profiles

### Requirement: Skill-aware session bootstrap

The system SHALL accept the opt-in bootstrap profile `session` for clients that have already loaded the installed Exomem operating rules and present its metadata `skill_contract`. The server SHALL derive that contract as SHA-256 over canonical JSON mapping logical source paths to LF-normalized text for the core skill, `references/*.md`, and standalone workflow `SKILL.md` files discovered from the canonical manifest, with only `metadata.skill_contract` removed before hashing. It SHALL resolve the handshake before constructing compact state: an absent or mismatched contract SHALL return the actual `compact` profile in the same call with a closed unavailable reason. A valid session request SHALL return live policy and capability state without repeating the generic static authoring contract. It SHALL preserve the compact contract's filtered server identity, the `store` it reaches, active capabilities, engagement and delegation envelope, governance, workflow contracts, relation vocabulary currency, active entity registry, source taxonomy, selected knowledge-pack guidance and selection rule, and due state when present. It SHALL retain the compact due-state and family-disposition post-write guidance cluster. The response SHALL identify its prerequisite and generic fallback. Existing `compact`, `full`, and `diagnostics` behavior SHALL remain available.

#### Scenario: Skill-aware client requests current state
- **WHEN** a client calls bootstrap with profile `session`
- **THEN** current policy and active capability state match the compact projection for that caller
- **AND** static semantic authoring recipes and unselected pack instructions are omitted
- **AND** the result says that installed operating rules are required and compact supplies the portable contract

#### Scenario: Session profile names the store
- **WHEN** a client calls bootstrap with profile `session` and a current `skill_contract`
- **THEN** `store.kind` is `personal` on a personal deployment and `cloud` on an Exomem Cloud cell, each with its `name`

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
