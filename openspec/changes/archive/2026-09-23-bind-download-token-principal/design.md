## Context

`upload_tokens.mint` signs `scope:exp` with `EXOMEM_UPLOAD_TOKEN` and emits `v1.<exp>.<sig>`. The upload lane is bound the same way, folded into the signed scope. That works because the verifier can enumerate the lanes. A principal cannot be enumerated, so the audience has to travel inside the token.

`/download` already runs `egress.release_allows_download` under `server_transfer.download_principal` before it reads any bytes, and it already maps a withheld target to the route's own `NOT_FOUND`. The missing piece was the principal. `download_principal` returned `owner_principal(surface="transfer")` for the raw secret and for any verified minted token alike.

## Goals / Non-Goals

**Goals:**

- A minted download capability is decided under its minter's canonical audience, on every surface that can call `transfer_artifact`: MCP (stdio owner, OAuth, bearer), REST (shared key, Cloudflare Access) and the CLI.
- The raw transfer secret remains the owner.
- No capability without an audience resolves to the owner.
- Withheld and missing paths refuse identically.

**Non-Goals:**

- Change upload tokens, their lanes, or `/upload`.
- Change the `transfer_artifact` tool schema, description, or handoff shape.
- Carry session-scoped authority across the out-of-band hop.
- Change the hosted transfer grants, which already bind principal and target.

## Decisions

### Wire form

A bound capability is `v2.<exp>.<audience>.<sig>`. `<audience>` is the lower-case hex of the canonical audience id's UTF-8 bytes. `<sig>` is `HMAC-SHA256(secret, "v2\0<scope>\0<exp>\0<audience>")`. The version tag and NUL separators keep a bound message from ever equalling a `v1` `scope:exp` message, and keep the audience from bleeding into the scope or expiry. Verification rejects any claim that does not re-encode to itself, which gives each audience exactly one spelling. It compares the signature as bytes, so a non-ASCII signature is simply a mismatch rather than an exception.

The claim is hex rather than base64 because the handoff crosses the shared dispatcher's terminal credential scrubber. That scrubber rewrites long mixed-case, high-entropy runs in fields it does not recognise as identifiers, and `token` is not one of them. A base64 claim for a `principal:` id is exactly such a run, so a non-owner would have received a dead capability. Single-case hex, like the signature beside it, is never such a run. A test pins the round trip through the REST dispatcher.

### Resolution

`bound_audience` returns the verified audience. `download_principal` maps `owner` to the owner principal, maps a reserved `\x00` id (the fail-closed floor) to the most-restrictive principal, and maps any other audience to a resolved `RequestPrincipal` on the `transfer` surface. `op_transfer_artifact` binds `effective_principal()`'s audience when that principal is resolved and the floor otherwise. `effective_principal()` already returns the floor when nothing is bound, so an unbound mint cannot become the owner, and `mint_for_endpoint` defaults to the floor for the same reason.

The floor is not a dead token. Under an empty policy, where no governance is configured, a floor-bound token downloads everything, exactly as `read_memory` returns everything to the same caller. The floor restricts only once a policy exists.

### Path binding: path-free, audience-bound

The tool takes no path. The handoff is `{token, ttl_seconds, download_url}`, and the scaffold guidance tells clients to mint once and then `GET download_url?path=...`. Binding a path at mint time would need a new tool parameter and a regenerated tool contract, and it would break every client that downloads more than one file per token. It would also add nothing to what a principal may disclose: once the capability is bound to its minter's audience, it downloads at most what that principal could already read in full through `read_memory`. What path binding would still add is a narrower blast radius if a token leaks within its fifteen-minute life. That is left to a future change that also adds the parameter.

### Tokens without an audience are refused

A `v1` download token carries no principal. Resolving it as the floor would still let it through the empty-policy fast path, which is exactly the behaviour being retired. So the download branch of `_authorized` accepts only the raw secret or a verified bound capability, and a claimless token gets the route's ordinary `401`. Tokens live fifteen minutes, so the only cost is one re-mint for a client that minted just before an upgrade.

### Standing authority only

A capability carries the audience alone. It carries neither the minting call's purpose nor its verified authorization session. The release-gate requirement already forbids moving ephemeral grants, purposes or tokens between issuer families or sessions on canonical-principal equivalence alone, and an out-of-band bearer is exactly such a move. A download is therefore decided on standing policy for the minter's audience, which is at most what the minter holds.

### One refusal

Every `NOT_FOUND` from the download route (missing, withheld, reserved leaf) is rendered once, from the request's own normalized spelling. On NTFS and APFS the resolver re-spells an existing path to its on-disk casing but leaves a missing one as typed. Echoing the resolver's spelling would therefore separate a withheld file from a missing one and reveal the withheld file's real name.

### Hardening from review

- **Expiry parsing.** Both token parsers accept an expiry of at most twelve ASCII digits. `str.isdigit` admits superscripts that `int()` refuses, and `int()` refuses more than 4,300 digits outright, so either used to surface as a server error rather than a refusal.
- **Bearer comparison.** Every presented bearer is compared as bytes: the raw transfer secret, the upload lane lookup, the REST key and the lease coordinator's two tokens. `compare_digest` raises on a non-ASCII str, and a header carries whatever bytes the caller sent.
- **One refusal for folders.** `/download` renders `NOT_A_FILE` exactly as it renders `NOT_FOUND`, so a folder inside a withheld scope is indistinguishable from a missing path.
- **No server paths in refusals.** `/download` answers every other path refusal with a fixed `INVALID_PATH` reason. The resolver's own reason names the absolute path a traversal reached, or an escaping symlink's target.
- **Release before decoding.** A direct page read (`read_memory`, `get`, `fetch`, exact unit reads, and the page and frontmatter helpers beneath them) used to report bytes that failed to decode or parse as `UNREADABLE`, with the codec's message, before the release decision ran. That disclosed existence, one content byte and its offset. An unreadable result is now reported only where a path-only release decision releases the item to the caller, and with a fixed reason. Everywhere else it is the absent refusal, byte-identical to a missing file's. The full decision needs the frontmatter the bytes failed to yield, so a scope that needs frontmatter to classify the path withholds it.

## Risks / Trade-offs

- A capability minted by a principal with session-scoped grants downloads less than that session could read inline. That is deliberate: it fails closed, and the minter can still read inline.
- A leaked capability still discloses anything its minter may read in full, for up to fifteen minutes. The same was true before, with the owner's ceiling in place of the minter's.
- Timing still separates a withheld file from a missing one. A missing path stops at the existence check, while a withheld one runs the release decision first, and the review measured roughly 10 ms against 1.6 ms on `/download`. Every direct read shows the same gap, `read_memory` included, so it is a systemic property of the read path and is not fixed here.
- A symlink that escapes the vault answers `INVALID_PATH`, while a missing path answers `NOT_FOUND`, so the existence of such a link inside a withheld scope is still observable. The refusal names no server path.
