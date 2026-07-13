## Why

Exomem currently retains one upstream GitHub OAuth token per MCP client and asks GitHub to validate that token during ordinary MCP requests. GitHub revokes older tokens after ten authorizations for the same user, application, and scopes, so reconnecting one Codex or Claude client can invalidate another and create an endless login loop.

## What Changes

- **BREAKING** Replace existing FastMCP reference JWTs backed by per-client GitHub tokens with Exomem-owned, opaque MCP sessions. Existing clients perform one final OAuth authorization after deployment.
- **BREAKING** Require an explicit `EXOMEM_JWT_SIGNING_KEY` whenever HTTP OAuth is enabled; startup fails with remediation instead of deriving durable-session keys from the GitHub client secret.
- **BREAKING** Require `EXOMEM_GITHUB_USER_ID` to anchor the configured login to GitHub's immutable numeric subject, and require `EXOMEM_OAUTH_STORAGE_TOKEN` whenever shared HA auth storage is enabled.
- Use GitHub only during authorization-code exchange to prove the configured account's login and immutable user ID.
- Issue a high-entropy Exomem bearer token with no advertised expiry or refresh token, backed by a durable encrypted server-side session record.
- Validate ordinary MCP requests entirely against Exomem's authoritative session store, with no GitHub API call or retained upstream GitHub credential.
- Add immediate single-session and global Exomem revocation, plus invalidation on signing-key rotation.
- Make HA session validation uncached, remote-authoritative, and fail closed without allowing stale local state to resurrect revoked sessions.
- Return service-unavailable failures for authoritative-store outages instead of `401 invalid_token`, preventing infrastructure failures from triggering fresh authorization loops.
- Validate and revoke each short-lived GitHub token inside the IdP callback before persisting the downstream client code, so abandoned or failed client exchanges retain no upstream credential.
- Preserve the existing connector URL, OAuth discovery, DCR, consent, callback, PKCE, and authorization-code behavior.

## Capabilities

### New Capabilities

- `durable-mcp-auth-sessions`: GitHub-backed one-time identity proof, durable Exomem session issuance and validation, HA-safe revocation, operator management, migration, and failure semantics.

### Modified Capabilities

None.

## Impact

- Affects OAuth wiring and token exchange in `src/exomem/server_auth.py`, shared auth storage, the internal HA coordinator state API, auth-related CLI commands, remote setup/doctor documentation, and auth tests.
- Introduces no new external dependency; it pins the production-tested FastMCP 3.4.4 seam and continues to use `httpx`, Fernet-encrypted storage, and the now-required stable `EXOMEM_JWT_SIGNING_KEY`.
- Requires a coordinated replica rollout because old and new auth providers intentionally accept different token formats.
- Requires one final OAuth login for every existing MCP client. Connector definitions and URLs remain unchanged.
