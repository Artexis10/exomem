## Why

Named-speaker diarization writes a `speakers:` frontmatter list (e.g. `[Alice, Speaker B]`) on each
diarized media sidecar, and the named transcript embeds the names inline. But names are only findable
as *content* — there's no structured way to ask "what did <person> say", which the original
diarization change (`add-named-speaker-diarization`) explicitly scoped as future work.

## What Changes

- Add a `speakers` filter to `find`: restrict results to pages whose `speakers:` frontmatter includes
  any of the named speakers (case-insensitive), AND'd with the query and other filters, OR'd within
  the list — mirroring the existing `tags`/`projects` filters. Threaded through every search path
  (keyword/vector/bm25/hybrid) via `_passes_filters`, plus a `Page.speakers` accessor.
- Exposed on the unified command surface (MCP/REST/CLI/OpenAPI) via the `find` command registry.

Pure-substrate intact: this is a frontmatter filter over already-measured data — no model, no judgment.

## Capabilities

### Modified Capabilities
- `speaker-diarization`: diarized media is now findable by speaker via a `find(speakers=[…])` filter,
  not just by name-as-content.

## Impact

- Code: `src/kb_mcp/find.py` (`Page.speakers`, `speakers` param threaded through `find` + helpers +
  `_passes_filters`), `src/kb_mcp/commands.py` (the `find` command param + docstring), the regenerated
  MCP schema-fidelity fixture, the scaffold `SKILL.md` find-knobs note.
- Default `None` ⇒ zero behaviour change when the filter is unset (search never hides a page by default).
