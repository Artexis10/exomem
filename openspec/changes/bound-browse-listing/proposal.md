## Why

`browse_memory` in `list` mode returns every entry of a folder. Its cost grows with the folder: it reads every file's bytes to report a frontmatter type, then both egress passes decide every entry. On a long-running service, listings of one large folder took 44-161 s on 2026-10-18 and 2026-10-19. Concurrent recall calls slowed to 7-12 s while they ran, and one listing held a release cutover's drain. A client that receives thousands of entries cannot use most of them anyway.

## What Changes

- `browse_memory` `list` returns at most `limit` entries, in the existing order: directories first, then paths, case-insensitively.
- A response that stops early carries `truncated: true` and an opaque `next_cursor`. The caller passes it back as `cursor` to continue the listing.
- The leaf reads frontmatter and bytes only for the entries it returns. It enumerates names for the rest.
- A page holds only entries the caller may see. The response carries no total count.
- `overview` mode returns what it did, and refuses `limit` and `cursor` with `INVALID_MODE`.

## Capabilities

### New Capabilities

- `browse-listing`: bounded, resumable folder listings for `browse_memory` `list`.

## Impact

- Code: `src/exomem/commands.py` (`op_browse_memory`, `op_list_directory`), `src/exomem/list_directory.py`, `src/exomem/reserved_paths.py` (a names-only tree listing).
- Tool surface: `browse_memory` gains `limit` and `cursor`. The pinned tool-surface digest moves, and the generated contracts and fixtures are regenerated together. ChatGPT clients see the new parameters after a plugin refresh. Until then they receive the bounded first page.
- Behaviour: a listing of more than `limit` entries is no longer complete in one call.
