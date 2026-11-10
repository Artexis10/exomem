## Context

The roster row used: "The model keeps misusing a tool/MCP surface". The agent copies spans of what it reads, so what it reads must be what the server accepts. Either the server shows the stored form, or it accepts the shown form. This change does both ends of the second option and keeps one stored form.

## Decisions

**D1. One stored form.** `normalize_body_wikilinks` keeps writing the path form. A body on disk never holds a title link that the vault resolved. No migration, and the indexers, the link graph and the REST and CLI replies read what they read today.

**D2. Round-trip invariant.** A link is shown as `[[Title]]` only when writing `[[Title]]` back through the write-side resolver resolves to the same page. The check calls the resolver in the same order that the write path uses, so a duplicate title and a title that equals another page's stem fail it and keep the stored form. Titles that contain a link-syntax character (`/ # | [ ]` or a newline) keep the stored form, since they cannot be written inside a link. The set is link syntax, not vocabulary.

**D3. Titles in memory.** The link resolver kept a lower-cased `title_key` per page. It now keeps the display title beside it, from the same read, in the same entries. A render makes in-memory lookups only, and it costs O(links). A read never builds or copies a resolver, since a copy costs O(pages) per read. It borrows the resident, current resolver as a read-only reference, because rendering only looks entries up. With none resident the links stay as stored, and the first recall or write that warms the resolver restores the titles.

**D4. Visibility.** A target that the caller may not see keeps its stored form, and its title is not read. Only a page released in full is shown by title: a lower release level returns a projection, not the page's own text. A raw page (Source, Evidence) keeps its recorded text.

**D5. Inversion.** `render_title_links` returns the shown text and one span per rewritten link: the stored range and the shown range. `old_text` is counted in the stored body first, as before, so a caller that sends stored text, such as REST and CLI, behaves as before. Only a count of 0 renders the body and counts again. A match maps back to stored offsets: an edge at a span boundary or outside every span maps by the offset difference, and an edge inside a span has no mapping and refuses. `replace_all`, `AMBIGUOUS_MATCH` and the validate-only preview count the same way. `new_text` goes through the existing normalization.

**D6. Placement of the read.** The public shape is told apart by `capabilities.current_tool_shape()`. The render wraps the read leaf `op_read_memory`, after `op_get` has compared and hashed the stored bytes, so the evidence version and the extracted-text windows are not affected. An internal reader that never shows the page (`compiled_history`) calls the unwrapped `read_memory_stored` and pays nothing.

**D7. Observation text.** `add_observation` and `edit_observation` pass the unit text through `normalize_body_wikilinks` with the writer's resolver, so a unit stores the same form as a body. Typed `relations` (`kind: target`) are not link syntax, and they resolve when the page is derived, so they stay as written.

## Risks

- A link inside a unit that the agent writes as a title, for a page that does not exist yet, stays as written, as in a body.
- A stored link whose target is a retired collection path keeps its stored form.
- Residual surfaces, in the proposal, show the stored form. They are read-only, and an agent does not copy their spans into `old_text`.
