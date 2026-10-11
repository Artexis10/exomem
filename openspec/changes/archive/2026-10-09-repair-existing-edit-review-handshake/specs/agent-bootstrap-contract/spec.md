## ADDED Requirements

### Requirement: Full Bootstrap Teaches Existing Edit Review Recovery

The full bootstrap profile SHALL document the two-call existing-edit relation-review round-trip with copy-pasteable validation and commit examples. The commit example SHALL reuse the returned `transition_token` and `relation_review_hash` and include `relation_disposition="reviewed_none"` plus a bounded reason.

#### Scenario: Generic client bootstraps before editing
- **WHEN** a generic MCP client requests `bootstrap(profile="full")`
- **THEN** it receives an unambiguous validation call and exact commit call for refreshing a stale relation disposition

### Requirement: Full Bootstrap Teaches Canonical Typed Relations

The full bootstrap profile SHALL include a concrete accepted note-level relation bullet under `## Relations` and SHALL distinguish it from unsupported Dataview inline-field syntax.

#### Scenario: Client chooses the typed-relation remedy
- **WHEN** a generic client follows the full bootstrap example
- **THEN** the authored relation is presented in the parser-accepted `- supports [[Target]]` form
