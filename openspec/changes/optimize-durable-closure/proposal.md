## Why

A real multi-note, three-artifact closure workflow on 0.71.0 took roughly fifteen minutes. Its call ledger contains 35 calls and 671 seconds of server execution, including 204 seconds in media processing; normal recall also refused while graph/resolver projections were behind. Primitive acknowledgement latency does not measure whether an agent can finish its work.

## What Changes

- Add a reproducible durable-closure workflow benchmark and a content-free call-ledger decomposition. Separate measured server time, transport time, client gaps, acknowledgement, and final retrieval/convergence.
- Keep ordinary hybrid recall usable when optional graph expansion cannot obtain a current resolver, with explicit graph degradation and unchanged mandatory policy, visibility, and relation-query fences.
- Make media processing return after durable reconciliation/enqueue without draining unrelated derived work; support bounded selected-artifact batches.
- Reproduce and repair current graph/watcher convergence amplification using exact dirty-path custody, preserving canonical ownership and fast-ACK bounds.
- Audit mutation receipts and client guidance to avoid redundant rereads. Document composable dependency ordering and safe independent execution using existing tool contracts.
- Run an isolated persistent-service comparison with a pinned current Basic Memory release over the common Markdown write/edit/read/text-search subset. Report richer governance/evidence/media capabilities separately.

## Capabilities

### New Capabilities

- `durable-closure-performance`: Whole-workflow correctness and latency measurement, attribution, and comparison contract.

### Modified Capabilities

- `recall-read-path`: Optional graph lag must not invalidate otherwise admitted ordinary recall.
- `product-command-surface`: Bounded media batches and asynchronous processing terminal behavior.
- `live-index-freshness`: Coalesced exact-path derived convergence without watcher amplification.

## Impact

Retrieval candidate collection, media product command dispatch, graph/watcher derived consumers, public schemas, portable agent guidance, and standalone benchmark tooling. No new model or metered provider is introduced. Optional embeddings/OCR retain their existing opt-in/dependency-gated behavior; the deterministic benchmark declares model-free and real-extraction variants separately. Production rollout and merge remain separate operator actions.
