# mcp-session-continuity Specification

## Purpose
Preserve authenticated MCP access across process replacement through session-independent HTTP requests, durable OAuth authority, standard discovery metadata and bounded managed worker upgrades.

## Requirements

### Requirement: Remote MCP requests are process-session independent
The system SHALL run remote Streamable HTTP without requiring a process-local MCP transport session to survive between requests.

#### Scenario: Consecutive remote calls
- **WHEN** an authenticated client makes multiple MCP calls to the remote endpoint
- **THEN** each call is handled without depending on a previously allocated process-local transport session

#### Scenario: Client opens the optional GET stream
- **WHEN** an authenticated client opens the Streamable HTTP GET/SSE channel
- **THEN** the server accepts the connection without allocating or requiring a process-local session ID
- **AND** the GET endpoint remains protected by the same OAuth middleware as POST

#### Scenario: Service restart or replica failover
- **WHEN** the Exomem process serving the endpoint changes between authenticated MCP calls
- **THEN** the next call can establish transport handling without a stale `Mcp-Session-Id` lookup
- **AND** OAuth authentication remains required

### Requirement: Local stdio behavior remains unchanged
The system SHALL preserve the existing stdio transport behavior for local MCP clients.

#### Scenario: Stdio server start
- **WHEN** Exomem starts with the stdio transport
- **THEN** it does not apply HTTP stateless transport configuration

### Requirement: Codex discovery completes after OAuth exchange
The system SHALL expose OAuth authorization-server metadata at the OIDC well-known alias used by compatible MCP clients.

#### Scenario: Client probes OIDC discovery after token exchange
- **WHEN** a client requests `/.well-known/openid-configuration`
- **THEN** the server returns the issuer, authorization, token, registration, grant, client-authentication, and PKCE metadata for Exomem's OAuth server
- **AND** the response does not claim that Exomem issues OIDC ID tokens

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
