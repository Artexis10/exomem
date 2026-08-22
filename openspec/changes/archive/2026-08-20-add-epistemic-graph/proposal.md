## Why

Exomem already has hybrid search, wikilink neighborhoods, context packs, contradiction review, and source-governed notes, but its graph is still mostly implicit and shallow. Basic Memory's advantage is a first-class typed knowledge graph; this change closes that gap while keeping Exomem's stronger substrate: files remain the source of truth, graph data is derived, and deeper connections are surfaced for review rather than asserted by a server-side reasoning agent.

## What Changes

- Add a derived epistemic graph sidecar that indexes typed nodes and typed relationships from Markdown, frontmatter, wikilinks, source/evidence references, supersession fields, and semantic blocks.
- Add a broader semantic-block layer for claims, findings, decisions, assumptions, constraints, risks, failures, experiments, results, patterns, requirements, actions, entities, projects/cases, timeline events, and media segments.
- Add graph context and relation suggestion surfaces across the shared command registry so MCP, CLI, and REST expose the same contract.
- Extend context packs to optionally use typed graph neighborhoods when the graph sidecar is available, without changing `find` ordering or the default `pack=false` result.
- Extend live freshness/reconciliation so the graph sidecar can be incrementally maintained and rebuilt when drift is detected.
- Keep heavy/model-backed extraction optional, default-off, and soft-failing. Any model-backed path is treated as measurement that proposes candidate blocks or edges; it never mutates notes or becomes server-side reasoning.

## Capabilities

### New Capabilities
- `semantic-blocks`: Extract and represent rich, source-spanned knowledge units from ordinary Markdown without requiring a new mandatory authoring syntax.
- `epistemic-graph`: Maintain a derived typed graph over files, blocks, sources, evidence, entities, and relationships, plus read-only graph context and propose-only relation suggestions.

### Modified Capabilities
- `context-packs`: Allow packs to include typed graph neighborhoods and block-level relations when the graph sidecar is available, while preserving existing `find` behavior and soft-failing when unavailable.
- `live-index-freshness`: Extend freshness, watcher, and reconcile behavior to cover the graph sidecar without hiding external edits or requiring a full rebuild on every query.

## Impact

- Affected code: graph extraction/index modules, Markdown/frontmatter parsing helpers, context pack assembly, find/pack wiring, audit/reconcile, live watcher freshness registries, command registry, MCP/REST/CLI generated surfaces, and tests.
- APIs: new read-only graph context and relation-suggestion operations exposed through the shared registry; existing `find(pack=true)` gains graph fields only when requested/available.
- Dependencies: no required new heavy dependency. Optional model-backed relation/block suggestion paths must be gated behind explicit extras/env flags and soft-fail when unavailable.
- Storage: a rebuildable SQLite sidecar under the vault/KB runtime data area; Markdown files remain canonical and can recover the graph by reindexing.
