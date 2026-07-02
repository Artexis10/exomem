# Design - improve find latency and token cost

## Context

`find` currently returns a list of `Hit` objects from `src/kb_mcp/find.py`; `op_find` in
`commands.py` serializes those hits and optionally wraps them with a context pack when
`pack=true`. Hybrid search already has distinct lanes - vector, BM25, keyword, CLIP, graph,
temporal, fusion, rerank, auto-widen, and post-filtering - but timings are only visible through
external wall-clock measurement. Per-hit `signals` explain why a hit ranked, not where time went.

The repo already has mtime-based freshness primitives: parsed pages cache by file mtime, BM25
rebuilds on count/max-mtime changes, the wikilink resolver uses count/max-mtime, and embedding
drift compares markdown mtimes to sidecar row mtimes. The watcher watches `Knowledge Base/` and
re-embeds out-of-band edits, but its docstring explicitly accepts duplicate re-embedding after
server-authored writes.

The change borrows the low-friction parts that help Exomem - quick recall surfaces, measurement,
and small hot caching - without porting Engram's packaging or replacing Exomem's owned-vault,
governed, inspectable, multimodal model.

## Goals / Non-Goals

**Goals:**

- Make `find` latency measurable per lane/stage without requiring optional embedding, CLIP, or
  rerank models to be installed.
- Reduce token cost for callers that only need a recall shortlist, while preserving the existing
  default full hit shape.
- Avoid duplicate watcher reindexing for Exomem-authored writes that already refreshed the
  embedding sidecar.
- Add a small, bounded in-process hot cache for identical repeated recall queries, invalidated by
  vault and sidecar freshness.
- Keep all new user-facing `find` parameters on the unified command registry so MCP, REST, CLI,
  and OpenAPI stay consistent.

**Non-Goals:**

- No LSH, ANN migration, new vector database, or broader retrieval architecture rewrite in this
  change.
- No server-side reasoning model and no confidence/authority score on notes.
- No change to default `find` ranking, default `find` return shape, context-pack semantics, or
  out-of-band edit reindexing.

## Decisions

### Timing is opt-in and travels with the `find` response

Add `include_timings: bool = False` to the registry-level `find` command. When false, the return
shape is unchanged. When true, `op_find` returns an envelope containing `hits` and `timings`; if
`pack=true`, the existing pack envelope gains a sibling `timings` field.

`timings` should include `total_ms`, `cache` metadata, and per-stage entries keyed by stable names:
`freshness`, `cache_lookup`, `keyword`, `vector`, `clip`, `bm25`, `graph`, `temporal`, `fusion`,
`filter_hits`, `rerank`, `outside_kb`, `date_filter`, `pack`, and `serialize`. A stage can record
`skipped: true` or `error: <short class/name>` while still allowing `find` to return the same hits
it would have returned today. Timings must not include note body, excerpts, query-expanded text, or
vectors.

Alternative considered: log-only timing. That is useful for operations, but it does not help an
agent diagnose a slow recall call in the same turn. Returning opt-in diagnostics keeps the default
surface clean.

### Compact/detail mode is a serialization choice, not a second ranker

Add `detail: "full" | "compact" = "full"` to the `find` command. `full` is the current hit dict.
`compact` serializes the same ranked `Hit` objects with routing fields only: `path`, `title`,
`type`, `scope`, `updated`, lifecycle fields, media pointers, `outside_kb`, and `clip_match_at` when
present. It omits `excerpt` and `signals` by default because those dominate result tokens and are
available by rerunning `find(detail="full")` or reading a selected page with `get`.

Alternative considered: add `excerpt_chars`. A numeric excerpt knob creates more surface area and
still encourages shipping snippets in every call. A binary compact/full surface is easier for agents
to choose and test.

### The hot cache is small, request-keyed, and freshness-keyed

Add an internal LRU cache for base `find` hits, default size 32 with an environment override such as
`KB_MCP_FIND_CACHE_SIZE`; `0` disables it. The cache key includes the resolved vault root, query,
all filters, limit, scope, mode, graph/rerank/temporal/intent/date/preference knobs, and the active
ranking-config identity. It does not key on `detail` because detail is serialization over the same
hits. It should bypass or separately key explicit non-default internal `config` objects used by
ranking tests.

The freshness key should include the markdown scope that can affect the request:

- `scope="kb-only"`: count and max mtime under `Knowledge Base/`.
- `scope="vault"`: count and max mtime for the whole vault walk.
- `scope="kb"` with a non-empty query: both KB and vault freshness, because auto-widen can reserve
  out-of-KB results.
- Embedding sidecar mtime for hybrid/vector requests when embeddings are enabled.
- CLIP sidecar mtime when CLIP search can contribute.

Cached hits are copied before returning so caller mutation cannot poison later calls. A cache hit
must still record timings showing that the cache answered the request.

Alternative considered: cache only serialized dicts. Caching base hits avoids duplicated entries for
compact/full formatting and keeps `pack=true` able to assemble a fresh pack from the same hits.

### Watcher self-write suppression is explicit and bounded

Add a module-level self-write suppression registry in `file_watcher.py`, keyed by resolved vault root
and vault-relative path. Writer paths that already perform their own embedding update register their
self-authored mutations after filesystem replacement and before/around the existing sidecar update.
The watcher checks the registry in `_record` before enqueueing an upsert/delete.

For create/modify events, store a file signature such as `mtime_ns` plus size so a later manual edit
of the same path is not hidden by a stale suppression entry. For delete/move-away events, use a
short TTL because there is no post-delete stat. The registry is opportunistic and bounded; expired
entries are pruned during checks.

Alternative considered: give `FileWatcher` a direct reference to every writer. That would couple the
server lifecycle to low-level write helpers and make tests harder. A small module-level registry is
available even when the watcher is disabled and is easy to exercise directly.

### Query logging can capture summary timing, not result text

Extend `query_log.log_find_call` to accept optional timing summary fields such as `total_ms`,
`cache_hit`, and per-stage milliseconds. It must continue to log paths and signals only, never
excerpts or bodies.

## Risks / Trade-offs

- Cache freshness misses an input that affects ranking -> include all behavior-affecting request
  params plus markdown and sidecar freshness; add tests for markdown and sidecar invalidation.
- Cache returns mutable stale objects -> return copies of cached hits and clear the cache from the
  existing `find.clear_cache()` test hook.
- Timing instrumentation perturbs latency -> keep spans lightweight with `time.perf_counter()` and
  collect only when diagnostics/logging need them.
- Self-write suppression hides a real external edit -> require matching file signature for upserts
  and a short TTL for delete events; add a same-path external-edit test.
- Schema drift surprises MCP callers -> update the command registry once and regenerate the MCP
  schema-fidelity fixture, with tests proving default `find` remains unchanged.

## Migration Plan

No data migration is required. Deploy as a code change. Existing clients that omit `detail` and
`include_timings` receive the current `find` shape. If a cache issue is suspected, set the cache size
to `0` or call the existing cache-clear test/admin seam while preserving correctness through the
uncached path.

## Open Questions

None for implementation. LSH or larger retrieval rewrites stay deferred until the new timings show a
specific lane is responsible for unacceptable latency.
