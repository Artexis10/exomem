## Why

An agent that wants to edit a page it just created must hold the page's vault path, and the path is the only name every page tool accepts. Agents guess paths, mistype them, and carry them through replies that name each page by a long folder string. The path also tells the agent nothing about the page: a title and a version do.

A title is what the agent and the user already say. Today 14 page parameters take only a path or a ref, so a call that names a page by its title fails with a not-found refusal, and the retry costs a search.

## What Changes

The decision: **every MCP parameter that takes a page path also takes the page's title or a short id, and compact replies name pages by title and id instead of by path.**

- A new resolver, `page_names.resolve_page`, resolves a page in this order: an `exomem://` ref, an existing vault or KB-relative path, a short id, then a title. A value that names nothing is passed on unchanged, so every not-found and create-missing path behaves as before.
- A title resolves through the writer's own resolver, so a page created a second ago resolves. A title that several pages the caller may see share refuses with `AMBIGUOUS_REFERENCE` and at most five candidates (`title`, `type`, `date`, `id`), newest first. The retry names the picked page by `id`.
- A short id has two parts: 7 base32 characters of the page's `exomem_id` and 4 base32 characters of its content hash. The first part names the page across edits and moves. The second part names the version the caller read.
- `expected_hash` accepts a short id. An operation that requires `expected_hash` takes the version from the id the page was named by. A stale id refuses with `STALE_EDIT` and the current id.
- Compact write replies of page writes carry `title` and `id` in place of `path`; search hits carry `id` in place of `path` unless the search asks for `detail=full`; a page read carries `id`. `response_detail=full` keeps `path` under `diagnostics`.
- One `title_key` (NFC, trimmed, lower-cased, no case folding) replaces four copies of a bare `.lower()`.
- No tool schema or description changes, so the tool-surface digest does not move. The CLI and REST facades keep their reply shapes.

## Impact

- Affected specs: `command-surface`, `mutation-terminal-contract`.
- Affected code: a new `page_names` module, the command wiring in `commands`, the MCP wrapper in `command_surface`, the refusal candidates in `agent_refusal`, the title key in `vault`, and the scaffold references that describe replies.
- A program that reads `path` from an MCP write reply or search hit reads `title` and `id`, or asks for `response_detail=full` (writes) or `detail=full` (search) and reads the path. `read_memory` still returns `path`.
- A page without an `exomem_id` has an id built from its path, which a move invalidates. Such an id refuses as not found after the move; the title still resolves.
- The unit-level guards, vocabulary handles and fingerprint inputs keep their current forms and move in a later change.
