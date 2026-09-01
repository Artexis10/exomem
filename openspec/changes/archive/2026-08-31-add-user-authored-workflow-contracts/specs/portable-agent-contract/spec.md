## ADDED Requirements

### Requirement: Workflow contract projections share one resolved identity

The code-owned workflow invariant kernel, built-in fallback, schema version, resolver semantics, and fixed-template renderer SHALL have one normalized runtime source and content digest. Bootstrap, command descriptions, generated capability documentation, plugin copies, filesystem installs, wheel/sdist contents, and generic skill archives SHALL be checked against that identity. The generic scaffold SHALL remain the repository's hand-authored, deliberately generic skill source rather than a generated runtime projection; parity tests SHALL pin its declared workflow contract version, operation names, and invariant statements to the machine identity. Vault-authored contract instances SHALL remain personalized data and SHALL NOT enter public package generation or generic fixtures.

#### Scenario: Generic and personalized projections agree
- **WHEN** the same canonical workflow contract is resolved through MCP, REST, CLI, and an installed skill-enabled client
- **THEN** each surface reports the same contract identity, fingerprint, machine decision, provenance, and invariant boundary while presentation may differ only by documented profile bounds

#### Scenario: Public package remains generic
- **WHEN** public artifacts are built on a machine whose vault contains personal workflow contracts
- **THEN** the artifacts contain only the generic invariant/resolver contract and no personal tool names, projects, contract titles, paths, or identifiers

#### Scenario: Human renderer drift fails deterministically
- **WHEN** a renderer or skill edit changes the meaning of a workflow field without changing the normalized contract identity
- **THEN** parity validation fails and names the drifted projection
