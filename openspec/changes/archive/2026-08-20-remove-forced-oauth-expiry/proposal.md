## Why

Exomem currently overrides FastMCP's provider-aware OAuth lifetime with a forced 30-day access-token expiry. GitHub OAuth Apps do not issue refresh tokens, so otherwise healthy ChatGPT/Codex connections become unrecoverable on that schedule and fall into a broken reauthentication flow.

## What Changes

- Stop forcing a 30-day fallback lifetime in the GitHub OAuth proxy.
- Let FastMCP choose its provider-aware fallback: upstream `expires_in` when supplied, refresh-aware behavior when available, and its long-lived no-refresh fallback for GitHub OAuth App tokens.
- Add regression coverage that rejects reintroducing a fixed fallback expiry.
- Document `https://exomem.substratesystems.io` as the canonical deployed host while keeping deployment examples generic.

## Capabilities

### New Capabilities

- `oauth-session-longevity`: Remote OAuth sessions inherit provider-aware token lifetime and refresh behavior instead of an Exomem-imposed recurring expiry.

### Modified Capabilities

None.

## Impact

Affected areas are `src/exomem/server_auth.py`, OAuth unit tests, and remote deployment documentation. There are no MCP, CLI, REST, vault-format, or dependency changes. Existing expired client connections still require one clean reconnect; newly issued sessions no longer inherit Exomem's forced 30-day cutoff.
