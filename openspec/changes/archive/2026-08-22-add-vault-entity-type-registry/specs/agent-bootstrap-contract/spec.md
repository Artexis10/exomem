## MODIFIED Requirements

### Requirement: Agent Bootstrap Contract
The system SHALL expose a read-only `bootstrap` operation that returns a versioned operating contract for agents using Exomem without a native skill. The contract MUST be deterministic, structured JSON and MUST NOT inspect or summarize private vault content. Its entity-capture section SHALL list the active vault-aware entity type IDs and explain that unknown recurring types are registered only through the governed save leaf with a rationale.

#### Scenario: Compact bootstrap returns the operating contract
- **WHEN** `bootstrap` is called with default arguments
- **THEN** the response includes `contract_version`, `server`, `workflow`, `tool_defaults`, `performance_profiles`, `search_guidance`, and `common_tools`
- **AND** the response identifies the current compute policy
- **AND** the response does not include note bodies, excerpts, paths from the user's vault contents, or private project names

#### Scenario: Entity capture exposes active extension types
- **WHEN** a vault defines active entity type `place` and calls bootstrap
- **THEN** `entity_capture.types` contains `place` and every core type
- **AND** its guidance routes unknown recurring types to `save-entity-types` with `why`

#### Scenario: Invalid bootstrap profile is rejected
- **WHEN** `bootstrap(profile="invalid")` is called
- **THEN** the operation fails with a validation error naming the accepted profiles
