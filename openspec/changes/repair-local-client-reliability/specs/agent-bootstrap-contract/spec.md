## ADDED Requirements

### Requirement: Executable Reviewed-None Bootstrap Guidance

The bootstrap contract SHALL describe the reviewed-none creation handshake using the exact
canonical value `reviewed_none` and the exact public parameter names. It MUST tell callers
to validate first, use the returned `relation_review_hash`, supply an explicit bounded
reason, and commit the unchanged draft. It MUST NOT refer to a response field that is not
present.

#### Scenario: Generic agent can commit a zero-candidate draft

- **WHEN** an agent reads bootstrap guidance and validation reports that reviewed-none is
  required
- **THEN** the guidance supplies `relation_disposition="reviewed_none"`
- **AND** it tells the agent to echo the returned `relation_review_hash`
- **AND** it requires an explicit `relation_review_reason`
- **AND** no undocumented guess is needed to form the commit call

