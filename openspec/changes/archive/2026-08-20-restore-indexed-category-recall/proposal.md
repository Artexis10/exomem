# Restore Indexed Category Recall

## Why

Cold `find` requests with structured category filters currently spend seconds walking and parsing every Markdown parent before candidate search. Unit recall can also return false-empty results when the rebuildable lexical sidecar is slightly stale, lacks FTS5, or encounters one transient SQLite busy/locked error that retires the store for the rest of the process. Category retrieval should be an indexed operation, not a corpus scan or a fragile optional shortcut.

## What Changes

- Compile safe category/kind predicates into indexed candidate queries, then hydrate and evaluate only the bounded candidate parents.
- Iterate already-eligible paths directly for empty-query keyword requests; retain the full-scan oracle only for filter predicates that cannot be represented safely by the index.
- Give lexical-sidecar calls explicit available, stale, transient-failure, fatal-failure, and unsupported states instead of overloading `None` or sticky process retirement.
- Maintain exact semantic category/kind metadata independently of FTS5, and return an explicit non-cacheable warming outcome when completeness cannot be proven.
- Repair a bounded missed-event delta in the foreground for large corpora; never trigger an unbounded foreground rebuild.
- Treat SQLite busy/locked failures as recoverable on the next request, reserve sticky retirement for proven corruption, and preserve correct recall when FTS5 is unavailable.
- Add structural scaling tests and category-filter latency lanes that prove work tracks indexed candidates rather than corpus size.

## Capabilities

### New Capabilities

- `category-retrieval-reliability`: Defines an FTS-independent semantic catalog, indexed category eligibility, bounded hydration, explicit incomplete outcomes, and atomic bounded delta repair.

### Modified Capabilities

- `find-recall-efficiency`: Structured category/kind filtering gains candidate-bounded cost and explicit latency gates.
- `live-index-freshness`: Missed watcher events can be healed by bounded lexical deltas without blocking on a full foreground rebuild.

## Impact

Expected implementation areas include `find.py`, the lexical store and semantic-unit index, freshness plumbing, timing diagnostics, and retrieval/latency tests. Search syntax and successful complete-hit semantics do not change; incomplete exact recall now returns a typed retryable error instead of a false empty. Rebuildable sidecars remain optional acceleration artifacts, but their failure modes become observable and correctness-preserving.
