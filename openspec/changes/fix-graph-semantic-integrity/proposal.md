## Why

Exomem's canonical note-level relation syntax is currently omitted from corpus-contract inference and validation, while similarity-only relation suggestions overstate semantic direction by labelling nearby notes as `refines`. These two defects undermine the governed meaning the graph is intended to preserve and should be corrected before expanding schema or graph surface area.

## What Changes

- Make memory-contract inference, validation, and diff treat canonical `## Relations` edges as observed typed relations alongside block-level semantic relations.
- Make similarity-only and shared-source relation suggestions semantically neutral; they may propose related candidates but must not assert `refines` without directional evidence.
- Preserve explicit wikilink and frontmatter-derived suggestion behavior, proposal-only semantics, graph ordering, and all existing write contracts.
- Add regression coverage for canonical relation contracts and neutral similarity/shared-source candidates.

## Capabilities

### New Capabilities

- `graph-semantic-integrity`: Ensures schema observation and relation proposals preserve the meaning and provenance of canonical graph relations without laundering proximity into directional claims.

### Modified Capabilities

None.

## Impact

- Affected code: `memory_schema.py`, `epistemic_graph.py`, and focused schema/graph tests.
- APIs: existing `schema_memory` and `connect_memory(operation="suggest-relations")` response shapes remain compatible; only incorrect relation classification changes.
- Dependencies and runtime: no new dependency, model, background task, or sidecar migration.
