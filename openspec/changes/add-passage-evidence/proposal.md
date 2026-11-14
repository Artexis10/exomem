## Why

A service caller that keeps its own records, such as transcript segments with time ranges, needs Exomem to rank those records as evidence and to say which record matched.
Exomem cannot do that today:

- A Source made by text capture returns one excerpt for the whole page and no position in it.
- The vector lane ranks chunks and then keeps one chunk for each file, so a long Source yields one result.
- A hit names no caller record below the page (issue #1727).

A measurement on 2026-11-13 shows the cost of the page path for this use.
One Cloud cell with 2 CPUs searched 1,500 captured Sources (40,527 chunks) at p50 760 ms and served one search at a time.
The same embedding model, ranking 33,454 passages directly, took p50 42 ms on 2 CPUs.

## What Changes

- **Declared passages at capture.** `capture_source` accepts a list of caller passages in place of `content`. Each passage has its text and a passage key, the caller's own name for it.
- **One passage, one chunk.** Exomem indexes each declared passage as its own chunk. A passage above the chunk cap splits; its parts stay one passage.
- **Keys live in the Source page.** The page's frontmatter holds the ordered keys, so both storage modes carry them and no index holds them.
- **A passage result level.** `search_memory` with `result_level: "passage"` returns ranked passages across eligible Sources. Several passages of one Source can appear. Each result has the Source reference, the caller's source identity, the passage key and ordinal, a hash of the passage text, the span and the score.
- **A lean path.** Passage results come from dense chunk ranking. They skip page fusion, graph and rerank. Existing eligibility filters apply before ranking.
- **A request profile.** The passage path gets its own bar in the request latency contract, with limits named before the measurement.

Not in this change: answer generation, passage text in results, lexical fusion for passages, erasure of a Source (its own change), and read views for a service caller.

## Capabilities

### New Capabilities

- `passage-evidence`: ranked passage results with spans and caller keys for Sources captured with declared passages.

### Modified Capabilities

- `durable-source-capture`: a capture may declare passages with caller keys; they are part of the original authored inputs.

## Impact

- Code: `src/exomem/add.py`, `src/exomem/source_capture.py`, `src/exomem/embeddings.py`, a new passage module, `src/exomem/public_tools.py`, `src/exomem/request_latency_contract.json`. `src/exomem/find.py` and `src/exomem/commands.py` get a dispatch and a registration only.
- Stored data: a Source with declared passages has one more frontmatter field. The embedding index, the vault store and every existing page keep their structure, and no reindex is needed.
- Docs: `docs/source-capture.md`.
- Reuse: the existing vector store, chunk selection, eligibility filters, capture identity and replay. Custom work is the chunk rule for declared passages and the result level. The added maintenance cost is one more chunk rule and one more result shape.
