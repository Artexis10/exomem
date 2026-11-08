## MODIFIED Requirements

### Requirement: Executable Reviewed-None Bootstrap Guidance

The bootstrap contract SHALL describe reviewed-none using the exact canonical value
`reviewed_none` and the exact public parameter names. It MUST tell callers to send
`relation_disposition="reviewed_none"` and an explicit bounded reason when no truthful
relation applies, that a validated draft also sends its returned `relation_review_hash`,
and that an edit refused for relation review is re-sent with the same fields, a validated
edit also sending its `transition_token` and `relation_review_hash`. It MUST NOT refer to a
response field that is not present.

#### Scenario: Generic agent can commit a zero-candidate draft

- **WHEN** an agent reads bootstrap guidance and a write is refused because reviewed-none
  is required
- **THEN** the guidance supplies `relation_disposition="reviewed_none"`
- **AND** it requires an explicit `relation_review_reason`
- **AND** it tells the agent that a validated draft also echoes the returned `relation_review_hash`
- **AND** no undocumented guess is needed to form the commit call
