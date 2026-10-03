# Design

## Context

See proposal.md for the reproduced failure. `client_artifacts.py` already stages bounded downloads before the canonical writers. Cloud runtime policy currently denies every outbound connection. Substrate's gateway authenticates and routes MCP without fetching files. Local held uploads require a local session; Cloud registers no upload route.

## Goals / Non-Goals

Restore file-handle preservation in Cloud with one safe-fetch implementation. Do not introduce a second custody store, Cloud upload authority, model-visible binary payload, compiler change, preference mutation or general tenant egress.

## Decisions

1. Use a pull broker, importing the existing safe-fetch implementation. A push design would introduce another cell route, staging authority and lifecycle. Cells continue to use the existing ordered per-file loops and commit functions.
2. Gateway signs a separate Ed25519 grant after authenticating and routing the actual JSON-RPC `tools/call` for an explicitly selected cell. Bound authority inspection to 1 MiB and five seconds. On incomplete inspection, forward the buffered prefix and remaining original stream without a grant; known oversized bodies retain pass-through directly. This limits new authority without rejecting a previously supported large or slow ordinary save. Non-artifact calls retain their bytes and semantics; the inspection buffer is bounded by the existing 16-call gateway admission. Do not infer operation from an untrusted header; retain the existing refusal of caller-supplied internal headers.
3. Grant claims are version 1, audience `exomem-artifact-broker`, cell ID, operation (`capture_source` or `preserve_artifacts`), UUID nonce, issued milliseconds, expiry (60 seconds) and an ordered list of descriptor digests. Each digest is SHA-256 over UTF-8 JSON `[canonical_file_id, download_url, mime_type-or-null, file_name-or-null]` with compact separators and literal Unicode. Canonical file ID uses the existing custody writer's Python Unicode whitespace stripping; project only the four descriptor fields consumed by the public schema. The Node producer and Python verifier must exercise this normalization together. URLs are bounded to 8192 characters. Signing material belongs only to the gateway; the broker receives a public key. The cell receives the grant in `x-exomem-artifact-grant`, checks its cell/operation binding, and forwards it only to the configured broker, never to a public download host.
4. Broker `POST /internal/artifacts/fetch/v1` takes one descriptor and its index with the grant as its bearer. It verifies signature, audience, lifetime, startup cutoff, cell, operation and digest before retrieval. A nonce record owns the shared FetchBudget and consumed indexes. Allow one active transfer per cell and four globally; retain slots through body completion/cancellation. Up to 1024 non-expired nonce records are retained; no live eviction. Each index is claimed once, including failure. One process/replica and non-overlapping Recreate rollout keep these bounds real. Reject pre-start grants; clients can start a fresh call after a broker restart.
5. Broker responses stream bytes with size, SHA-256, content type and sanitized filename metadata. Cells independently count and hash them into private temporary files, then reuse existing writers. HTTP failures are bounded content-free artifact errors; no final file exists until the canonical writer commits it. Broker storage is ephemeral with no vault or database access. Uvicorn access logging is disabled.
6. Configure the broker's actual literal Service ClusterIP and fixed TCP port 8767 in cellctl ClusterConfig/CellManifestSpec. Deployment resolves and verifies it once; no new runtime DNS or controller Service-discovery privilege. An explicit cell-ID activation list starts with the reviewer only. Include the endpoint in render-digest inputs only for selected cells; disabled/unselected cells retain their existing digest, environment and zero-egress policy. Runtime NetworkPolicy and cellctl admission allow only the broker namespace/pod selector and port. Broker egress allows DNS and public HTTPS/443 with private/metadata ranges excepted; safe-fetch revalidates and pins every redirect. Gateway networking is unchanged.

## Risks / Trade-offs

- A shared broker adds a file-only network hop and availability dependency. Retrieval remains outside the mutation lock; unavailable transport produces a file failure and does not block ordinary tools.
- Grant extraction buffers a bounded prefix instead of immediately passing through. The inspection cap prevents an unbounded new allocation; larger or slow ordinary calls keep their original streaming path without new fetch authority. Signed grant headers contain digests, not temporary URLs.
- Restart invalidates in-flight grants and removes ephemeral replay state. A fresh client call receives a new grant; canonical preservation receipts retain duplicate protection. No persistent broker database is justified.
- Isolation changes must reach rendering and admission together. Updating only the runtime NetworkPolicy is rejected by current admission.
- This capability cannot repair a client that supplies no attachment handle. Public guidance continues to distinguish available transports instead of pretending custody succeeded.

## Migration Plan

Deploy the broker image, public key, restrictive policies and Service first. Verify its literal ClusterIP and readiness. Coordinate gateway signing configuration, compatible controller policy/configuration and reviewer runtime with the rollout owner, then test the synthetic reviewer file and denied network paths. No database or vault migration. Keep owner/ordinary QA, fleet and preferences unchanged. Rollback disables grant issuance and the broker route and restores the zero-egress runtime rule after transfers finish; stored canonical artifacts remain intact. Submit/Publish stays held for Hugo review.
