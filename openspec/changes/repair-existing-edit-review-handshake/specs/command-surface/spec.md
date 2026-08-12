## ADDED Requirements

### Requirement: Existing Edit Remediation Matches Its Public Schema

An `edit_memory` relation-disposition remediation SHALL reference only parameters exposed by the selected edit kind and SHALL identify `relation_review_hash` as the exact validation response value to send back. Creation-only draft parameters MUST NOT appear in existing-edit remediation.

#### Scenario: Edit remediation is checked against discovery
- **WHEN** a blocking relation-disposition finding is rendered for `edit_memory`
- **THEN** every named call parameter exists in that kind's public discovery schema
- **AND** the text describes the validate-then-commit call sequence

### Requirement: Edit Memory Documents Typed Relation Authoring

The `edit_memory` description SHALL include a copy-pasteable note-level typed-relation example using a bullet under `## Relations`, and SHALL state that Dataview inline-field syntax is not parsed as a typed relation.

#### Scenario: Generic client reads edit discovery
- **WHEN** a generic MCP client inspects `edit_memory`
- **THEN** it can author `- supports [[Knowledge Base/Notes/Research/example-target]]` under `## Relations`
- **AND** it is not led to use `supports:: [[...]]`

### Requirement: Semantic Errors Do Not Claim Arguments Are Missing

The edit adapter SHALL append a `(missing: [...])` suffix only when argument validation identified actual missing or invalid fields. Semantic governance and transition errors MUST preserve their code and reason without the suffix.

#### Scenario: Transition token mismatches
- **WHEN** an edit fails with `LIFECYCLE_TRANSITION_MISMATCH`
- **THEN** the error does not contain a `missing` suffix
