# Design - reduce find per-query overhead

## Context

Line references below are against the current `src/kb_mcp/find.py`, `bm25.py`, `vault.py`,
`context_pack.py`, and `server.py` (pre `improve-find-latency-token-cost`), since that change has
not landed yet. That change touches some of the same regions (`Hit` serialization, a hot-cache
lookup at the top of `find()`) this change's decisions build on, so expect these line numbers to
shift once it merges; the mechanisms and function names described here do not.

A single hybrid `find(scope="kb")` call today does at least three full-tree markdown stat walks
that exist purely for freshness checking, not ranking:

- `bm25.BM25Index.search` compares `_current_max_mtime(vault_root, scope)` against its cached max
  before deciding whether to rebuild (`bm25.py:141-165`, walk at `bm25.py:174-192`).
- `_get_query_resolver` recomputes a `(count, latest)` key by walking the whole vault before
  deciding whether to reuse the cached `WikilinkResolver` (`find.py:1540-1561`).
- Auto-widen calls `bm25.search(vault_root, query, k=..., scope="vault")` to look for out-of-KB
  matches — and it does this on **every** non-empty-query `scope="kb"` call, not only when the KB
  result set underfills, because the design reserves out-of-KB slots up front rather than
  back-filling (`find.py:605-632`; the comment at `find.py:595-604` explains why reservation, not
  back-fill, is used). That triggers a second `_current_max_mtime` walk, this time over the whole
  vault, plus (on a cache miss) a full `bm25._build(vault_root, "vault")` walk
  (`bm25.py:100-139`).

Both freshness keys are also weaker than they look. `bm25.py:150`'s
`current_max > cached[0]` only fires on an mtime **increase**; it misses a delete (count drops, max
unchanged), a rename (count and max both unchanged), and a replacement whose new file has an older
mtime than the file it replaced (max can even go down, so the comparison never fires).
`_get_query_resolver`'s `(count, latest)` key (`find.py:1555`) has the same rename blind spot: a
rename changes neither the vault's file count nor its max mtime.

Per page, the same derived text is recomputed on every call that touches that page. `_make_excerpt`
lowercases `page.body` on every invocation (`body = page.body.strip(); ... body_norm =
body.lower()`, `find.py:1769-1774`) and is called once per KB page by the keyword lane
(`_keyword_match_paths`, `find.py:1457`) on every hybrid query, plus again per selected hit
(`find.py:679`, `840`, `952`). `_stem_tokens_present` (`find.py:1564-1581`) and `_any_stem_present`
(`find.py:1136-...`) each re-tokenize and re-stem `page.title + " " + page.body` from scratch on
every call via `bm25.tokenize` (lazy-imported as `bm25_module`, `find.py:1575`).

After RRF fusion, three independent passes each re-fetch every candidate's `ParsedPage` and re-sort
the whole fused list: `_apply_type_boost` (`find.py:1219-1240`), `_apply_status_demotion`
(`find.py:1243-1259`), and `_apply_temporal_boost` (`find.py:1339-...`), invoked sequentially at
`find.py:915-922`. When `rerank=True`, the CrossEncoder rescoring can undo those multipliers, so the
type and status multipliers are re-applied a second time directly to `Hit.rerank_score`
(`find.py:1029-1044`) before the final sort — the temporal multiplier is deliberately **not**
re-applied there today.

`vault.find_inbound_wikilinks(vault_root, target_rel_path)` (`vault.py:471-536`) does two full-vault
passes per call: one to count basename occurrences for its uniqueness gate (`vault.py:491-498`), one
to read every file's text and regex-match wikilinks against the target (`vault.py:500-536`).
`context_pack._neighborhood` calls it once per packed page (`context_pack.py:206-210`), and the
default pack size is 5 (`context_pack.py:35`, `_DEFAULT_MAX_HITS`), so a single `find(pack=true)`
does about five full-vault read scans just for the neighborhood.

`server.py`'s `build_server` already preloads bge-base, the reranker, and CLIP synchronously so the
first user-facing call does not pay model-load latency (`server.py:311-346`), gated by
`KB_MCP_DISABLE_EMBEDDINGS`. But nothing primes the BM25 corpus (`bm25.py:100-139`), the wikilink
resolver (`find.py:1540-1561`), or the page-parse cache — those are still built lazily on the first
real hybrid query in the process.

## Goals / Non-Goals

**Goals:**

- Make each of the redundant per-request full-tree walks — BM25 KB freshness, BM25 vault freshness,
  wikilink-resolver freshness — happen at most once per `find` call.
- Make per-page derived text (normalized body/title, stemmed tokens) computed at most once per page
  revision, reused across every query that touches that page while the revision is current.
- Replace the three sequential post-RRF multiplier passes with one combined pass, in both the
  fused-score and rerank-score code paths, without changing which multiplier applies when.
- Replace `find(pack=true)`'s per-packed-page brute-force inbound-link scan with a single cached,
  process-level index rebuilt only when the vault changes.
- Warm the BM25/resolver/page caches at server startup so the first hybrid query in a process is not
  the one that pays for building them.
- Fix the freshness-key blind spots (delete, rename, backdated replacement) uncovered while auditing
  the walks above, since a stronger key is required for the snapshot and inbound-link-index designs
  below to be safe.

**Non-Goals:**

- No change to `find` ranking, ordering, or return shape. The global invariant for this change is
  byte-identical `find` results before and after, for any vault history without deletes, renames, or
  backdated replacements; those histories get corrected (not just faster) freshness behavior.
- No watcher-maintained generation counter as a freshness mechanism. It would be KB-only (a
  `scope="vault"` counterpart needs the whole vault watched too), it is off in tests (the watcher is
  disabled there), and it is a second invalidation mechanism that would have to stay consistent with
  the stat-walk fallback used whenever the watcher is off. A stronger, still-lazy, per-request
  stat-walk key covers the same cases with less surface.
- No reuse of `BM25Index._tokens` (`bm25.py:90-98`) as the source for stem-membership checks. It
  would couple `find.py` to the `BM25Index` singleton's cache lifecycle, and `_tokens` stores
  `list[str]` (order-preserving, duplicates allowed), not the `set[str]` membership test
  `_stem_tokens_present`/`_any_stem_present` need — reusing it would require converting on every
  call anyway, erasing the saving.
- No separate cache for `_keyword_match_paths`'s return value. The in-flight change's hot-query LRU
  (cleared by `find.clear_cache()`, keyed by the full request) already serves an identical repeated
  `find` call from cache; a second, narrower cache for one internal helper would duplicate that
  invalidation surface for a case the hot cache already covers.
- No `Hit`-serialization micro-optimizations and no `get` payload dedup in this change. The former is
  owned by the in-flight change's compact/full serialization work; the latter is its own change,
  `dedupe-get-payload`.
- No new `find`/command-registry parameters, no MCP/REST/CLI/OpenAPI schema change.

## Decisions

### A lazy, per-request `FreshnessSnapshot` replaces per-consumer stat walks

Add a `FreshnessSnapshot` created once inside `find()` per call, exposing two lazily-computed,
memoized-on-the-instance accessors:

- `.kb()` — walks `find._walk_md(vault_root / "Knowledge Base")`, the same walk
  `bm25._current_max_mtime` (`bm25.py:174-192`) and `bm25._build` (`bm25.py:100-139`) already do for
  `scope="kb"`.
- `.vault()` — walks `vault.walk_vault_md(vault_root)`, the same walk `bm25._current_max_mtime` does
  for `scope="vault"` and `_get_query_resolver` does today (`find.py:1540-1561`).

Each accessor computes its result at most once per snapshot instance. `find()` builds exactly one
snapshot per call and threads it as an optional keyword into `bm25.search(...)` (both the
`scope="kb"` call and auto-widen's `scope="vault"` call) and into `_get_query_resolver(...)`. A
`scope="kb"` query with a non-empty `query_norm` — which checks BM25 KB freshness and also triggers
auto-widen's `scope="vault"` search (`find.py:605`) — walks the KB tree once and the vault tree
once for that request, not twice each. A `scope="kb-only"` request never touches `.vault()`, so it
still performs zero vault walks, matching today. Callers that do not pass a snapshot (direct
`bm25.search`/`_get_query_resolver` calls, e.g. from tests) fall back to today's per-call walk, so
this is purely additive.

The snapshot key strengthens the shape both existing consumers use — `bm25.py`'s `current_max >
cached[0]` comparison (`bm25.py:150`) and `_get_query_resolver`'s `(count, latest)` key
(`find.py:1555`) — from `(count, max_mtime)` to `(count, max_mtime_ns, digest)`, where `digest` is a
hash of the sorted vault-relative paths seen during the walk. This is recorded as a **behavior
fix**: the current comparisons miss a delete (count drops, max unchanged), a rename (count and max
both unchanged), and a replacement with an older mtime than the file it replaced (max can even
decrease, so `current_max > cached_max` never fires). Both `bm25.py` and `_get_query_resolver`
adopt the stronger key. For histories without deletes/renames/backdated replacements — the
overwhelming common case — the digest changes exactly when count or max-mtime would have changed
today, so results are identical; the fix only changes behavior for the pathological histories it
corrects.

Alternative considered: a watcher-maintained generation counter, bumped on every observed vault
mutation and compared instead of re-walking. Rejected per the Non-Goals above — it is KB-only, off
in tests, and a second invalidation mechanism to keep consistent with the stat-walk fallback used
whenever the watcher is disabled.

### `cached_property` derived text on `ParsedPage`, no BM25 coupling

Add `body_norm`, `title_norm`, and `stem_set` as `functools.cached_property` members on `ParsedPage`
(`find.py:210`, a plain `@dataclass`, not frozen, so `cached_property` can store into instance
`__dict__` normally):

- `body_norm` — `self.body.strip().lower()`, matching `_make_excerpt`'s current `body =
  page.body.strip(); body_norm = body.lower()` (`find.py:1769-1774`), so `_make_excerpt`,
  `_keyword_match_paths` (`find.py:1457`), and the keyword-mode all-tokens-present gate read the
  cached value instead of recomputing it.
- `title_norm` — `self.title.lower()`.
- `stem_set` — `set(bm25.tokenize(self.title + " " + self.body))`, lazy-imported the same way
  `_stem_tokens_present` already does (`from . import bm25 as bm25_module`, `find.py:1575`, to avoid
  a module-level `find -> bm25` import cycle), replacing the per-call re-tokenize+stem in
  `_stem_tokens_present` (`find.py:1564-1581`) and `_any_stem_present` (`find.py:1136-...`).

Because `FrontmatterCache.get()` already replaces the whole `ParsedPage` object when a file's mtime
changes, rather than mutating an existing one (`find.py:439-451`), a `cached_property` computed once
on a `ParsedPage` instance is invalidated for free the next time that file changes — no separate
invalidation hook is needed. Nothing cached here is query-dependent: the same three properties serve
every query against a given page revision.

Alternative considered (rejected, see Non-Goals): reaching into `BM25Index._tokens` — wrong
container type (`list`, not `set`) and couples `find.py` to the `BM25Index` singleton's lifecycle.

Alternative considered (rejected, see Non-Goals): caching `_keyword_match_paths`'s return list
directly — duplicates the in-flight hot-query LRU's invalidation surface for a case it already
covers.

### One combined post-RRF multiplier pass, in two modes

Replace the sequential `_apply_type_boost` → `_apply_status_demotion` → `_apply_temporal_boost`
calls (`find.py:915-922`, definitions at `find.py:1219`, `1243`, `1339`) with a single
`_apply_post_rrf_multipliers(fused, vault_root, query, config, *, prefer_compiled, prefer_active,
temporal, page_memo)` that computes each path's combined multiplier
(`type_multiplier * status_multiplier * temporal_multiplier`, each defaulting to `1.0` when its
stage is inactive) in one loop and sorts once by `(-score, path)`, instead of three independent
re-fetch-and-sort passes. It preserves the existing media-sidecar exemption from the type/source
penalty (`page.media_type` short-circuits to `mult = 1.0`, `find.py:1232-1235`), and returns `fused`
unchanged when no stage is active — matching the current behavior where each of the three functions
only runs (and only re-sorts) when its own gate is true.

`find.py:1029-1044` re-applies the type and status multipliers a second time, to `Hit.rerank_score`
after the CrossEncoder rescoring, so `prefer_compiled`/`prefer_active` survive the post-rerank sort;
the temporal multiplier is deliberately not re-applied there today. The combined-multiplier helper
needs a second mode for this: the same per-path multiplier computation, applied to `h.rerank_score`
per hit instead of to `(path, score)` fused pairs, with the temporal multiplier excluded exactly as
today. Both modes share one multiplier computation, so a `prefer_compiled`+`prefer_active`
combination produces the identical numeric multiplier whether it runs in fused-score mode or
rerank-score mode.

A per-request `dict[str, ParsedPage | None]` page memo is threaded through and shared by three call
sites that each independently call `_CACHE.get(vault_root / path, vault_root)` today: the combined-
multiplier pass, the fused-candidate-to-`Hit` resolution loop, and auto-widen's strong/weak
partition (`find.py:620-632`, which calls `_CACHE.get` per hit to test `_stem_tokens_present`).
Since `FrontmatterCache.get()` is already an mtime-keyed cache (`find.py:439-451`), the per-request
memo only removes the dict-lookup-and-mtime-stat overhead of asking the same cache for the same path
repeatedly within one `find()` call — it is not a correctness concern.

Alternative considered: none tracked separately for this decision — the combined pass described
above **is** the alternative to the current three-pass design; no wider refactor of the multiplier
gates themselves (e.g. making them config-driven beyond today's booleans) was considered in scope.

### A process-cached `InboundLinkIndex` in `vault.py`

Add a module-level, process-cached reverse index in `vault.py`: `normalized_target -> ordered list
of (seq, path, line_number, context, raw_target)` entries, plus a `basename -> count` map for the
uniqueness gate `find_inbound_wikilinks` currently recomputes on every call (`vault.py:491-498`).
The index is built in one read pass over `walk_vault_md(vault_root)`, replacing the current per-call
double walk — one pass for basename-uniqueness counting (`vault.py:491-498`), one pass reading every
file's text and regex-matching wikilinks (`vault.py:500-536`).

`context_pack._neighborhood` calls `vault.find_inbound_wikilinks` once per packed page — five calls,
and five full-vault scans, for the default `KB_MCP_PACK_MAX_HITS=5` (`context_pack.py:35`,
`context_pack.py:206-210`) — so building the index once per index revision and looking up each
packed page's target against it turns that into one scan regardless of pack size.

`find_inbound_wikilinks(vault_root, target_rel_path)` keeps its exact signature and output
ordering: entries for a normalized target are merged by insertion sequence (`seq`) across the
`target_full`/`target_stripped`/basename-match branches, so the merged order matches today's
single-pass-in-file-order behavior, and the target file itself is excluded from its own inbound list
exactly as `vault.py:511-512` does today.

The freshness key MUST be digest-strength — a hash of sorted `(rel_path, mtime_ns)` pairs, not a
`(count, max_mtime)` pair. This is called out explicitly because `move_file`/`delete_file`'s
existing safety checks (which query inbound links before allowing a destructive operation) must see
a cache that a pure rename invalidates: a rename changes neither the vault-wide file count nor any
file's mtime, so a weaker key would let a stale index tell `move_file`/`delete_file` that a
just-renamed reference no longer exists.

A reset hook is wired into `find.clear_cache()` (`find.py:1810-1812`) or an added
`vault.clear_link_index()`, so tests that mutate the vault and re-check inbound links can force a
rebuild the same way other freshness-keyed caches in this codebase are cleared.

Alternative considered: computing the index lazily inside `_neighborhood` and passing it down as a
parameter instead of module-caching it in `vault.py`. Rejected because `find_inbound_wikilinks` is a
public `vault.py` function called from more than one place (context packs today; potentially direct
inbound-link queries elsewhere), and a module-level cache keeps every caller's freshness behavior
consistent without every caller needing to know about and construct an index parameter.

### Synchronous startup warm-up, mirroring the existing model preload

Add `src/kb_mcp/warmup.py` with `warm_caches(vault_root)`, called synchronously in `build_server`
(`server.py`) immediately after the existing embedding/reranker/CLIP preload block
(`server.py:311-346`). It walks the KB through `find._CACHE` so every KB page is parsed once, calls
an explicit `bm25.warm(vault_root, scope)` for **both** `scope="kb"` and `scope="vault"` — vault
scope is needed because auto-widen's `bm25.search(scope="vault")` runs on every non-empty
`scope="kb"` query (`find.py:605`), not only on genuine underfill — builds the wikilink resolver via
`_get_query_resolver`, and instantiates `EmbeddingIndex` when embeddings are enabled. This means the
first real hybrid query in a fresh process no longer pays the lazy BM25 corpus build
(`bm25.py:100-139`), the lazy resolver build (`find.py:1540-1561`), or the first-page-parse cost that
model preload does not cover.

`KB_MCP_DISABLE_WARMUP` opts out, mirroring the existing `KB_MCP_DISABLE_EMBEDDINGS`/
`KB_MCP_DISABLE_MEDIA_EXTRACTION` env-flag convention (`server.py:315`, `server.py:352`). Each stage
soft-fails independently with a warning and a duration log, matching the existing preload block's
per-model `try/except ... # noqa: BLE001 — preload is best-effort` pattern (`server.py:316-346`) — a
warm-up failure must never prevent the server from starting or change what a subsequent `find` call
returns; it only changes whether that call pays a cold-start cost. No registry/schema surface
changes: warm-up is an internal call inside `build_server`, not a new command or parameter.

Alternative considered: an async/background warm-up task kicked off without blocking server startup.
Rejected to match the existing preload block's synchronous, best-effort style exactly, and because an
async warm-up would let the very first real request race the warm-up and pay the exact cold-start
cost this change removes — the point is that the first hybrid query in the process is already warm.

## Risks / Trade-offs

- Hashing sorted paths on every freshness check costs more per walk than a bare max-mtime comparison
  -> the walk itself (stat'ing every file) already dominates; hashing the already-collected sorted
  path list is cheap relative to the walk, and the walk now happens at most once per
  request/consumer instead of three-plus times.
- Combining three multiplier passes into one with two modes (fused/rerank) could silently drop the
  media-sidecar exemption or the rerank-mode's intentional omission of the temporal multiplier ->
  cover both with an equivalence-grid test against the old three-pass output before removing the old
  functions.
- `InboundLinkIndex`'s freshness key must be digest-strength or `move_file`/`delete_file` safety
  checks can silently mis-evaluate after a rename -> covered explicitly above and in the test plan's
  rename-after-cached-call case.
- Startup warm-up could slow server startup on very large vaults -> the `KB_MCP_DISABLE_WARMUP`
  opt-out and per-stage soft-fail bound the downside; warm-up runs once per process start, not per
  request.
- `cached_property` on `ParsedPage` could go stale if some code path ever mutated a `ParsedPage` in
  place instead of replacing it via `FrontmatterCache.get()` -> `FrontmatterCache.get()` is the only
  place `ParsedPage` instances are constructed and cached (`find.py:434-454`); no code path mutates
  an existing instance's `body`/`title` today.

## Migration Plan

No data migration. This is an internal, code-only change: no new required environment variables
(`KB_MCP_DISABLE_WARMUP` is opt-out, warm-up on by default), no new command-registry parameters, and
no schema-fidelity fixture change. Existing deployments get the warm-up and the
freshness/derived-text/multiplier/inbound-index efficiency changes on their next deploy with no
config changes. If a regression is suspected, `KB_MCP_DISABLE_WARMUP=1` isolates whether warm-up
(rather than the per-request efficiency changes) is responsible; the freshness-key fix can be
reasoned about independently since it only changes behavior for delete/rename/backdated-replacement
histories.

## Open Questions

None for implementation. The `get` payload dedup identified during this audit is scoped to its own
change (`dedupe-get-payload`) and is not addressed here.
