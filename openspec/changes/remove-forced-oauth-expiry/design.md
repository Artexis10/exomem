## Context

`build_oauth()` constructs FastMCP's `OAuthProxy` over a GitHub OAuth App. GitHub OAuth App access tokens normally omit both `expires_in` and `refresh_token`. FastMCP already detects that shape and selects its no-refresh fallback, but Exomem currently overrides the decision with `fallback_access_token_expiry_seconds=30 * 24 * 60 * 60`. The resulting FastMCP access token expires after 30 days with no refresh token available, forcing users through reauthorization.

## Goals / Non-Goals

**Goals:**

- Remove Exomem's arbitrary 30-day session cutoff.
- Preserve FastMCP's upstream-aware handling when an identity provider supplies expiry or refresh metadata.
- Lock the behavior with a focused construction-level regression test.

**Non-Goals:**

- Change the canonical MCP hostname or OAuth callback.
- Replace GitHub OAuth, patch FastMCP internals, or migrate existing client tokens.
- Make expired app links recoverable inside ChatGPT/Codex; one clean reconnect remains necessary.

## Decisions

Pass no `fallback_access_token_expiry_seconds` argument to `OAuthProxy`. This is preferable to replacing 30 days with another Exomem-owned constant: FastMCP's default explicitly distinguishes upstream expiry, refresh-capable providers, and non-refreshable token providers such as GitHub OAuth Apps.

Test the constructor call by replacing `OAuthProxy` with a recording stub and asserting that the fallback override is absent while stable signing and shared storage inputs remain intact. This avoids binding the test to FastMCP's private token-exchange implementation.

Keep public deployment documentation generic. The personal production hostname is operational configuration, not a value that belongs under `src/exomem/` or the shipped scaffold; user-facing diagnosis should nevertheless use `https://exomem.substratesystems.io` for this deployment.

## Risks / Trade-offs

- [Longer-lived downstream token when GitHub supplies no refresh token] → FastMCP's no-refresh default is intentionally designed for this provider shape; GitHub revocation and Exomem's verifier still gate access.
- [Existing expired sessions remain broken] → document that deployment fixes newly issued sessions and requires one fresh reconnect.
- [A future FastMCP default changes] → the design intentionally delegates provider policy upstream; tests assert absence of an Exomem override rather than pinning FastMCP's current duration.
