## Why

A managed service loads the corpus embedding matrix in the background after a release cutover or an idle reap, and a recall never waits for it. The context pack's proximity tension still read the matrix inline, so a deep `ask_memory` or a `read_memory` context request waited on the load that its own find had just started. With the wait removed, the pack has no proximity pairs while the matrix loads. An empty `tension` list must then say why, or a reader takes "still loading" for "no tension".

## What Changes

- In an activated managed runtime, pack assembly reads proximity pairs only from a resident matrix. While the one background load runs, the pack computes no proximity pair, reports `embeddings_available: false`, and adds a truncation line saying the embedding matrix is still loading.
- Asserted pairs and `superseded` edges are still surfaced, as with embeddings disabled.
- An offline caller with no activated runtime keeps the inline load.

## Capabilities

### Modified Capabilities

- `context-packs`: a pack names a still-loading embedding matrix as the reason for having no proximity pairs.

## Impact

- Code: `src/exomem/corpus_aware.py` (`pairwise_best_cosine_from_sidecar`) and `src/exomem/context_pack.py` (`_tension_pairs`, `assemble_pack`).
- Behaviour: the first deep recall or context request after a cold matrix returns at once, with no proximity pairs and a truncation line. Later requests get proximity pairs from the loaded matrix.
