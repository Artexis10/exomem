## Why

`find(scope="kb")` pays several redundant full-tree walks and recomputations on every call, not
because ranking needs them repeated but because each consumer computes its own freshness or
derived text independently. BM25's freshness check walks the KB tree to get `_current_max_mtime`
(`src/kb_mcp/bm25.py:174-192`); the wikilink resolver's freshness check walks the whole vault again
(`src/kb_mcp/find.py:1540-1561`); and auto-widen triggers a second `bm25.search(scope="vault")`
walk on every non-empty `scope="kb"` query — not only when the KB result set underfills
(`src/kb_mcp/find.py:605`). That is three-plus full-tree stat walks per hybrid `find` call today.

Per page, `_make_excerpt` lowercases the whole body on every call
(`src/kb_mcp/find.py:1759-1803`), the keyword lane calls it for every KB page on every hybrid query
(`src/kb_mcp/find.py:1457`), and the stem-match gates re-tokenize and re-stem the full title+body per
page per call (`src/kb_mcp/find.py:1564`, `src/kb_mcp/find.py:1136`). The three post-RRF multiplier
passes — `_apply_type_boost`, `_apply_status_demotion`, `_apply_temporal_boost`
(`src/kb_mcp/find.py:915-922`, definitions at `1219`, `1243`, `1339`) — each re-fetch pages and
re-sort independently instead of combining into one pass, and are each re-applied a second time to
`Hit.rerank_score` after the CrossEncoder runs (`src/kb_mcp/find.py:1029-1044`). `find(pack=true)`
calls `vault.find_inbound_wikilinks`, itself a full-vault read scan, once per packed page — five
full-vault scans for the default pack size (`src/kb_mcp/vault.py:471-536`,
`src/kb_mcp/context_pack.py:206-210`). And even though `server.py` preloads the embedding,
reranker, and CLIP models at startup (`src/kb_mcp/server.py:311-346`), the first hybrid query in a
fresh process still pays the lazy BM25 corpus build, resolver build, and page-parse cost that model
preload does not cover (`src/kb_mcp/bm25.py:100-139`).

None of this changes ranking — it is the same work done more than once per request, or done on the
first request in a process instead of at startup. This change removes the redundancy without
changing what `find` returns.

This change is sequenced after `improve-find-latency-token-cost` lands. It builds on that change's
freshness-invalidated hot-cache seam and `find.clear_cache()` test hook, and it deliberately leaves
that change's compact/full serialization, `Hit` shape, and timing-diagnostics surface untouched —
this change is about the retrieval work done underneath a single `find` call, not the shape of the
response or a second caching layer for a whole request. The `get` payload dedup identified during
this audit belongs to its own change, `dedupe-get-payload`, and is out of scope here.

## What Changes

- Introduce a per-request `FreshnessSnapshot` so BM25 KB freshness, BM25 vault freshness (needed by
  auto-widen), and wikilink-resolver freshness are each computed at most once per `find` call
  instead of once per consumer, and strengthen the freshness key from `(count, max_mtime)` to
  `(count, max_mtime_ns, digest-of-sorted-rel-paths)` so deletes, renames, and mtime-preserving
  replacements are no longer missed by a stale cache — a behavior fix, not just a speedup.
- Memoize each page's normalized/stemmed derived text (`body_norm`, `title_norm`, `stem_set`) on
  `ParsedPage`, invalidated for free by `FrontmatterCache`'s existing mtime-based replacement.
- Collapse the three sequential post-RRF multiplier passes (type boost, status demotion, temporal
  boost) into one combined-multiplier pass, in both the pre-rerank fused-score path and the
  post-rerank `rerank_score` path, plus a shared per-request page-lookup memo reused by the boost
  pass, the fused-candidate loop, and auto-widen's strong/weak partition.
- Add a process-cached `InboundLinkIndex` in `vault.py`, built from one read pass over the vault, so
  `find(pack=true)`'s neighborhood assembly stops re-scanning the whole vault once per packed page.
- Add a synchronous startup warm-up (`src/kb_mcp/warmup.py`) that primes the BM25 corpus (KB and
  vault scope), the wikilink resolver, and page caches after model preload, with a
  `KB_MCP_DISABLE_WARMUP` opt-out and per-stage soft-fail.

Every change here is an internal efficiency change: `find` results, ordering, and the command-surface
contract are byte-identical before and after, except for the freshness-key fix, which corrects
already-wrong stale-index behavior on deletes/renames/replacements rather than changing normal-case
results.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `find-recall-efficiency`: `find` now stat-walks each markdown scope at most once per request,
  reuses per-page derived text across a page's lifetime, uses a stronger corpus-freshness key that
  catches deletes/renames/backdated replacements, and the server warms its retrieval caches at
  startup instead of on the first query.
- `context-packs`: `find(pack=true)` neighborhood assembly now derives inbound links from a cached,
  single-scan vault index instead of one brute-force scan per packed page, with identical output.

## Impact

- Code: `src/kb_mcp/find.py`, `src/kb_mcp/bm25.py`, `src/kb_mcp/vault.py`,
  `src/kb_mcp/context_pack.py`, `src/kb_mcp/server.py`, new `src/kb_mcp/warmup.py`.
- Surfaces: none — no new `find`/command-registry parameters, no MCP/REST/CLI/OpenAPI schema change,
  no change to the committed MCP schema-fidelity fixture.
- Tests: freshness-snapshot walk-count tests, staleness/rename/delete invalidation tests for BM25 and
  the resolver, `ParsedPage` derived-text invalidation tests, combined-multiplier equivalence tests
  against the old three-pass output (fused and rerank modes), inbound-link-index equivalence and
  rename-safety tests, warm-up tests, and a full-suite regression proving byte-identical default
  `find` results.
- Dependencies: none. No optional model becomes mandatory; existing soft-fail behavior for
  embeddings/CLIP/rerank/media is unchanged.
