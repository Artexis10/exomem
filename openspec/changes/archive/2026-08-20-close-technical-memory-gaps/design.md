## Context

Exomem already has a single command registry, governed writers, semantic blocks, a derived epistemic graph, context packs, audit/reconcile, and broad transport tests. The missing system property is composition: the current product benchmark exercises lean CLI subprocesses while disabling the model, watcher, and media paths, and the current ASGI test path can hang inside the FastMCP/Starlette lifespan. Existing `exomem://` values are labels over paths rather than resolvable identities, graph context is split across several operations, and schema validation covers Exomem's fixed source contract rather than user knowledge patterns.

## Goals / Non-Goals

**Goals:**

- Prove the governed memory lifecycle through real stdio MCP and HTTP boundaries.
- Give governed artifacts persistent identity without replacing path compatibility.
- Assemble bounded, provenance-rich context through the existing product surface.
- Infer, persist, validate, and diff optional user-owned schema contracts.
- Keep every new operation registry-generated and every derived index rebuildable.

**Non-Goals:**

- No cloud, teams, web UI, canvas clone, importer breadth, or documentation-parity project.
- No canonical graph database, mandatory schema, or rewrite of existing vault notes.
- No server-side reasoning LLM, automatic relation acceptance, or automatic supersession.
- No requirement that heavy embedding/media tests run on every pull request.

## Decisions

### Real clients define end-to-end success

The lean E2E starts from a built wheel, invokes setup through the installed CLI, then uses a FastMCP client over a real stdio subprocess. A separate HTTP smoke starts the actual app, performs authenticated MCP/REST calls, and proves clean shutdown. In-process leaf and `TestClient` tests remain useful but cannot satisfy the product E2E contract alone. Every transport boundary has a hard timeout.

Heavy deterministic models remain tiered: embeddings/reranking run in the existing model job, while OCR/ASR/CLIP/video fixtures run scheduled or opt-in. They are measurement under the pure-substrate rule and soft-fail outside their configured jobs.

### Persistent IDs live in governed frontmatter

New governed Markdown pages and evidence sidecars receive `exomem_id` UUIDs. Canonical references use `exomem://memory/<uuid>`. A SQLite reference sidecar maps IDs to current paths and is rebuilt from Markdown; paths remain authoritative fallback inputs. Existing pages are changed only by explicit `maintain_memory(mode="backfill-ids", dry_run=false)`.

Content- or path-derived IDs were rejected because edits and moves would break identity. A central database identity was rejected because Markdown must remain canonical.

### Context is one product operation, not a new graph product

`connect_memory(operation="context")` becomes the canonical context assembly route; `graph-context` remains an alias. It resolves query seeds through recall and path/reference seeds through the reference index, then returns bounded semantic blocks, typed edges, source/evidence provenance, supersession history, and truncation. Deterministic assembly does not summarize or judge the material.

Unresolved graph targets become explicit placeholder nodes so observed edges are not silently discarded.

### Schema contracts are optional governed files

One `schema_memory` command exposes `infer`, `validate`, and `diff` across MCP, REST, and CLI. Contracts live under `Knowledge Base/_Schema/contracts/`. Inference reports corpus frequencies and proposes required fields/blocks only at 100% presence over at least five pages. Saving is explicit; overwrite requires an expected hash. Validation is read-only, and strict mode changes exit status only for CI callers.

Schemas describe frontmatter, semantic blocks, and relation vocabulary while allowing unknown content by default. They guide and audit Markdown rather than block normal writes.

### Compatibility and architecture stay conservative

Every writer calls one shared identity helper, and every surface uses registry metadata. Existing path inputs, response paths, and current context-reference schemes remain supported. `commands.py` may be decomposed only through behavior-preserving registry composition with schema-fidelity coverage.

For the transport hang, first reproduce against an unmodified FastMCP app. Fix Exomem lifecycle ownership if the reproduction is Exomem-specific; otherwise constrain the dependency range to the verified compatible set and retain an upgrade canary.

## Risks / Trade-offs

- [Risk] ID backfill creates noisy vault diffs. -> Mitigation: dry-run default, explicit write flag, atomic batches, and no automatic startup migration.
- [Risk] Schema inference overfits a small corpus. -> Mitigation: five-page minimum, 100% threshold for required elements, frequency evidence, and proposal-first persistence.
- [Risk] Context responses become token-heavy. -> Mitigation: shallow defaults, independent node/edge/block caps, compact excerpts, and explicit truncation.
- [Risk] Reference and graph sidecars drift after external edits. -> Mitigation: watcher hooks, writer hooks, audit findings, reconcile repair, and full rebuild fallback.
- [Risk] Upstream transport upgrades reintroduce hangs. -> Mitigation: hard test timeouts, a minimal lifecycle canary, and tested dependency ranges.

## Migration Plan

1. Fix transport lifecycle and land lean black-box E2E before changing persisted data.
2. Add identity generation for new writes and the rebuildable resolver with no backfill.
3. Add explicit dry-run/write backfill and reference-aware inputs/outputs.
4. Add unified context and graph completeness changes.
5. Add schema contracts and registry surfaces.
6. Enable required lean/quality gates; keep heavy jobs tiered.

Rollback disables new command modes and ignores/deletes derived sidecars. `exomem_id` fields remain harmless user-owned frontmatter; path inputs continue to work.

## Open Questions

None. Product scope, E2E tiering, and persistent-ID policy were selected before implementation.
