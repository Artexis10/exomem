## Context

The current binary route is a two-step transport: `transfer_artifact(operation="upload")` mints a short-lived capability, then the client runtime sends multipart bytes to `/upload`. That works only when the runtime holding the attachment also has outbound network access. ChatGPT's MCP connector can reach Exomem while its file-owning Code Interpreter sandbox cannot, so a valid token is minted but no bytes ever arrive.

OpenAI's plugin contract now lets an MCP tool mark a top-level input as `openai/fileParams`. ChatGPT then supplies temporary `download_url` and `file_id` values, plus optional MIME type and filename. This is a transport adapter, not a trust boundary: Exomem remains responsible for authenticated command admission, bounded retrieval, and governed persistence.

## Goals / Non-Goals

**Goals:**

- Expose one canonical `preserve_artifacts` command across the generated MCP, REST, OpenAPI, and CLI surfaces.
- Let ChatGPT preserve one or many attachments in one MCP call without Code Interpreter, base64, or client-side egress.
- Keep the command client-neutral: any caller that can supply a temporary HTTPS file handle and opaque file ID can use the same leaf.
- Reuse `preserve_stream` for append-only Evidence layout, filename sanitization, hashing, sidecars, indexing, and media reconciliation.
- Fetch outside the wide vault mutation boundary, then acquire the existing writer/mutation authority only for each canonical commit.
- Return truthful per-file outcomes so partial batches are recoverable and retryable without claiming missing bytes landed.

**Non-Goals:**

- Making a client expose attachments when it has no file-handle capability. Claude and similar clients continue to use `transfer_artifact` plus `/upload` until they expose an equivalent handle.
- Removing or changing `/upload`, its token contract, the browser form, or local desk-side ingestion.
- Sending binary bytes through model-visible base64 arguments.
- Providing all-or-nothing atomicity across an arbitrary multi-file batch; the append-only sink commits and reports each item independently.
- Enabling outbound fetches inside hosted tenant cells. Hosted acquisition requires a separate gateway-to-transfer-v2 design and is not exposed by this delivery.

## Decisions

### One capability, transport adapters at the edge

The public command is `preserve_artifacts`, not a provider-branded name. Its required `files` array uses the exact four-field file-object contract OpenAI scans: `download_url` and `file_id` are required strings; `mime_type` and `file_name` are declared optional strings. MCP registration adds `meta={"openai/fileParams": ["files"]}`. REST and CLI callers can submit the same objects directly. The legacy `transfer_artifact` command remains a lower-level compatibility transport.

This is preferred over overloading `transfer_artifact`, whose current result-only token minting has different retry and schema semantics, and over a ChatGPT widget, which would add UI, CSP, CORS, and browser-auth surfaces to a workflow the file-parameter contract already solves.

### Tool metadata belongs in the command registry

`Command` gains immutable, optional MCP descriptor metadata, and the single generated-registration loop passes it to FastMCP. The registry remains the source of truth; there is no hand-registered exception for `preserve_artifacts`. Schema-fidelity tests cover both the exact file-object schema and serialized `_meta`.

### Remote file handles are hostile input

The fetcher accepts HTTPS only, rejects userinfo and fragments, sends no Exomem credentials, cookies, or caller headers, and never logs the URL or query. Each initial URL and redirect is bounded and validated. DNS resolution must yield only globally routable addresses, and the connection must be pinned to a validated address while retaining the original host for TLS SNI and HTTP `Host`. Loopback, private, link-local, multicast, unspecified, reserved, and metadata destinations are refused for IPv4 and IPv6.

Downloads use bounded connect/read/write/pool timeouts, no automatic decompression, a small redirect limit, an item-count cap, per-file and aggregate byte caps, and streaming into private temporary files. `Content-Length` is rejected early when oversized; the streaming counter remains authoritative. Temporary files and signed URLs are removed from observable results and logs.

An optional exact/suffix host allowlist may narrow deployment policy further, but public-address validation remains mandatory and the feature does not depend on undocumented provider hostnames.

### Narrow commit boundary and per-file results

`preserve_artifacts` is a mutating command with implicit MCP retry protection, but it joins the narrow-boundary set. It downloads and validates outside the vault lock, then calls `preserve_stream` under `active_manager().mutation_guard(...)` for each staged item. Media reconciliation follows the existing upload behavior and soft-fails with a content-free warning after the original bytes are preserved.

Each input produces exactly one ordered result keyed by `file_id`: `stored` includes the existing path/size/hash/media fields; `failed` includes a stable code and sanitized reason. Processing continues after an item-level failure. If any item committed, the mutation is marked committed before returning the batch envelope. A replay of the same MCP mutation receives the cached terminal result rather than downloading or writing again.

When `file_name` is absent, the staged SHA-256 and declared/observed MIME type produce a deterministic `attachment-<hash-prefix>.<ext>` fallback. Existing append-only collision refusal remains authoritative.

### Capability-driven guidance

Bootstrap and the scaffold teach one decision:

1. If the client can populate `preserve_artifacts.files`, call it directly for the whole batch.
2. Otherwise call `transfer_artifact(operation="upload")` and deliver bytes through `/upload` or the prefilled browser form.
3. Never infer success from token minting; only stored path, size, and digest prove preservation.

This replaces the current promise that a web sandbox can always `curl` the returned URL and also corrects the stale documented parameter name `mode` to `operation`.

## Risks / Trade-offs

- **[SSRF or DNS rebinding through a temporary URL]** → Pin each connection to a validated public address, revalidate every redirect, bound all resources, and never forward credentials.
- **[Provider changes its attachment host or URL shape]** → Do not hardcode OpenAI hostnames; rely on the documented four-field contract plus transport-safe URL validation.
- **[One item fails after earlier items commit]** → Return ordered per-file outcomes and rely on append-only paths plus mutation replay; do not claim batch atomicity.
- **[Long downloads hold writer authority]** → Stage outside the vault boundary and acquire the mutation guard only around canonical persistence.
- **[A client without file handles still needs two steps]** → Keep `/upload` as an explicit fallback; a uniform tool name cannot manufacture a byte transport the client does not expose.
- **[Hosted cells are intentionally egress-denied]** → Do not expose this command in the hosted-alpha profile; a future hosted change must fetch at the gateway and enter through transfer-v2.

## Migration Plan

1. Add the command and descriptor metadata without changing existing tool schemas.
2. Refresh connector discovery so ChatGPT sees `openai/fileParams`.
3. Deploy with `/upload` still enabled; clients can fall back immediately if direct handles are unavailable.
4. Roll back by removing the new command/metadata. No stored artifact format or existing transfer route changes, so preserved files remain valid.

## Open Questions

None for the self-hosted delivery. Hosted gateway acquisition remains a separate design because tenant cells intentionally have no general outbound egress.
