## Context

FastMCP's `OAuthProxy` currently treats its downstream JWT as a reference to an upstream GitHub token. Authorization-code exchange stores one GitHub token set per MCP client, and `load_access_token()` resolves the JWT's JTI and revalidates the GitHub token on ordinary requests. Exomem's shared signing key, encrypted JTI mapping, and upstream-token record are all healthy in the reproduced incident; GitHub rejects the retained upstream credential.

The store contains 44 upstream-token records. GitHub permits only ten OAuth tokens per user/application/scope combination and revokes the oldest when another is created. A reconnect therefore repairs one MCP client by invalidating another. The approved security boundary is that GitHub proves the configured user's identity once and Exomem owns session continuity thereafter.

Exomem runs in both single-node and active/passive HA configurations. HA already has an encrypted remote OAuth state store plus a local read-through mirror. That mirror is appropriate for recoverable OAuth bookkeeping, but it is unsafe as a revocation authority because a remote miss can repopulate stale local data.

## Goals / Non-Goals

**Goals:**

- Preserve the standard MCP OAuth browser flow while using GitHub only as an authorization-time identity proof.
- Give Codex, Claude, and other MCP clients an Exomem session that does not expire with inactivity, conversation boundaries, GitHub token eviction, or ordinary service restarts.
- Make explicit Exomem revocation and signing-key rotation the only session-ending mechanisms after issuance.
- Keep session secrets encrypted at rest, absent from logs, and independently revocable.
- Make revocation immediate and consistent across replicas without stale fallback resurrection.
- Distinguish unavailable auth infrastructure from invalid credentials so an outage does not trigger a login loop.
- Require only one final login during migration, without changing connector URLs or registrations.

**Non-Goals:**

- Automatically terminate an Exomem session when GitHub later revokes the temporary authorization token or the account changes.
- Add multi-user authorization, roles, or vault-level permissions.
- Depend on client refresh-token behavior for continuity.
- Preserve legacy FastMCP reference JWTs indefinitely or dual-read legacy GitHub token records.
- Expose privileged session administration as an MCP knowledge-base tool.

## Decisions

### 1. Issue an opaque, non-expiring Exomem bearer session

During the normal IdP callback, `ExomemSessionOAuthProxy` exchanges the GitHub code, validates the access token exactly once with a cache-disabled `SingleUserGitHubVerifier`, and requires both the configured normalized login and the configured `EXOMEM_GITHUB_USER_ID`. The callback extracts a minimal verified identity proof, revokes/discards the GitHub token, and stores only that proof in the short-lived downstream client code. Downstream authorization-code exchange consumes the proof and never sees an upstream credential.

The provider then creates a bearer token with a versioned opaque format containing a random session ID and at least 256 bits of secret entropy. The session ID is only a lookup/revocation handle; possession of it is not sufficient to authenticate. The token response omits `expires_in` and `refresh_token`, which the OAuth response model and MCP SDK represent as optional. `AccessToken.expires_at` is also unset.

The server stores an HMAC of the complete bearer token, never the raw token. The HMAC key is derived from `EXOMEM_JWT_SIGNING_KEY` with an Exomem-session-specific purpose salt distinct from FastMCP JWT signing and storage encryption salts. Rotation therefore makes old token proofs unusable even though the bearer is opaque.

HTTP OAuth startup requires explicit `EXOMEM_JWT_SIGNING_KEY` and `EXOMEM_GITHUB_USER_ID` values. The current fallback that derives keys from the GitHub client secret is rejected with an actionable configuration error: a non-expiring Exomem session needs a deliberate, independently rotatable root of trust. The setup flow resolves and records the numeric GitHub ID for the configured login; subsequent authorization requires both values, preventing a renamed or recycled login from becoming a different trusted subject.

This is preferred over short access JWTs plus local refresh tokens because refresh rotation adds client persistence assumptions and race conditions to the exact path whose reliability matters. It is preferred over one canonical GitHub token because a canonical token still couples every Exomem session to GitHub availability and revocation.

### 2. Keep authorization data in an encrypted server-side session record

Each record is keyed by the random session ID and contains:

- schema version and session ID;
- HMAC token digest;
- MCP client ID and MCP scopes from the downstream authorization code;
- Exomem issuer and protected-resource audience;
- GitHub immutable user ID and normalized login captured at issuance;
- issue time and current random auth-generation ID;
- `active` or `revoked` status plus revocation time/reason.

GitHub-returned scopes are never reused as MCP scopes. Ordinary requests verify the token HMAC with constant-time comparison plus record status, generation, issuer, audience, stored issuing-client context, scopes, and stored account identity. They return the stored issuing-client context in `AccessToken`; an ordinary bearer request provides no independent sender proof. They do not re-check GitHub or current login configuration; changing authorization policy requires explicit revocation.

### 3. Make the session authority remote-canonical and fail closed in HA

A small `SessionAuthority` abstraction owns session records and the auth-generation record.

- In HA, it wraps the coordinator-backed store directly in Fernet encryption. Session and generation reads are uncached and never use `ReadThroughMirrorStorage`. Both the Exomem replica and coordinator must have a non-empty, matching OAuth-storage bearer credential; HA startup fails if `EXOMEM_OAUTH_STORAGE_TOKEN` is absent.
- In a single-node deployment, it uses an encrypted `FileTreeStore` under the stable FastMCP home.
- Session and generation collection names include a non-secret fingerprint of the current signing root. Signing-key rotation therefore reads a fresh namespace where old session IDs are unknown, yielding 401 rather than attempting to decrypt old ciphertext.
- Revoked sessions are overwritten with permanent tombstones, never deleted.
- `revoke --all` replaces the shared generation with a new random value. Existing records remain auditable but no longer validate.

The first generation in a fresh key namespace is initialized atomically so two replicas cannot establish competing roots. Later generation replacement is one authoritative write; issuance always re-reads it before returning a bearer.

Issuance reads the current generation, writes the session, and re-reads the generation before returning the bearer. If a concurrent global revocation changed it, issuance tombstones the provisional record and retries against the new generation. A revocation immediately after the final check is allowed to invalidate the newly returned session, which is the intended outcome of concurrent operator revocation.

Unknown, malformed, mismatched, revoked, wrong-generation, and prior-key-namespace tokens return the normal invalid-token result. Coordinator/network failures and decryption corruption inside the current key namespace raise a distinct auth-store-unavailable error that the HTTP boundary maps to 503 with retry guidance and no `WWW-Authenticate: invalid_token` challenge; they must not be collapsed into `None`/401 by a broad exception handler.

### 4. Provide explicit local and protocol revocation

The operator surface is:

- `exomem auth sessions` — list non-secret session metadata;
- `exomem auth revoke <session-id>` — overwrite one record with a tombstone;
- `exomem auth revoke --all` — rotate the auth generation.

The HA coordinator state API gains the minimal collection enumeration needed for the authenticated operator command. The route requires the existing coordinator bearer credential, which is mandatory for HA auth storage. The session values remain Fernet-encrypted; listing never returns bearer material. Single-session revocation addresses the record directly by its random session ID.

FastMCP's local RFC 7009 revocation endpoint is enabled and maps a client-presented token to the same tombstone operation. The management commands remain CLI/operator-plane only rather than MCP tools: a vault session must not grant authority to enumerate or revoke unrelated sessions.

### 5. Dispose of the temporary GitHub credential in the callback

The overridden IdP callback exchanges the upstream code, validates the configured login and immutable ID, extracts a minimal proof, and then calls GitHub's OAuth application token-deletion endpoint for that exact token using the configured application credentials. Both FastMCP's token cache and Exomem's former login cache are disabled for this one-shot proof. No raw GitHub token or `AccessToken` object enters a cache.

Cleanup is attempted on every callback path after a token is obtained, including identity rejection and failures to persist or redirect the downstream code. On success or best-effort cleanup failure, the raw token is discarded before the callback stores only the verified proof. A transient GitHub cleanup outage is logged and surfaced as an operational alert, but it cannot affect later local session validation. A client that abandons the downstream code therefore leaves no GitHub credential in Exomem storage.

### 6. Isolate the FastMCP private seam

`ExomemSessionOAuthProxy` preserves FastMCP's public provider behavior while overriding the IdP callback lifecycle, downstream authorization-code exchange, access-token loading, and revocation. Provider initialization also enables local RFC 7009 behavior, and the outer HTTP boundary maps session-authority failures to 503. Access to FastMCP's private transaction/code stores and callback helpers is isolated in this adapter instead of leaking through Exomem modules.

The implementation pins exactly `fastmcp==3.4.4`, the inspected production version, and updates the lockfile. Contract tests cover every private callback, transaction/code-store, response, revocation, and exception seam used by the adapter. A FastMCP change requires an explicit pin update plus a green contract suite before deployment.

### 7. Preserve protocol and connector compatibility

OAuth discovery, protected-resource metadata, DCR, consent, callback routes, PKCE, authorization codes, scopes, and connector URLs remain unchanged. The token endpoint returns a normal bearer response with optional lifetime fields omitted.

A black-box Codex CLI acceptance test is a rollout gate: log in once, restart several fresh Codex conversations/processes, and confirm the persisted bearer is reused without a browser prompt. The supported hosted connector receives an equivalent smoke test. If a supported client cannot persist an OAuth token without `expires_in`, implementation stops and returns to design; it does not substitute an arbitrary far-future expiry.

## Risks / Trade-offs

- **A stolen bearer remains valid until explicit revocation** → Use at least 256 bits of secret entropy, HMAC-only storage, encryption at rest, secret-safe logs, per-session revocation, and global generation rotation. This is an accepted trade-off for a single-user service.
- **The auth authority becomes an availability dependency** → Fail with 503 rather than 401, keep the coordinator strongly consistent, and retain encrypted local storage for single-node mode only.
- **FastMCP private internals can change** → Keep one adapter, pin exactly FastMCP 3.4.4, and require explicit pin updates with green contract tests.
- **A client may mishandle omitted `expires_in`** → Gate rollout on black-box Codex and hosted-connector tests; redesign around fully local refresh tokens if required.
- **GitHub token cleanup can fail transiently** → Alert without coupling the local session to cleanup; never retain the credential as session state.
- **Mixed old/new replicas disagree on token format** → Use a coordinated active/passive rollout with no mixed-version serving window.
- **Session enumeration adds an internal coordinator operation** → Require the existing coordinator bearer credential, return only encrypted values/opaque keys, and keep it outside the public MCP/REST knowledge surfaces.

## Migration Plan

1. Implement and test the adapter, session authority, revocation commands, 503 mapping, and client compatibility in an isolated environment.
2. Back up the existing production environment and shared OAuth state. Resolve and persist `EXOMEM_GITHUB_USER_ID`, verify matching non-empty coordinator OAuth-storage credentials, and preserve the existing `EXOMEM_JWT_SIGNING_KEY`; do not rotate it or change the connector URL during this rollout.
3. Quiesce connector traffic or otherwise ensure only the new provider serves requests, then update both active/passive replicas as one coordinated change.
4. Existing FastMCP reference JWTs intentionally receive 401 once. Each client completes one final GitHub authorization and receives a durable Exomem session. Do not delete or recreate connector definitions.
5. Verify a session issued by one replica works on the other, then verify single-session and global revocation across replicas.
6. After the rollback window, remove obsolete JTI/upstream-token records. Do not dual-read them during normal operation.

Rollback deploys the prior provider and leaves the new encrypted session collection inert. Clients that already migrated will need to authorize once into the prior token format. Legacy OAuth records remain untouched until the rollback window closes, so rollback does not require reconstructing them.

## Open Questions

There are no blocking product decisions. Omitted-`expires_in` interoperability with the exact production Codex and hosted connector versions is an implementation acceptance gate rather than an architectural assumption.
