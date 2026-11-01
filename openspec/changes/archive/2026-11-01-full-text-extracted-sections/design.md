# Design

## Storage

512 KB is a page size, not a limit on the document. `preserve._MAX_EXTRACT_BYTES` stays the page budget, so no page is ever too big to index or read. `extraction_parts.split` cuts each page in its second half, before its last paragraph break, else before its last line break, else at a character boundary. So no middle part is shorter than half a page, and the cut takes time linear in the text. The whitespace of a break starts the next part, so part 1 reads back unchanged and the parts joined are the text, character for character. The folder is a dot folder, except beside a page whose name carries the raw-protection mark: that folder keeps the mark at the start of its name, so the protection floor covers it. An extraction larger than the 128 MiB that the job store carries keeps its first 128 MiB and ends with a visible truncation line.

The Source page records `extraction_parts` and `next_part`. A part page records `part_of`, `part`, `parts` and `next_part`. No page stores where a part starts. A reader measures offsets from the parts as they are, so an edit of the Source page cannot leave a stale table behind. The links are paths relative to the page that holds them, not wikilinks: a wikilink from a part counts as a reference to the Source and blocks moving it. A reader finds the Source of a part from the part's own path, so the field cannot go stale in a way that breaks reading.

Part pages name no binary, so no processing pass picks them up. They are written in the batch that writes the Source page. A re-extraction that is shorter removes the surplus parts right after that batch commits, because a batch has no delete. Reconcile sweeps what a crash leaves. It removes a part page only when an extraction wrote it and its Source lists fewer parts, and reports any other file. It writes missing parts again from originals that read without a model, and reports the others. A reprocess moves the old parts to the folder of the prior-extraction page. A missing old part does not stop the reprocess: the prior page shows a visible line in its place.

## Search

The part pages are indexed like any page. The scene-frame collapse folds them onto the Source, fed by the same `emitted_parent_path` column. A part with no admitted Source, or past the number of parts its Source lists, is dropped and never shown alone. The best-ranked part per Source is reported. The passage is the matched chunk, or `MAX_WORDS_PER_CHUNK` words around the first match, with about 600 characters of context on each side.

## Reading

A window is 32 KiB of text, inside the 20 to 50 KB an agent takes in at once. A window stays inside one part and ends at a paragraph or line break. A page that is not released in full gets today's projection, not a window. A window withholds what the read withholds, and carries `body_truncated: true` with no `content_hash`, so it cannot guard a write of the whole page.

## Governance

A part page is more of its Source's text. Membership evaluation reads a part's scope membership and audience from its Source page, found from the part's path, in one place. A governed projection catalog holds no part page as an item of its own, so the projected search does not find text that only a later part holds.

## Trash and moves

A part folder travels with its page. Trash moves it beside the trashed page, recovery brings it back, and a move carries it with or without the original. Only emptying Trash removes part pages. When the folder cannot follow, the page still moves and the result warns where the parts stayed.

## Rejected

- A graph that excludes parts: the graph's source bookkeeping covers every corpus page, and a filter at admission stopped it from converging. The map skips parts instead.
- A format version bump for export: the layout is unchanged and the new rule only makes part pages canonical.
