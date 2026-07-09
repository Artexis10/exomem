## Why

`find` latency and result-token cost are now hard to reason about: callers see per-hit ranker
signals, but not where time was spent, and the default hit shape sends excerpts even when an
agent only needs a compact recall surface. Server-authored writes also echo through the live
watcher and trigger duplicate re-embedding, wasting work during write-heavy sessions.

## What Changes

- Add opt-in per-lane timing/observability for `find`, covering the major retrieval stages so
  latency regressions can be measured before changing retrieval architecture.
- Add an opt-in compact recall surface for `find` that returns token-cheap result summaries, with
  a detail mode for callers that still need excerpts/signals.
- Add a small in-process hot cache for repeated `find` calls, keyed by the full recall request and
  invalidated by vault/index freshness so stale results are not served after edits or reindexing.
- Suppress watcher echo for files written by Exomem itself while preserving live reindexing for
  Obsidian/mobile/manual filesystem edits.
- Defer LSH and broader retrieval architecture changes until timing data identifies a real need.

No server-side reasoning model is added. The new timing/cache behavior is deterministic
measurement over the existing owned vault, BM25/vector/graph/CLIP lanes, and sidecar freshness.

## Capabilities

### New Capabilities

- `find-recall-efficiency`: `find` timing visibility, compact/detail result surfaces, hot-query
  cache behavior, and unchanged default results.
- `live-index-freshness`: live watcher behavior for externally edited files and suppression of
  self-write reindex echoes after Exomem-authored writes.

### Modified Capabilities

- None.

## Impact

- Code: `src/kb_mcp/find.py`, `src/kb_mcp/commands.py`, `src/kb_mcp/query_log.py`,
  `src/kb_mcp/file_watcher.py`, `src/kb_mcp/vault.py`, and writer paths that already use
  `batch_atomic_write`.
- Surfaces: unified `find` command registry, MCP schema, REST facade, CLI, OpenAPI output, and the
  committed MCP schema-fidelity fixture.
- Tests: timing visibility, compact result shape, unchanged default `find` behavior, cache
  correctness/invalidation, watcher self-write suppression, and continued out-of-band reindexing.
- Dependencies: none expected. Existing optional embedding/CLIP/rerank lanes continue to soft-fail
  as today; timing records skipped/failed lanes instead of making optional models mandatory.
