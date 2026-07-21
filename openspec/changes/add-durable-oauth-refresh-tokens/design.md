## Context

Exomem uses GitHub only to prove the configured user's identity, then replaces the upstream credential with an Exomem-owned opaque `exo_s1` session stored through the encrypted local/shared session authority. Those sessions intentionally have no expiry or refresh path. The proxy nevertheless inherits FastMCP metadata that advertises the refresh grant, while `load_refresh_token` returns `None` and `exchange_refresh_token` returns `invalid_grant`. ChatGPT now depends on a real offline refresh contract and freezes discovered OAuth/tool metadata into an app version, so the mismatch produces manual reconnects and misleading connector setup.

The current authority already provides purpose-separated HMAC/encryption keys, encrypted durable collections, an atomic `put_if_absent`, generation-based global invalidation, and uncached remote-canonical reads. Those primitives must remain the source of truth across the laptop writer, passive replicas, and process restarts.

## Goals / Non-Goals

**Goals:**

- Give clients requesting `offline_access` a one-hour opaque access token and a durable rotating refresh token.
- Make a refresh retry safe across concurrent ChatGPT workers, processes, restarts, and replicas without persisting raw bearer tokens.
- Detect refresh-token reuse outside a short retry window and revoke the complete token family.
- Keep every existing `exo_s1` session valid and keep the old non-expiring issuance behavior for clients that do not request `offline_access`.
- Make canonical and compatibility discovery documents accurately advertise the supported scopes and grant.

**Non-Goals:**

- Retaining or refreshing GitHub credentials after identity bootstrap.
- Replacing OAuth with OIDC or issuing ID tokens.
- Forcing existing Codex, Claude, or other clients to reconnect during deployment.
- Solving ChatGPT's frozen tool snapshot in place; the post-deploy connector must be refreshed or recreated once.

## Decisions

### 1. Opt-in v2 token family alongside legacy sessions

An authorization code whose granted scopes include `offline_access` produces an `exo_a2` access token, an `exo_r2` refresh token, `expires_in=3600`, and the granted scope string. Authorization without `offline_access` continues to produce the current non-expiring `exo_s1` bearer with no refresh token. Validation accepts both formats, so rollout is additive and existing records require no rewrite.

Alternative considered: convert every authorization to expiring access tokens. Rejected because clients that do not request or implement refresh would be disconnected after one hour and existing connector behavior would regress.

### 2. Opaque deterministic refresh descendants with encrypted family metadata

Each refresh family has a random family identifier and immutable encrypted metadata: client, scopes, identity, issuer, audience, signing generation, creation time, and active/revoked state. Refresh tokens contain only a version, family identifier, monotonically increasing sequence, and a purpose-separated HMAC proof. The next token is deterministically derived for `sequence + 1`; raw refresh bearers are never stored. Access records retain only their existing keyed digest plus expiry and family identifier.

Alternative considered: store each next refresh bearer encrypted in a redemption record. Rejected because deterministic descendants give idempotent responses without keeping reusable bearer material at rest.

### 3. Atomic redemption receipts implement rotation and retry idempotency

Redeeming refresh sequence `n` creates one permanent encrypted receipt keyed by `(family_id, n)` via the existing shared `put_if_absent`. A matching encrypted grace marker carries the same random claim ID and a 30-second TTL enforced by the authoritative storage coordinator, not a replica clock. Creating the grace marker before the permanent receipt makes crash recovery safe; claim-ID matching prevents a late observer from recreating an expired marker around an older receipt. The winner returns the deterministic `n+1` refresh token and a new one-hour access token. A concurrent or repeated redemption with the matching live marker returns the same `n+1` refresh token and may issue a fresh access token. A use after the marker expires tombstones the family as refresh-token reuse and returns `invalid_grant`.

No compare-and-swap, replica-local clock, or replica-local lock is required: the permanent receipt plus coordinator-TTL marker is the single global claim. Family state is re-read after claiming and access-token validation checks the family, so a concurrent family revocation fails closed or invalidates any just-issued access token.

Alternative considered: rotate a mutable `current_sequence` field with ordinary writes. Rejected because the current store has no compare-and-swap and two replicas could both accept different rotations or overwrite revocation state.

### 4. Family revocation is authoritative and generation rotation remains the global kill switch

Revoking an `exo_r2` or `exo_a2` token revokes its complete family; otherwise an operator or client could "disconnect" an access token and have it silently resurrected through refresh. Reuse detection likewise revokes the family and therefore invalidates every access token bound to it. Replacing the existing signing generation invalidates legacy sessions, v2 access tokens, and refresh families without enumerating records. Unknown tokens still receive RFC 7009's non-disclosing success response.

### 5. Honest discovery and bounded scope behavior

The authorization server advertises `offline_access`, `exomem:read`, and `exomem:write` as valid scopes and continues advertising both `authorization_code` and `refresh_token` grants. The protected-resource documents advertise the resource scopes. Refresh requests may retain or narrow the originally granted scopes but cannot expand them. `offline_access` is a protocol scope, not an additional MCP authorization requirement.

## Risks / Trade-offs

- [A client retries a consumed refresh token after 30 seconds because its first response was lost] → Treat it as possible credential theft and revoke the family; the client must reauthorize. The 30-second window covers normal concurrent/retry races without leaving replay acceptance open indefinitely.
- [The shared authority becomes unavailable during refresh] → Return retryable `temporarily_unavailable`/HTTP 503 and do not fall back to replica-local state or mint an untracked token.
- [A failure occurs after the redemption receipt is committed but before the response is delivered] → Retries within the window reconstruct the same next refresh token and can issue another access token.
- [A replay races a legitimate current-token redemption] → Re-read family state after the atomic claim and check it on every v2 access validation; revocation wins from the caller's perspective.
- [Discovery metadata is cached by ChatGPT] → Release and deploy first, then refresh actions or recreate the app once; no server-side change can mutate an already-frozen connector snapshot.

## Migration Plan

1. Add v2 codecs and backward-compatible records to the session authority, with tests proving old v1 records still parse and validate.
2. Add refresh-family issuance, load, rotation, idempotency, replay detection, and revocation using encrypted shared collections.
3. Wire the OAuth proxy and discovery metadata, then run focused auth tests, the full suite, lint, and an independent security review.
4. Release 0.24.2 and restart the Exomem service. Existing `exo_s1` sessions continue without intervention.
5. Recreate or refresh the ChatGPT Exomem app with `offline_access` as its base scope, authorize once, and verify a refresh grant succeeds without GitHub interaction.
6. Roll back the service package if necessary. V2 tokens then fail closed on the old version while legacy sessions remain usable; restoring 0.24.2 restores the durable v2 records because storage is versioned and retained.

## Open Questions

None. The lifetime, retry window, rotation, replay, revocation, compatibility, and rollout behavior are fixed by this change.
