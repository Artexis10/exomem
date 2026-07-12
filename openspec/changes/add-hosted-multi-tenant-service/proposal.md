## Why

Exomem now has confirmed demand from non-technical users who want its governed agent memory without operating a vault server, OAuth application, tunnel, or connector infrastructure. The next product step is a hosted-but-portable service that preserves Exomem's owned-Markdown model while making the first useful recall achievable from an invite in minutes.

## What Changes

- Keep the open-source runtime single-vault and make that boundary the isolation unit. A hosted tenant is one existing Exomem data-plane cell with its own process/container, Markdown/media vault, derived sidecars, runtime state, logs, and secrets; canonical knowledge never becomes shared control-plane database rows.
- Add process-safe per-vault mutation serialization across MCP, REST, CLI, uploads, and background sidecar writers so concurrent hosted requests cannot lose filenames, index entries, log entries, or evidence writes.
- Add an explicit hosted-cell mode and readiness contract that fails closed on unsafe shared configuration, skips local `.env` overrides, separates runtime directories, disables sensitive query capture, and keeps optional media workers default-off until they can share the mutation boundary.
- Define the tenant-aware gateway-to-cell contract: authenticated account identity resolves exactly one private cell, entitlement is checked before forwarding, command schemas remain registry-derived, idempotency scope is preserved, and no public request can select a tenant by body, path, query, or header.
- Add complete vault snapshot/export and restore preparation over canonical Markdown and media while excluding secrets, logs, temporary files, and rebuildable sidecars. Account deletion remains an orchestrated control-plane workflow that stops routing before destroying cell storage and keys.
- Add adversarial isolation, concurrency, log-redaction, retry, export/restore, and cell-readiness tests suitable for the first hosted alpha.
- Keep embeddings and media processing tier-controlled, default-off when a tenant has no matching entitlement, resource-bounded, and soft-failing to lexical search and durable capture when optional workers are unavailable. These models remain deterministic measurement/transduction under Exomem's pure-substrate constraint; no hosted reasoning model is introduced.
- Publish the runtime contracts consumed by a companion Substrate change that owns invite/session auth, Exomem Home, friendly capture, Paddle-first internal entitlements, provisioning orchestration, and the full invite-to-value journey.

## Capabilities

### New Capabilities

- `hosted-mutation-safety`: Process-safe serialization and retry semantics for every mutation path that can touch one tenant vault.
- `hosted-tenant-cell`: Safe hosted-cell configuration, private readiness, runtime isolation, provisioning output, and privacy-preserving observability.
- `hosted-gateway-contract`: Registry-derived forwarding, immutable identity-to-cell routing, tenant-bound transfer/idempotency context, and fail-closed isolation behavior.
- `hosted-vault-portability`: Quiesced canonical-data snapshot/export, restore preparation, derived-sidecar exclusion, and lifecycle hooks for control-plane deletion.

### Modified Capabilities

None. Hosted orchestration is additive and consumes the existing single-vault command and Review Studio contracts without weakening their local-first behavior.

## Impact

- Adds mutation locking, hosted-cell configuration/readiness, gateway-facing contracts, snapshot helpers, CLI/operator wiring, and hosted security tests while leaving ordinary `exomem` CLI/MCP/REST startup unchanged.
- Reuses the existing scaffold, command registry, REST facade, transfer helpers, and single-vault runtime as the tenant data plane.
- Requires a companion Substrate OpenSpec change for account/session state, Exomem Home, friendly capture, Paddle checkout/webhooks/portal, internal entitlements, and provisioning orchestration. Paddle MCP remains an operator/development tool, never a runtime dependency of this package.
- Requires deployment-level process/container isolation, encrypted tenant storage, backup object storage, secret management, and ingress configuration outside the core package; this change defines and tests the cell side of those contracts.
- Changes the architecture documentation's former multi-tenant non-goal and adds threat-model, cross-tenant leakage, lifecycle, browser-flow, and recovery verification.
