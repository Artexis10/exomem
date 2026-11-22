# Proposal

## Why

Passage search ranks declared passages by dense similarity alone. A cell whose embedding model is weaker than a caller's own model then misses passages that share the query's words but not its phrasing. A paired evaluation on real questions on 2026-11-22 found such misses, and an offline prototype that fused lexical ranking with the dense ranking recovered most of them. Callers need a way to ask for that recall now, and the default should change only after a held-out decision run shows that fused ranking does at least as well.

## What Changes

- Passage search keeps exact dense ranking by default. `mode: "hybrid"` fuses a lexical ranking of declared passage chunks with it. `mode: "vector"` names the default. Any other mode is refused with an error that names the two accepted values.
- Each returned score is the dense similarity at that rank, so a fused reply has the score distribution of a dense-only reply. A new `similarity` field holds each hit's own dense similarity, and a new `ranking` field says whether the reply was fused or dense-only.
- A `hybrid` reply reports whether lexical matching is ready, preparing or failed under `diagnostics`, not in its summary.
- The embeddings sidecar keeps the lexical terms of its chunk rows, bound to the same rows and generation that the dense ranking reads.

## Capabilities

### Modified Capabilities

- `passage-evidence` can rank passages by fused lexical and dense evidence on request, and defines what a passage score and similarity mean.

## Impact

Lexical candidates pass the same published-bytes verification, withholding and page-read bound as dense candidates. A vault that never runs a `hybrid` passage search builds no lexical terms. Default passage requests and their latency bars are unchanged.
