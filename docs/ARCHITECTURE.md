# Exomem Architecture

Exomem is an owned-vault memory layer. Markdown files, binary evidence, and
their sidecars are the durable source of truth; indexes, embeddings, freshness
registries, and access logs are derived operating data.

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

Sidecars are rebuildable. This includes embeddings, CLIP vectors, media
transcripts/OCR, freshness/inbound registries, and relevance-pair snapshots.

Durable decisions, evidence, and source material belong in the vault. Derived
data should be safe to delete and regenerate, or explicitly documented when it
is not.

## Non-goals

Do not add a service/repository/database stack just to look layered. Exomem is
not a multi-tenant cloud product, a graph database product, or a general CMS.

Avoid:

- SQLAlchemy-style repository layers around markdown files.
- duplicated API/MCP/CLI business logic.
- graph database dependencies for the current vault scale.
- cloud sync/control-plane code in the core memory path.
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
