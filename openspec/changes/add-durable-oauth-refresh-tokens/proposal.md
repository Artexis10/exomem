## Why

Exomem currently advertises OAuth refresh-token support while issuing only non-expiring session bearers and rejecting the refresh grant. ChatGPT therefore cannot renew credentials reliably after connector expiry, reconnect, service restart, or replica failover, producing avoidable manual reauthentication and a misleading discovery contract.

## What Changes

- Issue one-hour access tokens plus durable, rotating refresh tokens to OAuth clients that request `offline_access`.
- Make refresh-token redemption restart-safe and replica-safe, with a short idempotency window for concurrent retries and whole-family revocation when an already-rotated token is replayed after that window.
- Advertise the actual `offline_access` scope and refresh grant in OAuth discovery metadata.
- Preserve existing `exo_s1` session validation so installed Codex and Claude connections are not forced to log in again.
- Extend revocation and operational tests to cover refresh families, concurrency, replay, restart/failover, and legacy-session compatibility.

## Capabilities

### New Capabilities

- `durable-oauth-refresh`: Defines Exomem-owned access/refresh token issuance, rotation, concurrency handling, revocation, discovery, and legacy-session compatibility.

### Modified Capabilities

None.

## Impact

- Affected code: OAuth proxy, session authority/storage records, authorization-server metadata, revocation handling, and auth boundary tests under `src/exomem/` and `tests/`.
- Affected APIs: `/authorize`, `/token`, `/revoke`, and OAuth/OIDC discovery documents at `exomem.substratesystems.io`.
- Persistence: refresh-family state and redemption receipts use the existing shared encrypted OAuth storage/coordinator so behavior survives process and replica changes; raw bearer tokens are never persisted.
- Rollout: release 0.24.2, deploy the service, then recreate or refresh the ChatGPT connector once so its frozen OAuth/tool snapshot includes the corrected metadata.
