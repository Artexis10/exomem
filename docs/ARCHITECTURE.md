# Exomem Architecture

Exomem is an owned-vault memory layer. Markdown files, binary evidence, governed
history, and explicitly portable sidecars are the durable source of truth;
indexes, embeddings, runtime registries, and access logs are operating data.

The architecture should stay explicit without becoming enterprise-shaped. The
command registry is the contract. Transport adapters, CLI dispatch, and tests
should route through that registry instead of wrapping separate business logic.

## Composition root

`src/exomem/server.py` is the FastMCP composition root. It wires runtime startup,
auth, public assets, transfer routes, the REST facade, and MCP tool registration.
It should not own operation behavior.

Server support modules:

- `server_runtime.py` - environment promotion, vault/schema resolution, warmup,
  model reaper policy, media worker startup, and file watcher startup.
- `server_auth.py` - GitHub OAuth verifier and OAuth proxy construction.
- `server_assets.py` - favicon and OAuth metadata routes.
- `server_transfer.py` - `/upload` and `/download` routes plus transfer auth.
- `server_rest.py` - personal `/api/*` facade generated from the command
  registry.

## Command boundary

`src/exomem/commands.py` is the source of truth for operation metadata:

- command name
- parameters and coercion metadata
- description exposed to MCP/REST/CLI
- guarded write fields
- Tier 2 exposure policy
- MCP annotations

Operation leaves should implement behavior once. MCP, REST, and CLI paths should
bind those leaves through the registry rather than duplicating validation or
surface-specific wrappers.

Hand-registered server tools are exceptions only when the operation is bound to
server-local runtime state rather than the vault command surface, such as
`mint_upload_token` and `mint_download_token`.

## Vault boundary

The vault is the durable system boundary. Modules that resolve paths, parse
frontmatter, update indexes, preserve evidence, or reconcile drift must treat
vault-relative paths and append-only trees as explicit invariants.

The main rule: do not let transport code perform filesystem policy. It should
pass requests into vault-aware helpers and return their structured result or
error envelope.

## Retrieval boundary

Retrieval behavior belongs in retrieval modules, not in server wiring:

- `find.py` owns query execution and result shaping.
- `ranking_config.py` owns ranker knobs and adopted-config loading.
- embeddings, BM25, graph/freshness registries, usage activation, and media
  sidecars are implementation lanes feeding `find`.

Ranking changes should be measurable. Keep defaults byte-compatible unless a
retrieval evaluation or focused test intentionally changes behavior.

## Derived data

Sidecars are classified explicitly rather than assumed equivalent. Embedding,
CLIP, lexical, graph, freshness, generated-frame, job, and runtime SQLite state
is rebuildable. User-visible media Markdown and `.review-state.json` are portable
because they preserve authored/extracted context or durable review decisions.
The versioned portability registry is the authority for new sidecars.

Durable decisions, evidence, and source material belong in the vault. Derived
data should be safe to delete and regenerate, or explicitly documented when it
is not.

## Hosted boundary

The open-source runtime remains single-vault. Hosted service turns that existing
boundary into the tenant isolation unit: one private process/container, one
vault mount, one state root, one log root, and unique service credentials per
opaque cell ID. Cells may share immutable images and model caches, but never
writable vault, sidecar, runtime, log, or secret state. A cell cannot see or
select another cell's filesystem.

The shared product and control plane belongs in Substrate. It owns public
accounts and sessions, the immutable account-to-cell mapping, the public gateway
and Home UI, internal entitlements and Paddle integration, provisioning and
release rollout, encrypted backup storage, and destructive deletion. Exomem owns
the private registry-derived command/transfer contract, cell readiness and
mutation safety, provider-neutral feature enforcement, and canonical
export/restore helpers. Public requests never carry a trusted tenant selector or
vault path; the gateway derives one destination from authenticated server-side
state.

This is owner-scoped hosted storage, not zero-knowledge or end-to-end encrypted
compute. Deployment encrypts tenant volumes, backups, and secrets at rest, but a
cell must read plaintext in memory to search and index it. Hosted logs therefore
exclude content, queries, paths, credentials, and billing identifiers, and human
volume access should be exceptional and audited.

Gateway/cell compatibility is an explicit versioned protocol. Additive optional
fields may roll out compatibly; removing a command, parameter, or stable error
requires a coordinated rollout. The control plane pins a cell release per tenant
and rolls back by stopping routing, draining the cell, and starting the previous
protocol-compatible image against the same canonical vault. Rollback never
rewrites canonical content; derived stores may be discarded and rebuilt.

See [hosted-operations.md](hosted-operations.md) for the cell runbook and
[substrate-control-plane-contract.md](substrate-control-plane-contract.md) for
the companion ownership contract.

## Non-goals

Do not add a service/repository/database stack just to look layered. Exomem core
is not the shared account, billing, scheduling, or product control plane; it is
also not a graph database product or a general CMS.

Avoid:

- SQLAlchemy-style repository layers around markdown files.
- duplicated API/MCP/CLI business logic.
- graph database dependencies for the current vault scale.
- account, Paddle, public-session, or scheduler code in the core memory path.
- transport-specific validation that bypasses the command registry.

## Refactor rules

Preserve these invariants during architecture cleanup:

- MCP tool schemas and docstrings are compatibility surface.
- `commands.py` remains the registry for shared command behavior.
- `server.py` remains a thin composition root.
- filesystem safety stays in vault/preserve/reconcile helpers, not route code.
- retrieval changes require focused retrieval tests or evaluation output.
- extracted modules should start as behavior-preserving moves before deeper
  redesign.

The comparison with Basic Memory suggests borrowing discipline, not mass. The
right target is clearer boundaries around Exomem's existing depth-focused
system.
