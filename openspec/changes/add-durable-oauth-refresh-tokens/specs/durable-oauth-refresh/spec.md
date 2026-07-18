## ADDED Requirements

### Requirement: Offline authorization issues bounded access and durable refresh credentials
When the granted OAuth scopes include `offline_access`, Exomem SHALL issue an opaque access token valid for 3600 seconds and an opaque refresh token valid until its family is explicitly revoked or its signing generation is replaced. The credentials MUST be bound to the authenticated identity, OAuth client, issuer, audience, generation, and granted scopes, and Exomem MUST NOT retain the upstream GitHub access or refresh credential after identity bootstrap.

#### Scenario: Client completes offline authorization
- **WHEN** an authorized client exchanges a valid authorization code whose granted scopes include `offline_access`
- **THEN** the token response contains `token_type=Bearer`, `expires_in=3600`, the granted scopes, an opaque access token, and an opaque refresh token
- **AND** subsequent access validation uses only the Exomem authority and does not contact GitHub

#### Scenario: Client authorizes without offline access
- **WHEN** an authorized client exchanges a valid authorization code without the `offline_access` scope
- **THEN** Exomem issues the existing non-expiring `exo_s1` session without a refresh token
- **AND** the client is not silently migrated to an expiring credential it cannot refresh

### Requirement: Refresh rotation is durable and cannot expand authority
Exomem SHALL accept an active refresh token only for its bound client and SHALL rotate it to a deterministic next refresh token while issuing a new 3600-second access token. A refresh request MAY retain or narrow the original scopes and MUST NOT add a scope outside the refresh family's grant. Family and redemption state MUST use the authoritative encrypted session store so rotation survives service restart and replica failover, and raw bearer tokens MUST NOT be persisted.

#### Scenario: Active token rotates successfully
- **WHEN** the bound client redeems an active refresh token with its original scopes or a subset
- **THEN** Exomem returns a new one-hour access token and the next refresh token in the family
- **AND** the consumed refresh sequence cannot begin another independent rotation

#### Scenario: Refresh attempts to expand scopes
- **WHEN** a client requests a scope that was not granted to the refresh family
- **THEN** Exomem rejects the request with `invalid_scope`
- **AND** the refresh token remains usable with its original scopes

#### Scenario: Service or replica changes between rotations
- **WHEN** one process issues a refresh token and another process handles its redemption after restart or failover
- **THEN** the second process observes the same family and redemption state and completes exactly the same rotation contract

### Requirement: Concurrent refresh retries are briefly idempotent
The first redemption of a refresh sequence SHALL create one atomic shared redemption receipt. Every redemption of that same token within 30 seconds of the first SHALL return the same next refresh token and MUST NOT be classified as theft; each response MAY contain a distinct valid access token.

#### Scenario: Two ChatGPT workers refresh concurrently
- **WHEN** two valid requests for the same refresh token race across one or more Exomem processes
- **THEN** exactly one atomic redemption receipt is created
- **AND** both successful responses contain the same next refresh token

#### Scenario: First refresh response is lost
- **WHEN** the bound client retries the consumed refresh token within 30 seconds because it did not receive the first response
- **THEN** Exomem reconstructs the same next refresh token from durable state and succeeds without GitHub reauthorization

### Requirement: Late refresh-token reuse revokes the family
A consumed refresh token presented more than 30 seconds after its first redemption SHALL be treated as refresh-token reuse. Exomem MUST revoke the complete family, return `invalid_grant`, and reject every refresh token and access token bound to that family thereafter.

#### Scenario: Rotated token is replayed after the retry window
- **WHEN** a previously consumed refresh token is presented more than 30 seconds after its redemption
- **THEN** Exomem returns `invalid_grant` and durably revokes the refresh family
- **AND** the current refresh token and all family-bound access tokens no longer validate

#### Scenario: Replay races the current token
- **WHEN** late reuse of an old token races a legitimate redemption of the current family token
- **THEN** family state is rechecked at the security boundary and the family ends revoked
- **AND** no access token remains usable after revocation is authoritative

### Requirement: Revocation and signing rotation fail closed
Revoking a refresh token SHALL revoke its complete family, while revoking a v2 access token SHALL revoke that access token. Replacing the signing generation SHALL invalidate every legacy session, v2 access token, and refresh family issued under the old generation. Revocation endpoints MUST NOT disclose whether an unknown token existed.

#### Scenario: Client disconnects an offline grant
- **WHEN** the client revokes any refresh token from an active family
- **THEN** every refresh token in that family and every family-bound access token is rejected thereafter

#### Scenario: Operator rotates the signing generation
- **WHEN** the operator replaces the active signing generation
- **THEN** all credentials bound to the prior generation fail validation without a per-record migration

#### Scenario: Unknown token is revoked
- **WHEN** a caller submits an unknown or malformed token to the revocation endpoint
- **THEN** Exomem returns the standard non-disclosing revocation success response

### Requirement: Legacy sessions remain compatible
Deployment of durable refresh support MUST preserve the parsing, validation, listing, and explicit revocation behavior of existing `exo_s1` records. No deployment or connector recreation for the new flow SHALL automatically revoke those sessions.

#### Scenario: Existing Codex or Claude session crosses the deployment
- **WHEN** an active `exo_s1` session issued by version 0.24.1 is presented after version 0.24.2 starts
- **THEN** Exomem validates it under the existing issuer, audience, digest, status, and generation rules
- **AND** the user is not forced to reconnect

### Requirement: Discovery advertises the implemented offline contract
The canonical authorization-server metadata and compatibility OIDC alias SHALL advertise `authorization_code` and `refresh_token` grants plus the supported `offline_access`, `exomem:read`, and `exomem:write` scopes. Protected-resource metadata SHALL advertise the resource scopes, and Exomem MUST NOT advertise OIDC userinfo or ID-token support it does not implement.

#### Scenario: ChatGPT discovers OAuth settings
- **WHEN** ChatGPT reads Exomem's authorization-server, protected-resource, or compatibility discovery document
- **THEN** the document identifies the Exomem authorization, token, registration, resource, and supported-scope contract consistently
- **AND** ChatGPT can select `offline_access` as a base scope before creating the connector

#### Scenario: Connector refreshes without GitHub
- **WHEN** an installed connector uses the advertised refresh grant after its one-hour access token expires
- **THEN** Exomem rotates the refresh token and issues a new access token without redirecting the user to GitHub
