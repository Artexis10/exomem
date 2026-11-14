## Context

`capture_source` writes a text capture as one Source page: frontmatter with `source_identity` and `capture_input_digest`, then the text under `## Capture`.
The chunker splits the search body on blank lines, caps a chunk at 350 words and prepends the title.
The embedding sidecar stores `file_path`, `chunk_idx`, `chunk_text`, the vector and a modification time.
`collect_candidates` retrieves ranked chunks and keeps the best chunk for each file. Page fusion, metadata reads and excerpt hydration follow.
The structured passage that some hits carry comes from `## Extracted text`, so a text capture has none.

Two storage facts bound the design:

- Every vault runs file mode. Store mode is a flag for each vault and derives its rows from page bytes. A fact that lives only in a side file or a private table does not exist in store mode.
- A Source is append-only. Its bytes do not change after capture.

This change uses these terms:

- **Declared passage:** one piece of text that the caller supplies at capture, with one passage key.
- **Passage key:** the caller's own name for a declared passage, an opaque string. Exomem stores and returns it. Exomem never reads meaning from it.
- **Span:** the character offset and length of a chunk inside the Source body.
- **Passage result:** one ranked chunk of a declared passage.

## Goals / Non-Goals

**Goals:**

- A caller can map every passage result to exactly one of its own records, and can prove that the record's text is the text that Exomem ranked.
- Several passages of one Source can rank.
- The passage path meets its latency bar on a 2-CPU cell at a corpus of 40,000 chunks.
- Both storage modes answer the same, with no new stored structure.

**Non-Goals:**

- Passage results for Sources without declared passages. That needs a reindex of existing vaults and is a later decision.
- Lexical or graph signals in passage ranking. They enter later only on a measured gain.
- Passage text in results. A caller that wants the text reads the Source and takes the span.
- A correction of a Source that has declared passages, erasure of a Source, and read views for a service caller.

## Decisions

### D1. The caller declares passages at capture

`capture_source` accepts `passages`: an ordered list of objects with `key` and `text`. `passages` and `content` exclude each other.
Exomem renders the body as the passage texts joined by one blank line under `## Capture`, so every existing reader sees an ordinary text capture.

Bounds, each rejected before any write with an error that names the passage, the field and the limit:

- at most 2,000 passages in one capture;
- a key that is a string of 1 to 128 bytes as UTF-8, and is unique within the capture;
- passage text that is not empty, holds no blank line and no carriage return, and has no white space at its start or end.

The text rules exist so that the stored text equals the caller's text byte for byte.
Without them, capture would have to change the text, and every hash check by the caller would fail without a visible cause.
The cost of a refusal is one `strip` in the caller, and the error names it.

A passage above the chunk cap of 350 words splits at the cap. Each part is its own chunk, and all parts belong to the same passage.

The alternative was to keep `content` and infer passages from paragraphs. It leaves the boundary to a chunking rule that can change, so a caller could not rely on it.

### D2. The capture digest covers the passages

The original authored inputs include each passage's key and text, in order.
The same identity with the same passages reuses the Source. The same identity with different passages is a conflict, as it is for different text today.
A caller that must change a key captures a new revision.

### D3. Passage keys live in the Source page

The Source's frontmatter holds `passage_keys`, the ordered list of keys.
The body holds the passage texts. Together the page's bytes hold the whole declaration, so store mode carries it with no new column, table or side file.
No search field reads frontmatter as text, so no index holds a key.

A key is short on purpose. It names the caller's record; everything else about that record stays with the caller, where it can change without a new capture.

Rejected homes:

- a side file beside the Source, because store mode derives its rows from page bytes;
- the body, because a Source's body is searched in full, and every text reader would see the keys;
- a Records collection, because collections keep append-only versions and prohibit item deletion;
- a structured object for each passage, because it copies caller state that goes stale.

### D4. Spans and ordinals are derived from the page, not stored

For a Source with `passage_keys`, the chunker makes the chunks from the declared passages: one for each passage, split at the cap.
The same function gives each chunk its passage ordinal and its span in the body.
The passage path calls that function on the Source's current bytes when it builds a result.

The embedding sidecar does not change. Its `chunk_text` is the binding: a selected chunk is returned only when its stored text equals the text of the same chunk derived from the current page.
A chunk that fails this check is withheld and counted.

A Source whose number of `passage_keys` differs from the number of passages in its body is withheld in the same way.

The alternative was to store ordinal, span and a body hash in new sidecar columns. It adds a schema change, a second copy of what the page already says, and a risk for every reader of chunk rows.
A Source is append-only, so the derived table for a page is cached in memory against the page's identity and costs one derivation for each Source.

### D5. A passage result level on search

`search_memory` accepts `result_level: "passage"`. The path is:

1. Resolve eligibility as today. Filters, governance scope and relation scope produce the allowed paths.
2. Embed the query once.
3. Select the top chunks over the allowed Sources that have declared passages, with `EmbeddingIndex.select_chunks` and without the collapse to one chunk for each file.
4. Derive each selected chunk's passage and span from its page, and check the binding of D4.
5. Return the results in score order.

Each result carries: the Source reference, the `source_identity`, the passage key, the passage ordinal, the hash of the whole passage's text, the span of the matched chunk and the cosine score.
The reply also names the recall space, because a score compares only inside one space, and the number of withheld chunks.
`limit` applies as at other levels, up to 200.

The passage hash is SHA-256 of the passage text as UTF-8. Because of D1 it equals the hash of the text that the caller sent.

The path runs no lexical lane, no graph, no fusion, no rerank and no usage boost.
A request that combines the passage level with an option that changes ranking is refused with an error that names the option to remove. A silent ignore would let a caller believe that the option took effect.

The passage path lives in its own module. `find.py` gets only the dispatch to it.

### D6. Filters apply before ranking, and no new attribute is added

Eligibility already resolves before candidate collection, and the shortcuts for types, tags, source kinds and projects compile into the same plan.
A caller that separates readers captures a tag for each class of reader and filters on it.
A new Source attribute would need its own indexed column, registry entry and migration. The existing tags give the same result.

### D7. The latency bar is named before the measurement

`request_latency_contract.json` gains a reference `passage-evidence-reference` (at least 1,500 Sources and 40,000 declared-passage chunks, 2 CPUs, warm served encoder) and these bars for the passage path at the MCP boundary:

- one caller: p95 below 200 ms;
- four concurrent callers: p95 below 500 ms and no `MODEL_BUSY` refusal.

A Cloud cell scores vectors from sidecar rows on disk today. The measured page path spends about 230 ms at p50 in that stage.
If the passage path cannot meet the bar that way, it holds the active space's passage matrix in memory when the resource policy admits its size, and it reports which mode served the request.
The implementation measures both modes on the reference before it chooses.

## Risks / Trade-offs

- **Deriving spans at read time costs a page read for each Source in the results.** → At most `limit` Sources for each request, cached against the page's identity; the latency bar of D7 includes it.
- **A long list in frontmatter.** → At most 2,000 keys of 128 bytes; a transcript of 100 passages adds about 2 KiB. The reference of D7 measures a catalogue of such pages.
- **Dense-only ranking can miss an exact term.** → One measurement on a production-sized corpus showed no difference from another dense engine; a lexical signal enters only on a measured gain.
- **A resident matrix costs memory in each cell.** → About 137 MiB for 33,000 passages at 1,024 dimensions; admission goes through the resource policy, and the disk mode stays as the fallback.
- **`find.py` and `commands.py` have other open work.** → The passage path is a new module; the two shared files get a dispatch and a registration only.
- **Vocabulary in code (C4).** → A passage key is opaque. No code reads, lists or branches on its content.

## Migration Plan

1. Ship capture and the result level behind the new argument and the new level. No existing request, page or index row changes.
2. Measure the reference on both scoring modes. Record the run files and choose the mode.
3. Add the bars to the latency contract with the chosen mode's evidence.
4. Rollback: stop sending `passages` and `result_level: "passage"`. Captured Sources stay valid text captures.

## Open Questions

- Is a second, lexical signal worth its cost for callers whose questions name rare terms? Only a measurement on such questions can say.
