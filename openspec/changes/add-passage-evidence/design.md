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

### D3. Passage keys live in a block of the Source body

The Source body holds the ordered keys in a block of its own, before the `## Capture` section. The frontmatter holds only the number of declared passages.
The page's bytes hold the whole declaration, so store mode carries it with no new column, table or side file.
Every reader of the captured text reads what follows `## Capture` and sees passage texts only.

The search body leaves the block out, by one rule that applies only to a Source whose frontmatter holds a passage count. The existing rule for withheld OCR text has the same shape.
So no lexical or vector index holds a key, and only the passage reader parses the block.

A key is short on purpose. It names the caller's record; everything else about that record stays with the caller, where it can change without a new capture.

Rejected homes:

- the frontmatter. Read paths that never use the passages decode frontmatter for each candidate page. One decode of a Source page measured 0.04 ms with no keys, 0.13 to 0.19 ms with 100 keys and 6.7 ms with 2,000 keys of 128 bytes, so a request over a few hundred such pages would lose seconds;
- a side file beside the Source, because store mode derives its rows from page bytes;
- a Records collection, because collections keep append-only versions and prohibit item deletion;
- a structured object for each passage, because it copies caller state that goes stale.

### D4. Spans and ordinals are derived from the page, not stored

For a Source whose frontmatter holds a passage count, the chunker makes the chunks from the declared passages: one for each passage, split at the cap.
The same function gives each chunk its passage ordinal and its span in the body.
The passage path calls that function on the Source's current bytes when it builds a result.

The embedding sidecar does not change. Its `chunk_text` is the binding: a selected chunk is returned only when its stored text equals the text of the same chunk derived from the current page.
A chunk that fails this check is withheld and counted.

A Source whose passage count, number of keys and number of passages in its body are not all equal is withheld in the same way.

The alternative was to store ordinal, span and a body hash in new sidecar columns. It adds a schema change, a second copy of what the page already says, and a risk for every reader of chunk rows.
A Source is append-only, so the derived table for a page is cached in memory against the page's identity and costs one derivation for each Source.

### D5. A passage result level on search

`search_memory` accepts `result_level: "passage"`. The path is:

1. Require existing whole-namespace content authority, as durable identity capture does.
2. Borrow one proved published corpus without waiting for its publisher or building it; resolve filters and relations through maintained indexes.
3. Embed the query once.
4. Select the top chunks over allowed Sources with declared passages, without collapsing one file's chunks. Derive declaration membership once per published state.
5. Derive each selected chunk's passage and span from its page, and check the binding of D4.
6. Return the results in score order, without replacing withheld chunks.

Each result carries: the Source reference, the `source_identity`, the passage key, the passage ordinal, the hash of the whole passage's text, the span of the matched chunk and the cosine score.
The reply also names the recall space, because a score compares only inside one space, and the number of withheld chunks.
`limit` applies as at other levels, up to 200.

The passage hash is SHA-256 of the passage text as UTF-8. Because of D1 it equals the hash of the text that the caller sent.

The path runs no lexical lane, no graph, no fusion, no rerank and no usage boost.
A request that combines the passage level with an option that changes ranking is refused with an error that names the option to remove. A silent ignore would let a caller believe that the option took effect.

A public passage request reads at most `limit` distinct canonical Source pages from its original selection, including pages whose chunks fail D4.
Terminal filtering can reread selected pages; the latency proof includes those physical reads. No stage reads an unselected Source body or frontmatter.
The passage branch retains existing policy and store snapshot scopes and terminal release filtering, while skipping the generic pending-page hydration path.
Its corpus borrower verifies the existing semantic registries and configuration census, plus the event publication caption in file mode.
In store mode, the pinned store revision proves store-owned pages only; the borrowed view excludes unowned filesystem pages without event proof.
The borrower adds no publication or stored state.
Cold, contended or unproved state returns unavailable before encoding or Source reads. The outcome names the publisher or warm-up action needed.
It promises retry only when existing publication can progress; an offline caller without a publisher gets an actionable instruction.
Unrelated pending writes do not by themselves prevent use of a proved published generation; selected-byte verification still applies.
When the cell cannot admit model work at once, it refuses with a busy outcome and a retry hint, without waiting for admission.
Admitted requests use the existing serialized execution turn. Execution contention alone does not consume another admission or cause a busy refusal.

The passage path lives in its own module. `find.py` owns the dispatch and its bounded snapshot-wrapper branch.

Whole-namespace authority is a product restriction for this bounded result level, not a new governance rule.
It prevents per-candidate content admission from reading the whole corpus. A refusal costs a non-owner passage search; ordinary page search remains available.
The service owner keeps passage search. The alternative requires new published admission machinery and broader ownership changes.

Design source: the engineering primary-source roster's context-engineering and citation rows support minimal published evidence and separate byte binding from claim support.

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
The implementation first qualifies existing disk scoring on the reference.
Only a failed disk qualification justifies designing a resource-bounded resident mode and comparing both modes.
Amendment 1 precedes the run: the current service profile explicitly unloads matrices, so a resident comparison requires broader retention and publication changes.
Those changes have no demonstrated value if existing disk scoring already meets both bars.

The maintained MCP read runner already supplies the managed worker, isolated lease coordinator and terminal-response observer.
Its general fixture does not declare passages or issue concurrent calls; a passage-specific driver adds only those missing pieces.
The reference profile lives in `benchmarks/passage-evidence/profile.json` and reuses the pinned public prose of the recall reference.
It generates 1,500 Sources with 27 short passages each, verifies the stored declaration and chunk counts, and records corpus and runtime digests.
As in the recall reference, deterministic corpus vectors avoid spending preparation time on model throughput; every measured query uses the real served encoder.
This proves latency and availability, not retrieval quality. Gate A remains the separate quality evidence.
The same 40 queries run three times in each caller condition, after untimed warm-up.
The runner freezes query bytes and arguments before measurement and verifies the container's two-CPU and 3 GiB limits.
Each four-caller batch starts four independent MCP clients together, and its caller times include execution waiting.
Every terminal outcome stays in the sample set: 120 single-caller attempts and 480 four-caller attempts.
Qualification requires all 600 attempts, 30 verified hits and zero withheld chunks per response, and both strict p95 bars.
The report records encoder, publication, scoring-mode and concurrent-dispatch evidence.
If a resident comparison becomes necessary, it keeps the same service policy, corpus, encoder and MCP boundary.
That comparison alternates mode order and reports per-query differences with a case-level confidence interval.
Failed preparation or incomplete samples remain visible and cannot establish a passing mode.
The added driver is maintenance for this capability's fixture and concurrency protocol; it reuses the existing observer and worker rather than creating another instrument.

## Risks / Trade-offs

- **Deriving spans costs Source reads.** → At most `limit` selected Sources per public request, including withheld candidates; D7 includes all physical reads.
- **Non-owner callers cannot use passage search.** → The result level requires whole-namespace authority; ordinary page search keeps its existing governance behavior.
- **One more rule in the search body.** → It applies only to a Source with a passage count in its frontmatter, and a test proves that a key gives no search signal in both storage modes.
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
