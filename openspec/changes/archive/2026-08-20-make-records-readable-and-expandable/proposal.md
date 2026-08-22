## Why

Markdown-item Records preserve nested structured values losslessly in YAML frontmatter, but ordinary Obsidian property rendering makes object arrays such as laboratory measurements effectively unreadable. The query surface compounds that friction: `expand_children=true` works for Markdown-log child rows but silently returns zero rows for equivalent array-of-object fields in Markdown-item Records.

## What Changes

- Keep each Markdown-item Record as one human-owned `.md` file whose YAML frontmatter is the sole canonical structured authority; do not introduce parallel standalone YAML Record files or one file per child value.
- Add an optional, domain-neutral `record_presentation` manifest contract for a deterministic managed Markdown body projection: compact summary, declared child tables, notes, and collapsible provenance. The namespaced key leaves unrelated legacy extension fields untouched. The projection is readable in Obsidian, rebuildable from canonical frontmatter, explicitly non-authoritative, and preserves authored prose outside its managed block.
- Regenerate the managed projection atomically during append, value update, or explicit guarded refresh, and report stale or manually altered projections during inspection without treating them as canonical values. Lifecycle transitions remain item-content-free.
- Add an explicit `expand_child` query selector for declared array-of-object fields. Preserve `expand_children=true` only when exactly one eligible child field is unambiguous; otherwise refuse with an actionable error instead of returning zero rows.
- Keep expanded child rows governed, bounded, snapshot-stable, paginated, and correlated to their parent Record.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `structured-collections`: Add the optional managed presentation contract for Markdown-item bodies while preserving frontmatter as canonical data and authored prose as user-owned content.
- `records`: Define readable observed-value presentation, exact child expansion, governance, and bounded query behavior for Records.
- `command-surface`: Add the explicit `expand_child` query argument without adding another public tool.

## Impact

The change affects manifest parsing/schema projection, Markdown-item rendering and guarded mutation, collection inspection and lifecycle validation, Records query/adapters/governance, the `record_memory` MCP/CLI/REST schema, generated surface artifacts, and installed-wheel Records acceptance. Existing manifests and Record files without the newly reserved `record_presentation` key remain valid; managed presentation is opt-in and no storage strategy migrates. It builds on the completed but not yet archived `make-records-first-class-and-recoverable` nine-action Records contract and SHALL be archived only after that prerequisite has been synced.
