## Why

A managed recall no longer loads a cold corpus matrix on its request thread, but a save's write advisory still did. After a release cutover or an idle reap, the first save whose overlap advisory needed the matrix either waited for the running background load or read every stored vector itself while it held the index lock. On the personal service a cold load took 72-104 s.

## What Changes

- In an activated managed runtime, a write advisory's overlap sweep uses the corpus matrix only when it is resident and current, or when the bounded catch-up makes it current.
- Otherwise the sweep starts at most one background load and skips with reason `matrix_warming`, as it already skips while the model warms. The save commits without the advisory.
- The receipt-owned deferred advisory follows the same rule, so it never holds the single derived-drain thread or the index lock through a cold load.
- An offline caller with no activated runtime keeps the inline load.

## Capabilities

### Modified Capabilities

- `find-recall-efficiency`: a managed save's write advisory never loads the corpus matrix on its request thread.

## Impact

- Code: `src/exomem/embedding_index.py` (`search_many` takes the same `defer_cold_load` switch as `search`) and `src/exomem/corpus_aware.py`.
- Behaviour: a save in the warm window after a cutover or reap reports no overlap advisory; a later save gets one. A deferred advisory computed in that window publishes as failed, as it does while the model warms.
