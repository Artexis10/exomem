# Design — find speaker filter

## Approach

Mirror the existing `tags`/`projects` filters exactly: a `speakers: list[str] | None` param on
`find`, threaded through every ranker path to `_passes_filters`, which drops a page unless its
`Page.speakers` (the `speakers:` frontmatter list) intersects the requested speakers
(case-insensitive). A page with no `speakers:` never matches a speaker filter; an unset filter is a
no-op (default-allow, like `file_types`).

## Decisions

- **Frontmatter facet, not a new index.** The `speakers:` list already exists on diarized sidecars
  (written by `preserve.update_sidecar_extraction`); the filter reads it directly — no sidecar, no
  row-IDs, no migration (consistent with the "no queryable sidecar" rule).
- **AND with query / OR within the list** — consistent with `tags`/`projects`, so combinations behave
  predictably (`find(query="thyroid", speakers=["Alice"])`).
- **Named speakers only in practice.** The point is finding by person; anonymous `Speaker A` labels
  match only if explicitly requested (harmless, rarely useful).
- **Unified surface via the registry.** The `find` command gains the param once; MCP/REST/CLI/OpenAPI
  all inherit it. The byte-identical MCP schema-fidelity baseline was regenerated (only the `find`
  tool's `speakers` property is added, positioned after `tags`).
