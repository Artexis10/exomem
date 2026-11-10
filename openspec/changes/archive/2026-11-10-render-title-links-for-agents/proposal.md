## Why

The vault stores a link to another page as its path, `[[Knowledge Base/Notes/Insights/backoff-ceiling]]`. An agent on the public MCP tool surface reads that path in `read_page`, although it names pages by title everywhere else. The agent then types `[[Backoff ceiling]]` into the `old_text` of `edit_note`, because that is the form it writes. The text is not in the stored body, so the edit fails with `STRING_NOT_FOUND`. The operator ruling is that titles are human-friendly and that paths and ids are not exposed.

## What Changes

The decision: **an agent on the public tool shape reads links as `[[Title]]`, and the server takes that form back. Disk keeps the canonical path form.**

- **Read.** `read_page` shows each link of a body and of a unit text as `[[Title]]`, with its anchor and alias kept. `content_hash` and the page `id` stay computed from the stored bytes.
- **Edit.** On the public tool shape, `edit_note` (`replace_text` and `edits[]`) counts `old_text` in the body as the agent reads it, and replaces the matching stored bytes. Only when that count is 0 does it count the stored bytes, so a snippet in the stored form still applies, except an occurrence that covers only part of a link shown by title. A match that starts or ends inside a shown link is refused, with `STRING_NOT_FOUND` and a hint that the text must include whole links, or with `AMBIGUOUS_MATCH` when other occurrences are outside links. A link that `new_text` repeats unchanged keeps its stored bytes. Every other surface counts the stored bytes only.
- **Observation writes.** `add_observation` and `edit_observation` store a `[[Title]]` in a unit as the path form, as note body writes already do, and return the unresolved-link warnings.
- **Per-link rule.** A link is shown by title only when its target is visible to the caller, its title is a valid link text, and the title written back resolves to the same page. A withheld target, a duplicate title and a title that is another page's name keep the stored form. A withheld target's title is never read.
- **Unchanged.** Disk bytes, the migration state, REST and CLI reply shapes, and the tool schemas.

Residual: search excerpts, context packs, `prose_for_caller` carriers, `read_page` link summaries and unit-reference reads still show the stored form.

## Impact

- Affected specs: `get-payload-shape` and `semantic-write-contract`.
- Affected code: `vault` (the title kept in the link resolver, `render_title_links`), `edit` and `multi_edit` (the match against the shown body), `commands` (the public read tail), `observe_memory`.
- Moves the deferral "`[[Title]]` rendering of stored bodies" of `replace-agent-tool-surface` here.
