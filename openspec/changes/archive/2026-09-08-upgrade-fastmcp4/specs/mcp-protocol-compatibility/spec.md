## Purpose

Keep MCP command access, authentication and credential protection consistent
across supported transport generations and framework upgrades.

## ADDED Requirements

### Requirement: Legacy and sessionless clients share command semantics

The server SHALL support legacy initialized MCP clients and modern sessionless
MCP clients with the same public command inputs, results and per-request
authorization rules. Transport identifiers and discovery SHALL NOT establish
governance authority. Ordinary writes SHALL retain durable acknowledgement
and read-your-write behavior independently of protocol generation.

#### Scenario: Legacy client reconnects

- **WHEN** a legacy client initializes, lists tools and reconnects to stateless HTTP
- **THEN** it can call the same public tools with valid credentials
- **AND** the optional authenticated GET channel remains compatible

#### Scenario: Modern client calls without initialization

- **WHEN** a modern client discovers the server and calls a public tool
- **THEN** the command uses the same response contract as a legacy call
- **AND** authorization is verified for that request without initialization state

#### Scenario: Declared argument types remain enforced

- **WHEN** a caller supplies a boolean or numeric string for a strict integer argument
- **THEN** the call refuses without executing the command
- **AND** valid JSON values matching the published argument types remain accepted

### Requirement: Upgrades preserve credential custody and durable authentication

Framework upgrades SHALL preserve the existing authorization-session binding
and OAuth-session longevity contracts. Invalid credentials and upstream
verification failures SHALL fail closed without exposing bearer values in
responses or logs. Existing signing configuration and durable OAuth records
SHALL remain usable without upgrade-induced state deletion or forced re-login.

#### Scenario: Invalid carrier on either transport

- **WHEN** a stdio or HTTP tool request carries an invalid or duplicated authorization credential
- **THEN** the request is refused before ordinary validation or command execution
- **AND** raw credentials are absent from SDK logs and refusal output

#### Scenario: Provider transport fails

- **WHEN** the configured OAuth identity provider times out or fails its HTTP request
- **THEN** verification refuses access and logs only bounded non-secret diagnostics

#### Scenario: Existing OAuth state survives a framework upgrade

- **WHEN** the runtime is upgraded with unchanged issuer, signing key and storage configuration
- **THEN** existing valid access tokens and refresh records retain their configured lifecycle
- **AND** revocation and single-user identity restrictions continue to apply
