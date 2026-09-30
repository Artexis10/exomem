## ADDED Requirements

### Requirement: Existing clients survive managed worker upgrades
The system SHALL preserve an already connected authenticated MCP client's usability through a managed worker upgrade completed within the configured request budget. The client SHALL NOT need a new conversation, manual refresh, new client context or new OAuth grant.

#### Scenario: One client before during and after upgrade
- **WHEN** the same entered client context and bearer credential call before, during and after a managed worker upgrade
- **THEN** all admitted calls return their normal results without reinitializing that context
- **AND** authentication remains required on every dispatched request

#### Scenario: Active POST response stream
- **WHEN** a tool's POST response is streaming as an upgrade begins
- **THEN** its full response finishes before the serving worker stops
- **AND** that POST is never replayed to the replacement

#### Scenario: Standalone authenticated GET stream
- **WHEN** the client has an authenticated standalone GET/SSE stream open during replacement
- **THEN** its outer stream remains open through the handoff
- **AND** the replacement authenticates the original credential before further upstream content is forwarded

#### Scenario: Invalid credential
- **WHEN** a client supplies an invalid bearer before or after a managed upgrade
- **THEN** the worker denies access using the existing authentication contract
