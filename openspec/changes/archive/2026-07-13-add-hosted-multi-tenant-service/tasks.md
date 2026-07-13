## 1. Mutation Safety Foundation

- [x] 1.1 Add pure-logic and multiprocessing tests for one reentrant process-safe mutation boundary per canonical vault, independent vault concurrency, bounded timeout, exception release, and process-exit recovery.
- [x] 1.2 Implement the cross-platform `VaultMutationCoordinator` with a process-local reentrant guard plus an OS-backed lock file under the configured state root.
- [x] 1.3 Route every registry write through the coordinator while preserving writer-lease checks, explicit idempotency, bounded MCP retry replay, result envelopes, and local single-vault defaults.
- [x] 1.4 Namespace idempotency records by canonical vault/cell identity so identical public keys remain independent across tenant cells.
- [x] 1.5 Route upload finalization and media-sidecar commit sections through the same coordinator; keep expensive extraction outside the lock and hosted media default-off until safe commit wiring is active.
- [x] 1.6 Add adversarial same-vault concurrency tests covering twenty captures, identical evidence uploads, complete `index.md`/`log.md` updates, and absence of temporary/backup residue.

## 2. Hosted Cell Configuration And Lifecycle

- [x] 2.1 Add tests for explicit hosted mode, missing/overlapping/shared roots, symlink/path rejection, immutable cell binding, and unchanged ordinary local startup.
- [x] 2.2 Implement `HostedCellConfig` and policy parsing for opaque cell identity, vault/state/log roots, private service credentials, protocol version, resource limits, and optional compute grants.
- [x] 2.3 Wire hosted startup to skip dotenv, apply privacy-safe defaults, validate all required boundaries before listeners/workers start, and refuse unsafe configuration with stable codes.
- [x] 2.4 Add private hosted liveness/readiness and quiesce/resume state with content-free checks for vault binding, mutation authority, service authentication, worker safety, and read/write admission.
- [x] 2.5 Add idempotent hosted-cell provisioning that initializes the generic scaffold in an empty staged root, atomically promotes it, and refuses to overwrite incompatible or existing tenant data.
- [x] 2.6 Add hosted lifecycle tests for readiness degradation, quiesce draining, mutation rejection while quiesced, idempotent resume, and deletion sealing.

## 3. Gateway And Transfer Contract

- [x] 3.1 Publish a versioned registry-derived gateway contract containing command names, parameter schemas, read/write metadata, stable envelopes, and compatibility metadata without hand-copied tool definitions.
- [x] 3.2 Require unique private cell authentication plus matching trusted cell context on hosted REST, transfer, readiness, and lifecycle routes; reject public tenant selectors and cross-cell credentials before command dispatch.
- [x] 3.3 Accept only authenticated gateway retry/principal scope, combine it with cell identity for idempotency, and ignore or reject equivalent caller-controlled public headers.
- [x] 3.4 Add tenant-bound hosted transfer grants and verification for operation, cell audience, expiry, and unique grant identity without returning cell master credentials or private addresses.
- [x] 3.5 Add two-cell conformance tests for identical titles/paths, selector attacks, cross-cell service credentials, idempotency-key reuse, transfer replay, unavailable-cell failure, and content-free errors.

## 4. Canonical Vault Portability

- [x] 4.1 Add golden tests for the versioned durability classification registry covering canonical Markdown/media/history versus derived sidecars, secrets, locks, temporary files, and provider logs.
- [x] 4.2 Implement quiesced deterministic export with a versioned manifest of normalized relative path, size, SHA-256, artifact class, schema version, and Exomem release.
- [x] 4.3 Verify every completed archive against its manifest before returning an opaque private artifact reference and integrity metadata.
- [x] 4.4 Implement restore preparation into a new empty staging root with traversal, absolute-path, link, device, duplicate, case-collision, digest, size, and resource-limit rejection.
- [x] 4.5 Add atomic staged-root publication helpers and readiness behavior that rebuilds derived indexes while preserving canonical bytes and serving safe lexical recall.
- [x] 4.6 Implement private idempotent export release and deletion-preparation checkpoints that seal a cell without claiming external account, billing, backup, storage, or KMS deletion.
- [x] 4.7 Add export/restore/deletion tests proving byte-identical round trips, no cross-tenant sentinel leakage, derived-sidecar independence, unauthorized-hook rejection, and content-minimal lifecycle audit records.

## 5. Privacy, Documentation, And Compatibility

- [x] 5.1 Redact raw query text and paths from hosted call logs, disable local relevance/query JSONL in hosted mode, and add regression tests for sensitive sentinels across success and error logs.
- [x] 5.2 Update architecture, deployment, capability, and operator documentation with the shared-control-plane/isolated-cell boundary, honest encryption ceiling, protocol/version rules, Paddle runtime non-dependency, and rollback model.
- [x] 5.3 Add a companion-control-plane contract document for Substrate covering account-to-cell mapping, internal entitlements, Paddle adapter ownership, public gateway, Home, backup delivery, and destructive deletion responsibilities.
- [x] 5.4 Verify local MCP/REST/CLI schemas and startup remain compatible, optional compute soft-fails, the scaffold leak guard stays clean, and no personal/product-specific tenant data enters `src/exomem/_scaffold/`.

## 6. Verification And Handoff

- [x] 6.1 Run focused mutation, hosted-cell, gateway, transfer, portability, auth, and browser/static-contract tests with embeddings disabled.
- [x] 6.2 Run `ruff check`, strict OpenSpec validation, the full lean pytest suite, and the latency gate; document intentional optional-dependency skips.
- [x] 6.3 Run a local two-cell lifecycle drill: provision, capture unique sentinels, route/read independently, quiesce/export, restore without derived sidecars, resume, seal one cell, and prove the other remains available.
- [x] 6.4 Record verification evidence, ensure the branch diff contains only scoped files, and leave the change ready for companion Substrate integration and OpenSpec verification.
