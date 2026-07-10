## Why

Exomem's governed memory substrate is now deeper than a basic note graph, but its strongest capabilities are not yet proven through one real product loop. Transport lifecycle hangs, path-only references, fragmented graph context, and the absence of schema evolution keep technically complete components from forming a dependable whole.

## What Changes

- Add black-box stdio MCP and authenticated HTTP end-to-end gates covering setup, adoption, source-backed memory, evidence, graph context, supersession, review, reconcile, and restart persistence.
- Diagnose and fix or constrain the FastMCP/Starlette lifecycle combination so transport tests fail with timeouts instead of hanging.
- Add persistent `exomem_id` identifiers, canonical `exomem://memory/<uuid>` references, a rebuildable reference index, and explicit ID backfill.
- Turn `connect_memory(operation="context")` into one bounded context response over semantic blocks, typed graph edges, provenance, evidence, and history.
- Add governed schema inference, validation, and diff through one registry-generated product command.
- Harden graph completeness, context-quality evaluation, dependency gates, linting, and targeted typing without introducing a canonical graph database or server-side reasoning model.

## Capabilities

### New Capabilities
- `product-e2e`: Black-box product-loop verification across installed CLI, stdio MCP, HTTP, persistence, and tiered model/media paths.
- `stable-memory-references`: Persistent governed object identity, canonical references, resolution, and explicit backfill.
- `memory-schema-evolution`: Corpus-derived schema proposals, validation, diff, and optional governed contract persistence.

### Modified Capabilities
- `command-surface`: Add schema governance, stable-reference inputs/outputs, context aliasing, and ID backfill while preserving registry parity.
- `context-packs`: Assemble one bounded context response with graph, provenance, evidence, and supersession data.
- `live-index-freshness`: Maintain and reconcile the stable-reference index and unresolved graph targets alongside existing derived indexes.

## Impact

The change affects the command registry, note/source/entity/evidence writers, move/delete/reconcile hooks, context and graph assembly, server lifecycle tests, generated MCP schemas and capability docs, CI, and the scaffold guidance. It adds an optional schema-contract tree and rebuildable reference sidecar while keeping Markdown and preserved artifacts canonical. Existing path inputs and current `exomem://vault` / `exomem://source` references remain compatible.
