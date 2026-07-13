## ADDED Requirements

### Requirement: GitHub is a one-time identity proof
The system SHALL use the upstream GitHub OAuth token only inside the IdP callback for identity proof and exact-token disposal. It MUST match both the configured normalized login and configured `EXOMEM_GITHUB_USER_ID`, MUST disable raw-token verification caches, and MUST persist only a minimal verified identity proof in the downstream client code.

#### Scenario: Configured GitHub account authorizes successfully
- **WHEN** the callback token resolves to the configured login and configured immutable user ID
- **THEN** the callback stores a token-free identity proof that can issue an Exomem-owned MCP session

#### Scenario: Wrong or unverifiable GitHub account is rejected
- **WHEN** GitHub rejects the token, the login differs, or the immutable user ID is absent or differs from `EXOMEM_GITHUB_USER_ID`
- **THEN** the system issues no Exomem session and returns an OAuth authorization failure

#### Scenario: Identity proof is not cached with the raw token
- **WHEN** the callback finishes verification on any success or failure path
- **THEN** neither FastMCP nor Exomem retains the raw GitHub bearer in a verifier cache

### Requirement: Durable sessions require an explicit root of trust
The system MUST require non-empty `EXOMEM_JWT_SIGNING_KEY` and `EXOMEM_GITHUB_USER_ID` values whenever HTTP OAuth is enabled. It SHALL derive session HMAC and encryption keys from the signing value with distinct purpose salts and MUST NOT fall back to the GitHub client secret.

#### Scenario: HTTP OAuth starts with explicit trust anchors
- **WHEN** all OAuth settings including `EXOMEM_JWT_SIGNING_KEY` and `EXOMEM_GITHUB_USER_ID` are present
- **THEN** the system derives purpose-separated session keys and starts the protected HTTP server

#### Scenario: Trust anchor is missing
- **WHEN** HTTP OAuth is enabled without `EXOMEM_JWT_SIGNING_KEY` or `EXOMEM_GITHUB_USER_ID`
- **THEN** startup fails with an actionable configuration error before accepting requests

### Requirement: Remote setup provisions durable auth settings
The remote setup flow SHALL generate and preserve `EXOMEM_JWT_SIGNING_KEY`, resolve and persist `EXOMEM_GITHUB_USER_ID` for the configured login, and configure matching non-empty coordinator and Exomem OAuth-storage bearer credentials for HA. Remote doctor SHALL validate the settings offline and SHALL verify the coordinator credential when run with `--probe`.

#### Scenario: New remote setup provisions trust anchors
- **WHEN** a user completes a new authenticated remote setup
- **THEN** the resulting environment contains the signing key, immutable GitHub user ID, and any required matching HA storage credentials

#### Scenario: Existing signing key is upgraded safely
- **WHEN** remote setup runs against an existing installation with `EXOMEM_JWT_SIGNING_KEY`
- **THEN** it preserves that value instead of rotating all existing sessions implicitly

#### Scenario: Doctor finds incomplete HA authentication
- **WHEN** shared OAuth storage is configured with a missing or rejected coordinator token
- **THEN** offline doctor reports the missing setting or probed doctor reports the rejected credential with remediation

### Requirement: Exomem issues a durable opaque session
The system SHALL issue a versioned opaque bearer containing at least 256 bits of secret entropy. It MUST omit `expires_in` and `refresh_token`, MUST store only an HMAC proof rather than the raw bearer, and MUST persist an encrypted session record containing the MCP client, MCP scopes, issuer, protected-resource audience, issuance identity, issue time, status, and auth generation.

#### Scenario: Successful token response has no time-based expiry
- **WHEN** authorization-code exchange succeeds
- **THEN** the token response contains an opaque bearer and no access-token expiry or refresh token

#### Scenario: Stored state cannot reveal the bearer
- **WHEN** an operator or test inspects decrypted session fields
- **THEN** the raw bearer secret is absent and only its keyed HMAC proof is present

#### Scenario: GitHub scopes do not become MCP scopes
- **WHEN** GitHub's token response includes upstream scopes that differ from the downstream authorization code's scopes
- **THEN** the Exomem session contains and returns only the authorized MCP scopes

### Requirement: Ordinary requests are independent of GitHub
The system SHALL validate ordinary MCP bearer requests only against the Exomem session authority. It MUST NOT call GitHub, load a retained upstream GitHub token, or depend on a FastMCP JTI-to-upstream-token mapping after session issuance.

#### Scenario: Repeated requests make zero GitHub calls
- **WHEN** an active Exomem session performs any number of MCP requests
- **THEN** every request validates locally and the GitHub verifier and GitHub API receive zero calls

#### Scenario: GitHub later revokes or evicts the authorization token
- **WHEN** GitHub invalidates the temporary token after the Exomem session was issued
- **THEN** the Exomem session remains valid until Exomem revokes it or the signing key rotates

#### Scenario: More than ten clients authorize
- **WHEN** more than ten downstream MCP clients authorize through the same GitHub user, application, and scopes
- **THEN** every previously issued active Exomem session remains valid

### Requirement: Session validation is cryptographically and contextually bound
The system MUST verify the bearer HMAC using a key derived from `EXOMEM_JWT_SIGNING_KEY` with a distinct purpose salt and constant-time comparison. It SHALL also require an active record whose schema, generation, issuer, audience, stored issuing-client context, scopes, and issuance identity are valid for the current Exomem deployment.

#### Scenario: Unknown or modified token is rejected
- **WHEN** a bearer is malformed, unknown, or differs from the issued secret
- **THEN** the system returns invalid token without revealing which proof check failed

#### Scenario: Token record belongs to another deployment context
- **WHEN** a record's issuer or protected-resource audience differs from the current deployment
- **THEN** the system rejects the bearer as invalid

#### Scenario: Signing key rotates
- **WHEN** `EXOMEM_JWT_SIGNING_KEY` changes
- **THEN** the authority selects a fresh fingerprinted collection namespace and rejects prior sessions as unknown without decrypting old ciphertext

### Requirement: HA session state is authoritative and revocation-safe
In HA mode, the system SHALL read encrypted session and generation state from a bearer-authenticated remote coordinator as the sole authority, without read cache or local fallback. It MUST require a non-empty `EXOMEM_OAUTH_STORAGE_TOKEN`, use key-fingerprinted collection namespaces and permanent revoked tombstones, and MUST NOT resurrect a session from replica-local state.

#### Scenario: HA storage credential is missing
- **WHEN** a shared OAuth storage URL is configured without `EXOMEM_OAUTH_STORAGE_TOKEN`
- **THEN** startup fails before exposing the protected HTTP server

#### Scenario: Session crosses replicas
- **WHEN** replica A issues a session and replica B receives its bearer
- **THEN** replica B validates the session from the shared authoritative state

#### Scenario: Revocation propagates immediately
- **WHEN** replica A revokes a session that replica B previously accepted
- **THEN** replica B rejects the next request without waiting for cache expiry

#### Scenario: Stale local copy exists after revocation
- **WHEN** the authoritative record is a revoked tombstone and a replica has an older active local copy
- **THEN** the replica rejects the session and never restores the active copy

### Requirement: Auth-store outages are not authentication failures
The system MUST distinguish authoritative session-store unavailability from an invalid bearer. Invalid, unknown, or revoked sessions SHALL return 401 with the normal invalid-token challenge, while coordinator/network failures or decryption corruption in the current key namespace SHALL produce 503 without `WWW-Authenticate: invalid_token` or any OAuth authorization challenge.

#### Scenario: Coordinator is unavailable for an otherwise valid session
- **WHEN** session validation cannot reach the authoritative coordinator
- **THEN** the MCP endpoint returns 503 without an invalid-token challenge

#### Scenario: Revoked session is available in the authority
- **WHEN** the authoritative store successfully returns a revoked tombstone
- **THEN** the MCP endpoint returns invalid token

### Requirement: Operators can inspect and revoke Exomem sessions
The system SHALL provide authenticated operator commands to list non-secret session metadata, revoke one session by session ID, and revoke all sessions by replacing the shared auth generation. The system MUST NOT expose raw bearer material or privileged session enumeration through MCP knowledge tools.

#### Scenario: Operator lists sessions
- **WHEN** an authorized operator runs `exomem auth sessions`
- **THEN** the command returns session IDs, client metadata, login, issue time, and status without bearer secrets

#### Scenario: Operator revokes one session
- **WHEN** an authorized operator runs `exomem auth revoke <session-id>`
- **THEN** the authority overwrites that session with a permanent revoked tombstone and all replicas reject it

#### Scenario: Operator revokes all sessions
- **WHEN** an authorized operator runs `exomem auth revoke --all`
- **THEN** the authority replaces the auth generation and every session from the prior generation is rejected

#### Scenario: Revoke-all races with issuance
- **WHEN** global revocation changes the generation while a new session is being committed
- **THEN** the system never returns a session that remains active under the revoked generation

### Requirement: Client-initiated revocation uses the same authority
The system SHALL enable local OAuth token revocation and SHALL map a client-presented active bearer to the same permanent session tombstone used by operator revocation.

#### Scenario: Client revokes its bearer
- **WHEN** an authenticated OAuth client submits its bearer to the revocation endpoint
- **THEN** all replicas reject subsequent use of that session

### Requirement: Temporary GitHub credentials are disposed
After identity proof in the IdP callback, the system MUST NOT retain the GitHub access token in the verifier cache, downstream client code, or session state. Before storing the client code, it SHALL attempt to revoke the exact token with GitHub and SHALL emit a secret-safe operational alert if cleanup fails.

#### Scenario: GitHub cleanup succeeds
- **WHEN** the callback verifies identity and GitHub accepts exact-token deletion
- **THEN** the downstream client code contains only the verified proof and no upstream token

#### Scenario: GitHub cleanup fails after identity proof
- **WHEN** GitHub token deletion fails because GitHub is unavailable
- **THEN** the callback discards the raw token, stores only the verified proof, and emits a secret-safe cleanup alert

#### Scenario: Client abandons or fails downstream exchange
- **WHEN** the client never redeems the downstream code or fails its PKCE check
- **THEN** no GitHub credential remains in Exomem storage and no Exomem bearer is issued

#### Scenario: Callback fails after GitHub issued a token
- **WHEN** identity validation, proof persistence, or redirect handling fails after a GitHub token is available
- **THEN** the callback attempts exact-token cleanup and does not retain the raw credential

### Requirement: Existing OAuth protocol behavior remains compatible
The system SHALL preserve the current connector URL, OAuth discovery, protected-resource metadata, DCR, consent, callback, PKCE, authorization-code, and MCP scope behavior. The implementation MUST prove that supported clients persist and reuse a token response with omitted lifetime fields before production rollout.

#### Scenario: Existing connector performs final migration login
- **WHEN** an existing client presents a legacy FastMCP reference JWT after deployment
- **THEN** the server rejects it once, the client completes the unchanged browser authorization flow, and the connector receives a durable local session without being deleted or re-registered

#### Scenario: Fresh Codex conversations reuse one login
- **WHEN** Codex has stored a newly issued Exomem session and starts multiple fresh conversations or processes
- **THEN** each process reuses that bearer without launching the browser authorization flow

#### Scenario: Supported client mishandles omitted expiry
- **WHEN** a black-box compatibility test shows that a supported client discards or refuses a token without `expires_in`
- **THEN** production rollout is blocked and the implementation returns to design instead of adding an arbitrary far-future expiry

### Requirement: FastMCP integration failures are detected before deployment
The implementation SHALL isolate private FastMCP dependencies in one adapter, pin exactly `fastmcp==3.4.4`, and provide contract tests that fail when required callback, transaction/code-store, authorization-code, token-loading, revocation, or exception behavior changes.

#### Scenario: FastMCP upgrade changes a required private seam
- **WHEN** dependency resolution selects any FastMCP version other than 3.4.4 or the pinned internals no longer satisfy the adapter contract
- **THEN** dependency or contract validation fails before deployment

### Requirement: Migration does not preserve the faulty dependency
The deployment MUST use a coordinated replica cutover and MUST NOT dual-read legacy JTI/upstream-token records during normal operation. Legacy records MAY remain untouched for a bounded rollback window and SHALL be removed only after the new provider is verified.

#### Scenario: Old and new providers would otherwise serve together
- **WHEN** the HA rollout could route requests to both token formats
- **THEN** traffic is quiesced or replica routing is coordinated so only the new provider serves after cutover

#### Scenario: Rollback is required
- **WHEN** the deployment rolls back during the bounded rollback window
- **THEN** legacy records are still available, new session records remain inert, and migrated clients can authorize once into the prior format
