## ADDED Requirements

### Requirement: Executable marketplace reviewer fixture

The system SHALL maintain a versioned marketplace reviewer fixture whose complete pre-seeded payload is accepted by the current governed product write contract. Fixture validation MUST execute each exact checked note through `remember` validation and commit, including the canonical reviewed-none handshake only when validation requires it, and MUST read the committed note back without using a fixture exemption or privileged storage path.

#### Scenario: Checked fixture conforms to the real write contract

- **WHEN** release validation runs against the checked marketplace reviewer fixture in a newly initialized vault
- **THEN** every pre-seeded note is validated, committed, and read back through the real product leaves
- **AND** the resulting title and Markdown body match the exact checked payload

#### Scenario: Fixture contains a semantically invalid compiled note

- **WHEN** any pre-seeded note lacks a valid semantic unit or has another non-review write blocker
- **THEN** executable fixture validation fails before a candidate or directory packet is accepted
- **AND** validation does not add an exemption, reclassify the note as a raw source, or weaken the shared write contract

#### Scenario: Disconnected fixture note needs reviewed-none

- **WHEN** validation reports that an otherwise valid exact fixture note requires a relation-review disposition
- **THEN** the seeder commits the unchanged returned draft using `reviewed_none`, the returned relation-review hash, and a bounded generic reason
- **AND** it does not guess or fabricate a relation

#### Scenario: Fixture payload changes

- **WHEN** the content or seed order of the canonical reviewer payload changes
- **THEN** its fixture version and payload digest change together
- **AND** stale review cases, directory packets, credentials, and evidence cannot bind the new fixture

### Requirement: Pre-seal reviewer fixture preparation

The reviewer bootstrap SHALL use the redeemed setup session and OAuth token to wait for the dedicated reviewer cell, seed and verify the exact checked fixture through authenticated Hosted MCP, and only then issue provider reviewer credentials. Issuing either provider credential MUST remain the sealing event that revokes temporary setup access.

#### Scenario: Dedicated reviewer cell becomes ready

- **WHEN** invite redemption and token exchange succeed and owner status reaches `ready` with `CELL_READY` inside the bounded staged-release window
- **THEN** the bootstrap seeds and verifies the exact fixture through `/api/exomem/mcp/v1`
- **AND** it issues no Claude or OpenAI reviewer credential until all pre-seeded notes pass exact readback

#### Scenario: Readiness or fixture preparation fails

- **WHEN** token exchange fails, readiness exceeds its bound, a fixture call fails, or exact readback differs
- **THEN** the bootstrap fails closed without calling the reviewer-credential issuance endpoint
- **AND** it does not print bootstrap completion or represent the reviewer account as seeded

#### Scenario: Fixture preparation succeeds

- **WHEN** all checked notes have been committed and read back exactly
- **THEN** the bootstrap records only the fixture version, payload digest, note count, and verification outcome in its protected operator state
- **AND** it may create the provider siblings and issue their fixture-bound reviewer credentials

#### Scenario: Bootstrap transports protected material

- **WHEN** setup OAuth or MCP calls carry tokens, cookies, fixture bodies, or content-bearing responses
- **THEN** bearer material appears only in the authorization header and setup cookies only in the cookie header
- **AND** the bootstrap does not print or persist MCP request/response bodies outside the existing protected secret state boundary

#### Scenario: Native provider acceptance follows automated preparation

- **WHEN** the bootstrap reports a seeded reviewer and issues both provider credentials
- **THEN** real ChatGPT and Claude clients still MUST complete native install, authorization, discovery, recall, citation, capture, and fresh-chat recall
- **AND** only their separately signed evidence can authorize promotion
