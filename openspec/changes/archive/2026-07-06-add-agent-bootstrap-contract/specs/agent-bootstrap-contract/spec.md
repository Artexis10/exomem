## ADDED Requirements

### Requirement: Agent Bootstrap Contract
The system SHALL expose a read-only `bootstrap` operation that returns a
versioned operating contract for agents using Exomem without a native skill.
The contract MUST be deterministic, structured JSON and MUST NOT inspect or
summarize private vault content.

#### Scenario: Compact bootstrap returns the operating contract
- **WHEN** `bootstrap` is called with default arguments
- **THEN** the response includes `contract_version`, `server`, `workflow`,
  `tool_defaults`, `performance_profiles`, `search_guidance`, and `common_tools`
- **AND** the response identifies the current compute policy
- **AND** the response does not include note bodies, excerpts, paths from the
  user's vault contents, or private project names

#### Scenario: Invalid bootstrap profile is rejected
- **WHEN** `bootstrap(profile="invalid")` is called
- **THEN** the operation fails with a validation error naming the accepted profiles

### Requirement: Bootstrap Profiles
The system SHALL support `compact`, `full`, and `diagnostics` bootstrap profiles.
`compact` SHALL be the default. `full` SHALL include concrete workflow examples.
`diagnostics` SHALL include performance interpretation guidance for timing and
compute-mode discussions.

#### Scenario: Diagnostics profile includes performance guidance
- **WHEN** `bootstrap(profile="diagnostics")` is called
- **THEN** the response includes guidance for normal lookup, reasoning lookup, and
  diagnostics lookup
- **AND** the guidance distinguishes compute mode from retrieval knobs such as
  `rerank`, `pack`, and `include_timings`

### Requirement: Generic Client Workflow Guidance
The bootstrap contract SHALL tell generic agents to search before answering project
or durable-knowledge questions, treat misses as scoped misses, prefer compiled
notes for conclusions, use raw sources/evidence for provenance, and save durable
conclusions as compiled notes.

#### Scenario: Bootstrap teaches the core workflow
- **WHEN** an agent reads the bootstrap response
- **THEN** it can identify the recommended loop from initial lookup through
  optional `get`/`pack`, reasoning, and `note`/`edit`/`replace`
- **AND** it can identify the normal, reasoning, and diagnostics `find` defaults
