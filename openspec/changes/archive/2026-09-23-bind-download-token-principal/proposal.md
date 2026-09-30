## Why

`transfer_artifact(operation="download")` mints a short-lived bearer for `/download`, signed with the long-lived transfer secret. The route decides each requested path through the release plane, but it resolves the principal from the bearer, and the bearer recorded only its scope and expiry. It named no minting principal, so the route could not decide a download under the release ceiling of the caller who asked for it. It decided every minted bearer as the owner, whose key signed it.

A download capability has to name who minted it, just as every other content-returning read resolves its principal at the surface boundary.

## What Changes

- A download capability carries the canonical audience of the principal that minted it. The audience is signed together with the scope and expiry under the transfer secret, so the holder cannot swap it.
- `/download` resolves a presented capability to that audience and decides the requested path under it, through the same full-disclosure release decision the route already applies. Only the owner's own mint resolves to the owner. The long-lived transfer secret itself stays the owner's credential.
- A minting caller whose principal is unresolved, or who has no principal bound, binds the most-restrictive audience, never the owner.
- A capability without an audience claim is refused as an unauthenticated download. That includes one minted before this change: capabilities live fifteen minutes, and a claimless one names nobody, so there is no principal to recover.
- The capability stays path-free. The tool takes no path and client guidance chooses the path at download time, so binding one at mint time would break the existing contract. Audience binding already limits the capability to what its minter may read in full.
- Missing and withheld paths return one refusal, spelled from the request rather than from the resolver's on-disk spelling, so a case-insensitive filesystem no longer distinguishes them.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `release-gate`: out-of-band download capabilities carry, and are decided under, the principal that minted them.

## Impact

- Affected code: `src/exomem/upload_tokens.py` (`mint_bound`, `bound_audience`, `mint_for_endpoint(audience=...)`), `src/exomem/commands.py` (`op_transfer_artifact` binds the effective principal's audience), `src/exomem/server_transfer.py` (`download_principal`, the download branch of `_authorized`, the `/download` refusal).
- Affected tests: `tests/test_download_token_principal.py` is new. `tests/test_upload_tokens.py` gains the bound-token primitives. `tests/test_download_endpoint.py`, `tests/test_governance_tokens.py` and `tests/test_governance_structured_direct.py` mint owner-bound capabilities where they previously minted claimless ones.
- Contract: the `transfer_artifact` tool schema and description are unchanged, and so is the `{token, ttl_seconds, download_url}` handoff. The token's wire form for downloads changes from `v1.<exp>.<sig>` to `v2.<exp>.<audience>.<sig>`, and clients already treat it as opaque. Upload tokens are unchanged.
- Hosted: unaffected. The hosted runtime intercepts `transfer_artifact` and uses its own tenant-bound transfer grants.
