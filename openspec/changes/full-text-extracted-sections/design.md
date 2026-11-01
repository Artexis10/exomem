# Design

## Storage

512 KB is a page size, not a limit on the document. `preserve._MAX_EXTRACT_BYTES` stays the page budget, so no page is ever too big to index or read. `extraction_parts.split` cuts text before a paragraph break, else before a line break, else at a character boundary. The whitespace of a break starts the next part, so part 1 reads back unchanged and the parts joined are the text, character for character.

The Source page records `extraction_parts`, `extraction_chars`, `part_offsets` and `next_part`. A part page records `part_of`, `part`, `parts` and `next_part`. The table lives on the Source page, so a reader locates any offset without opening a part. The links are paths relative to the page that holds them, not wikilinks: a wikilink from a part counts as a reference to the Source and blocks moving it. A reader finds the Source of a part from the part's own path, so the field cannot go stale in a way that breaks reading.

Part pages name no binary, so no processing pass picks them up. They are written in the batch that writes the Source page. A re-extraction that is shorter removes the surplus parts right after that batch commits, because a batch has no delete. Reconcile sweeps what a crash leaves. A reprocess moves the old parts to the folder of the prior-extraction page.

## Search

The part pages are indexed like any page. The scene-frame collapse folds them onto the Source, fed by the same `emitted_parent_path` column. A part with no admitted Source is dropped and never shown alone. The best-ranked part per Source is reported. The passage is the matched chunk, or `MAX_WORDS_PER_CHUNK` words around the first match, with about 600 characters of context on each side.

## Reading

A window is 32 KiB of text, inside the 20 to 50 KB an agent takes in at once. A window stays inside one part and ends at a paragraph or line break. A page that is not released in full gets today's projection, not a window.

## Rejected

- A graph that excludes parts: the graph's source bookkeeping covers every corpus page, and a filter at admission stopped it from converging. The map skips parts instead.
- A format version bump for export: the layout is unchanged and the new rule only makes part pages canonical.
