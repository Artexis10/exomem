## 1. Token Authority Tests

- [x] 1.1 Add failing codec and record tests for `exo_a2` access tokens, deterministic `exo_r2` refresh descendants, expiry, encrypted family metadata, and v1 record compatibility.
- [x] 1.2 Add failing authority tests for offline issuance, successful rotation, scope narrowing, 30-second retry idempotency, late-replay family revocation, and access invalidation.
- [x] 1.3 Add failing shared-storage tests proving concurrent claims, restart/failover behavior, and fail-closed store outages.

## 2. Token Authority Implementation

- [x] 2.1 Add purpose-separated v2 access/refresh codecs and backward-compatible session record fields without changing `exo_s1` validation.
- [x] 2.2 Add encrypted refresh-family and redemption-receipt records plus offline family issuance to `SessionAuthority`.
- [x] 2.3 Implement atomic refresh rotation, deterministic retry responses, late-replay family revocation, v2 access expiry/family checks, and refresh/access revocation.

## 3. OAuth Boundary

- [x] 3.1 Add failing proxy and HTTP boundary tests for offline authorization-code exchange, refresh loading/exchange, invalid client/scope/grant behavior, RFC 7009 revocation, and absence of GitHub/legacy FastMCP token-store access.
- [x] 3.2 Wire the v2 authority into `ExomemSessionOAuthProxy` while retaining legacy issuance for authorization without `offline_access`.
- [x] 3.3 Add failing discovery tests, then advertise `offline_access`, `exomem:read`, and `exomem:write` consistently in canonical and compatibility OAuth metadata.

## 4. Verification and Rollout

- [ ] 4.1 Run focused auth/session tests, the full test suite with embeddings disabled, and `ruff check`.
- [ ] 4.2 Run an independent security review covering concurrent refresh races, replay, revocation, secret persistence, failover, and legacy compatibility; address every actionable finding.
- [ ] 4.3 Open the implementation PR, pass CI, release version 0.24.2, deploy/restart Exomem, and verify health plus public OAuth discovery.
- [ ] 4.4 Refresh or recreate the ChatGPT Exomem app with base scope `offline_access`, authorize once, and verify an actual refresh grant succeeds without GitHub reauthentication.
