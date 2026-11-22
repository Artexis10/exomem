# Proposal

## Why

Passage search ranks declared passages by dense similarity alone. A cell whose embedding model is weaker than a caller's own model then misses passages that share the query's words but not its phrasing. A paired evaluation on real questions on 2026-11-22 found such misses, and an offline prototype that fused lexical ranking with the dense ranking recovered most of them. Callers should not have to change to get that recall back.

## What Changes

- Passage search fuses a lexical ranking of declared passage chunks with the dense ranking by default.
- `mode: "vector"` keeps the exact dense-only ranking. Any other mode is refused with an error that names the two accepted values.
- Each returned score is the dense similarity at that rank, so a caller's score map keeps its meaning. A new `ranking` field says whether the reply was fused or dense-only.
- The embeddings sidecar keeps the lexical terms of its chunk rows, bound to the same rows and generation that the dense ranking reads.

## Capabilities

### Modified Capabilities

- `passage-evidence` ranks passages by fused lexical and dense evidence, and defines what a passage score means.

## Impact

Lexical candidates pass the same published-bytes verification, withholding and page-read bound as dense candidates. A vault that never runs passage search builds no lexical terms. The passage latency bars are unchanged.
