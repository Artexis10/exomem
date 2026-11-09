## Context

Page tools take a vault path, a KB-relative path or an `exomem://` ref through `_resolve_memory_identifier`. Replies return the path. The writer already holds a title index, the shared `WikilinkResolver`, which every governed write patches in its own fan-out and which body wikilinks already use. This change reads that index for page parameters and names pages by title and id in replies.

Design basis: the team's primary-source roster, row "The model keeps misusing a tool/MCP surface", which points to "Writing effective tools for agents". Its guidance applies directly: return meaningful identifiers rather than opaque ones, and let tools accept them. A title is the meaningful identifier; the short id is the compact one.

## Decisions

### One resolver, and the writer's index

`page_names.resolve_page(root, value, prefer=None)` is the single resolver. The title comes from `find.shared_resolver`, not from the lexical catalogue (which lags at acknowledgement) or the context catalogue (derived, can be switched off). A title match goes through `vault.wikilink_candidates` with the egress visibility filter, so a body link and a `path` argument resolve a title the same way.

### Disclosure comes first

The visibility filter runs before anything is counted or read. A withheld duplicate never makes a title ambiguous and never appears as a candidate. A title that only withheld pages hold resolves to nothing, so it refuses exactly like an unknown title. For `anchor`, anything but exactly one eligible page becomes the one existing anchor refusal. Each hidden duplicate costs one more visibility call; the same class of timing difference is already accepted for refs.

### The id is a format, not a vocabulary

An id is `<7 base32 of exomem_id>.<4 base32 of content hash>`. The check `^[a-z2-7]{7}\.[a-z2-7]{4}$` is a format check on a value this module mints, so it is sound as a regex. A page with no `exomem_id` takes a hash of its path as the first part; that part goes stale when the page moves, which is a named residual.

### Resolution order keeps every working call

A ref goes first, an existing path second. A path that exists is never a title, so every call that works today keeps its target. A title is last. No stem lookup is added: body links check stems before titles and keep that order.

### A tie narrows by what the caller can do

Several title matches narrow by what the operation needs. An edit prefers pages that are neither append-only nor navigation pages, so a compiled note and its Source of the same title do not block each other. A single match is never hidden. Anything still tied refuses.

### A guard compares in the leaf, after admission

`expected_hash` accepts the short id or the 64-character hash. The compare stays in the leaf writers, after the write is admitted, through `page_ids.guard_matches`: `edit.load_editable` (shared by `edit`, `multi_edit`, `set_take` and `observe_memory`) and `set_frontmatter_field`. An id names a version by its tail, and the path the caller gave names the page, so the compare reads no page identity and works for a KB-relative path. Nothing reads a page before the leaf admits the write: a read at the command seam would answer a caller who may not see the page, and a withheld page would differ from an absent one.

A stale id leaves the leaf as its usual refusal. `op_edit` and `op_observe_memory` then read the page, which the caller may now see, and refuse with `STALE_EDIT`, the current id and a retry. Every door model already requires `expected_hash` where the leaf does, so the server fills no guard. Optional guards stay optional.

### Replies are named after the egress release

`command_surface` names the pages in a released MCP reply, after the dispatcher's terminal filter. The filter binds a unit reference to its page by `path` and `after_hash`, so removing the path earlier would break that binding. Naming after it also leaves the CLI and REST replies as they are. A command opts in through `Command.names_pages`, which is true for the page-subject writes and for search. A file tool keeps `path`, because it takes no title or id.

A write reply takes the version part of its id from its own `after_hash`, so a replayed reply or a raced write does not name a newer version, and an edit guarded by that id is refused. A reply holds an `after_hash` for its primary page only; the other pages of a multi-page write and a source take the version current when the reply is named.

A search with `detail=full` keeps each hit's `path` beside its `id`. That level is for a harness that reads vault files, as `response_detail=full` is for a write; the default hit names the page by title and id only.

### A key change is reported

`title_key` folds Unicode form (NFC), trims and lower-cases. It does not case-fold: that merges distinct titles. Only a non-NFC title changes its graph key.

## Risks

- Three active changes restate the compact-reply requirement of `mutation-terminal-contract`. Archive this change after the other two, and refresh its block against the canonical text then.
- A short id names a version by 20 bits of its content hash. A stale guard then matches a different version with a chance of about one in a million per stale edit. The 64-character hash stays available where exactness matters.
- Title ambiguity is common in a large vault. The refusal lists the newest five and the retry names the pick.
- A cold resolver costs a full vault walk on the first title lookup of a process. The id and path routes do not pay it when the path exists.
- An agent that guesses a path now falls through to a title lookup before it reaches not-found; the answer is the same refusal.
