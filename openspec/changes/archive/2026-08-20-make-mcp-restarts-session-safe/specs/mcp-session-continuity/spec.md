## ADDED Requirements

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
