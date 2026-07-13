"""Read-only search across the Knowledge Base.

Scans every `.md` under `Knowledge Base/`, parses YAML frontmatter, filters by
structured fields, then does case-insensitive substring matching on
title + body. A typical vault is hundreds of pages — full-scan is fast enough.

Cached in-process between calls: keyed by file path, invalidated by mtime.
"""

from __future__ import annotations

import copy
import logging
import os
import threading
import time
from collections import OrderedDict
from datetime import date
from pathlib import Path
from typing import Any

from . import find_candidates, find_corpus, find_policy, find_results, find_types, freshness
from . import ranking_config as _ranking_config
from .find_types import (
    FindTimings,
    Hit,
    ParsedPage,
)
from .kbdir import kb_dirname, kb_prefix

log = logging.getLogger(__name__)

EXCLUDED_DIR_NAMES = find_corpus.EXCLUDED_DIR_NAMES
# Navigation files — auto-generated summaries / activity logs. Their bodies
# mention every recently-written page, so they false-positive on hybrid
# queries that touch any term recently introduced into the KB. Excluded
# from search results regardless of mode.
_NAVIGATION_BASENAMES = find_corpus.NAVIGATION_BASENAMES
FRONTMATTER_PATTERN = find_corpus.FRONTMATTER_PATTERN
H1_PATTERN = find_corpus.H1_PATTERN

FrontmatterCache = find_corpus.FrontmatterCache
_CACHE = find_corpus.CACHE
_walk_freshness_key = find_corpus.walk_freshness_key
_walk_md = find_corpus.walk_md
_parse_page = find_corpus.parse_page
_passes_filters = find_corpus.passes_filters
_all_projects = find_corpus.all_projects
_format_timestamp = find_types._format_timestamp
_span = find_types.timing_span

EXCERPT_RADIUS = find_results.EXCERPT_RADIUS
EXCERPT_MAX_LEN = find_results.EXCERPT_MAX_LEN
_transcript_ts_for_hit = find_results.transcript_ts_for_hit
_stem_tokens_present = find_results.stem_tokens_present
_stem_anchored_excerpt = find_results.stem_anchored_excerpt
_semantic_excerpt = find_results.semantic_excerpt
_make_excerpt = find_results.make_excerpt
_collapse = find_results.collapse

# --- Silent-degradation counter (process-scoped observability) --------------
# Every semantic lane has a soft-fallback, and a POST-WARM failure historically
# dropped the request to a weaker ranking (vector→BM25, or every-lane-empty→
# keyword) emitting nothing but a log line — so a persistently broken sidecar or
# a flapping model was invisible in aggregate. These counters make the fallbacks
# countable: doctor (and any future health endpoint) can read them, and each
# find carries a per-request `degraded` envelope marker built from `failed_out`
# below (distinct from the `warming` marker, which means "lane deferred while a
# model preload is still in flight", not "lane failed"). Thread-safe — find runs
# on FastMCP/REST worker + file-watcher threads concurrently.
_DEGRADATION_LOCK = threading.Lock()
_DEGRADATION_COUNTS: dict[str, int] = {}


def _record_degradation(lane: str) -> None:
    """Increment the process-lifetime silent-degradation counter for `lane`."""
    with _DEGRADATION_LOCK:
        _DEGRADATION_COUNTS[lane] = _DEGRADATION_COUNTS.get(lane, 0) + 1


def degradation_counts() -> dict[str, int]:
    """Snapshot of per-lane post-warm silent-degradation counts (process-scoped).

    Keys: "vector" (vector lane failed post-warm → BM25-only ranking), "clip"
    (CLIP image lane failed → image search skipped), "no_candidates" (every lane
    produced nothing → keyword fallback). Empty when nothing degraded this
    process. These count genuine fallbacks, NOT warm-time deferrals.
    """
    with _DEGRADATION_LOCK:
        return dict(_DEGRADATION_COUNTS)


def reset_degradation_counts() -> None:
    """Test hook: zero the degradation counters (process-reset, like clear_cache)."""
    with _DEGRADATION_LOCK:
        _DEGRADATION_COUNTS.clear()


DEFAULT_RANKING = _ranking_config.DEFAULT_RANKING
LANE_ORDER = _ranking_config.LANE_ORDER
RankingConfig = _ranking_config.RankingConfig
ranking_config_from_jsonable = _ranking_config.ranking_config_from_jsonable
ranking_config_to_jsonable = _ranking_config.ranking_config_to_jsonable
_REPO_ROOT = _ranking_config._REPO_ROOT


def _active_ranking() -> RankingConfig:
    """Compatibility wrapper for find.py's historical adopted-config seam."""
    _ranking_config._REPO_ROOT = _REPO_ROOT
    return _ranking_config._active_ranking()


def reset_active_ranking_cache() -> None:
    """Drop the memoized adopted ranking config."""
    _ranking_config.reset_active_ranking_cache()


def _accelerated_device(device: str) -> bool:
    """Whether a resolved torch device is an accelerator worth auto-reranking on."""
    d = (device or "").strip().lower()
    return d == "mps" or d == "cuda" or d.startswith("cuda:")


def auto_rerank_allowed_by_policy() -> bool:
    """True when unset `rerank` may invoke the CrossEncoder automatically.

    Explicit `rerank=True` remains allowed anywhere unless EXOMEM_DISABLE_RANKING
    hard-disables the reranker. This gates only the default/auto path so normal
    and quiet CPU service modes keep `find` latency predictable. The common
    normal/quiet path avoids probing torch; the device selector is consulted only
    when performance mode or an explicit text-device override asks for acceleration.
    """
    from . import mode as mode_module

    explicit_device = any(
        os.environ.get(env) and os.environ.get(env, "").strip()
        for env in ("EXOMEM_EMBED_DEVICE", "EXOMEM_DEVICE", "EXOMEM_TORCH_DEVICE")
    )
    if not explicit_device and mode_module.resolve_mode() != "performance":
        return False
    try:
        from . import accel

        return _accelerated_device(
            accel.select_device(override_env="EXOMEM_EMBED_DEVICE")
        )
    except Exception:  # noqa: BLE001 - auto-rerank must fail closed to cheap find.
        return False


# --------------------------------------------------------------------------- #
# Hot find cache: bounded in-process LRU over base Hit lists (OpenSpec change
# improve-find-latency-token-cost). Keyed by the FULL recall request (every
# ranking/filtering knob + the resolved RankingConfig, which is frozen and
# hashable) plus a freshness key covering the markdown scope the request can
# see, the embedding/CLIP sidecars when a semantic lane could contribute, and
# today's date (temporal lanes and recency filters are date-relative). Cached
# values are deep-copied on the way in AND out so caller mutation can never
# poison a later response. `EXOMEM_FIND_CACHE_SIZE=0` disables it.
# --------------------------------------------------------------------------- #
_FIND_CACHE: OrderedDict[tuple, list[Hit]] = OrderedDict()
_FIND_CACHE_LOCK = threading.Lock()
_DEFAULT_FIND_CACHE_SIZE = 32


def _find_cache_size() -> int:
    raw = os.environ.get("EXOMEM_FIND_CACHE_SIZE")
    if raw is None or not raw.strip():
        return _DEFAULT_FIND_CACHE_SIZE
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning("EXOMEM_FIND_CACHE_SIZE=%r is not an int; using default", raw)
        return _DEFAULT_FIND_CACHE_SIZE



class FreshnessSnapshot:
    """Per-request corpus freshness: each markdown scope is resolved at most
    once per `find()` call, and every consumer (hot-cache key, BM25 rebuild
    check, wikilink-resolver reuse, auto-widen's vault BM25) shares the result
    instead of recomputing. Lazy — a `scope="kb-only"` request never pays the
    vault cost.

    Reads the event-maintained `freshness` registry when it is live for the
    scope (sub-ms, syscall-free); otherwise falls back to a full stat-walk that
    yields a byte-identical triple."""

    def __init__(self, vault_root: Path) -> None:
        self._root = vault_root
        self._kb: tuple[int, int, str] | None = None
        self._vault: tuple[int, int, str] | None = None

    def kb(self) -> tuple[int, int, str]:
        if self._kb is None:
            live = freshness.triple(self._root, "kb")
            if live is not None:
                self._kb = live
            else:
                kb = self._root / kb_dirname()
                self._kb = _walk_freshness_key(_walk_md(kb) if kb.is_dir() else ())
        return self._kb

    def vault(self) -> tuple[int, int, str]:
        if self._vault is None:
            live = freshness.triple(self._root, "vault")
            if live is not None:
                self._vault = live
            else:
                from .vault import walk_vault_md

                self._vault = _walk_freshness_key(walk_vault_md(self._root))
        return self._vault

    def for_scope(self, scope: str) -> tuple[int, int, str]:
        return self.vault() if scope == "vault" else self.kb()


def _freshness_key(
    vault_root: Path,
    *,
    scope: str,
    query_norm: str,
    mode: str,
    graph: bool,
    snapshot: FreshnessSnapshot,
) -> tuple:
    """Freshness inputs that can change this request's answer.

    - scope="kb-only": KB walk key only.
    - scope="vault": full-vault walk key.
    - scope="kb" with a non-empty query: BOTH (auto-widen reserves out-of-KB
      slots on every non-empty query).
    - hybrid/vector modes: each semantic sidecar's `(epoch, generation, instance)`
      write token (0,0,0 when absent), since sidecar refreshes change semantic
      results.
      Deliberately NOT the sidecar file mtime — WAL-checkpoint timing moves it
      independent of content (spurious misses) and leaves an uncheckpointed commit
      unmoved (STALE hits); the in-band generation changes iff the content did.
      See EmbeddingIndex.cache_token / lexstore.cache_token.
    """
    parts: list[Any] = [date.today().toordinal()]
    if scope in ("kb", "kb-only"):
        parts.append(("kb", *snapshot.kb()))
    if scope == "vault" or (scope == "kb" and query_norm):
        parts.append(("vault", *snapshot.vault()))
    if mode in ("hybrid", "vector"):
        from . import embeddings

        parts.append((".embeddings.sqlite", embeddings.EmbeddingIndex.cache_token(vault_root)))
        parts.append((".clip.sqlite", embeddings.ClipIndex.cache_token(vault_root)))
        if graph:
            # The typed graph lane can re-rank on sidecar content, so its in-band
            # generation token joins the key (same guard as embeddings). Absent
            # sentinel when the sidecar is unavailable keeps typed-mode and
            # fallback-mode entries from colliding; never the sidecar mtime, which
            # a WAL checkpoint moves without a content change.
            from . import epistemic_graph

            parts.append((".graph.sqlite", epistemic_graph.cache_token(vault_root) or "absent"))
    if mode in ("hybrid", "keyword"):
        # Which lexical backend serves (fts5 vs python) changes bm25-lane
        # scores, so a mid-process flip must not hit entries cached under the
        # other scorer. Index CONTENT changes always ride the walk triples
        # above; lexstore.cache_token explains why the sidecar's file mtime
        # is deliberately not used here.
        from . import lexstore

        parts.append(("lexical", lexstore.cache_token(vault_root)))
    return tuple(parts)


def find(
    vault_root: Path,
    *,
    query: str,
    types: list[str] | None = None,
    projects: list[str] | None = None,
    tags: list[str] | None = None,
    speakers: list[str] | None = None,
    file_types: list[str] | None = None,
    exclude_file_types: list[str] | None = None,
    limit: int = 15,
    scope: str = "kb",
    mode: str = "hybrid",
    graph: bool = True,
    rerank: bool | None = None,
    auto_rerank: bool = False,
    temporal: bool = True,
    intent: str | None = None,
    updated_after: str | None = None,
    updated_before: str | None = None,
    recency_days: int | None = None,
    prefer_compiled: bool = True,
    prefer_active: bool = True,
    prefer_used: bool = False,
    config: RankingConfig | None = None,
    timings: FindTimings | None = None,
    degraded_out: list[str] | None = None,
    failed_out: list[str] | None = None,
) -> list[Hit]:
    """Search the vault. Returns up to `limit` hits.

    `degraded_out`: optional caller-owned list. While the background warm-up
    is in flight (see `readiness`), model-touching lanes (vector, CLIP,
    rerank) are skipped instead of blocking on a model load; each skipped
    lane appends its component name here so the caller can mark the response
    as warming. Empty after the call = full ranking ran. Degradation is
    tracked internally even when the caller passes None, so a lexical-only
    ranking produced mid-warm is never stored in the hot cache.

    `failed_out`: optional caller-owned list, the POST-WARM sibling of
    `degraded_out`. A lane that FAILED (vector/CLIP `except`, or the
    all-lanes-empty keyword fallback) — not merely deferred — appends its lane
    name here and bumps `degradation_counts()`. The caller surfaces this as a
    `degraded` envelope marker, distinct from `warming`. A failed result is also
    never cached (a transient sidecar/model failure must not stick).

    `scope` controls the walk root:
    - "kb" (default): only `Knowledge Base/`. Compiled material + sources.
    - "vault": full vault, including sibling folders outside
      `Knowledge Base/` (e.g. curated, read-only material kept in its own
      top-level folders). Use when you need to discover content outside the
      KB. Existing filters still apply — such pages typically lack structured
      frontmatter so `types`/`projects`/`tags` filters won't match many of
      them; free-text queries work fine.

    `mode` controls the ranker:
    - "hybrid" (default): BM25 + local vector embeddings fused via RRF.
      Best recall on natural-language queries. Empty query falls back to
      keyword behavior (filtered most-recent). Embedding sidecar is
      KB-scoped; with `scope="vault"`, vector results cover KB only
      while BM25 covers the full vault.
    - "keyword": case-insensitive substring matching across title + body,
      sorted most-recently-updated first. The original behavior, preserved
      for backward compatibility.
    - "vector": vector embeddings only, no BM25. Testing aid for
      isolating semantic recall.

    `graph`: when True (default for hybrid/vector), the outbound wikilinks
    of top-ranked BM25/vector candidates contribute a third ranking that
    surfaces 1-hop neighbours of strong matches. Set False for pure
    BM25+vector hybrid without graph expansion.

    `rerank`: True/False forces the BAAI/bge-reranker-base CrossEncoder pass
    on/off; `None` (default) defers to `auto_rerank`. When on, runs the top
    `3 * limit` fused candidates through the reranker and re-sorts by reranker
    score. Recovers ordering quality on ambiguous queries — the LLM-Wiki cases
    where vector floats a topically-off doc to the top. ~50ms / candidate on
    Blackwell. Off by default to keep the model out of the common path.

    `auto_rerank`: when True AND `rerank` is left unset (None), the reranker
    fires only when `should_rerank()` judges it worthwhile (top-3 vector/bm25
    disagreement >50% or a long query). Public callers should gate this through
    `auto_rerank_allowed_by_policy()` so CPU steady-state modes keep predictable
    latency. An explicit `rerank=True/False` always wins over this. Default
    False so the suite never loads the model implicitly.

    `temporal`: when True (default), temporal queries (recent/latest/when/...)
    get a recency fusion lane and the optional Gaussian recency boost
    (`config.temporal_boost`). Both are strict no-ops on non-temporal queries,
    so this never perturbs the common case. Set False to disable recency logic.

    `intent`: force the intent label ("exact"/"temporal"/"relationship"/
    "conceptual") instead of classifying from the query text — a testing/override
    seam. None (default) auto-classifies. Drives the per-intent lane weights.

    `updated_after` / `updated_before` (ISO date strings) and `recency_days`
    (int) are an explicit post-filter: hits whose `updated` date falls outside
    the window are dropped (undated hits drop too). All None/off by default.

    `prefer_compiled`: when True (default), applies a small multiplicative
    boost to fused/rerank scores for COMPILED page types (insight, pattern,
    failure, research-note, entity) and a small penalty for raw `source`
    pages. Reflects the KB's epistemic hierarchy — compiled distillations
    are the intentional output, sources are inputs. Set False to retrieve
    raw source discussion verbatim (e.g. "what did I capture from Dr. X").

    `prefer_active`: when True (default), soft-demotes `status: superseded`
    pages so a replaced conclusion can't outrank the page that superseded it.
    The tombstone stays findable (never excluded) and its hit carries `status`
    + `superseded_by` either way, so the reader sees it's superseded and where
    it points. Set False to rank superseded pages on their content alone (e.g.
    "what did I used to think about X").

    `prefer_used`: when True (OFF by default — default ranking is usage-blind
    and byte-identical), applies a bounded, positive-only usage-activation
    boost from the JSONL access logs (see `usage.py`): pages you actually
    read and cite get up to `config.usage_boost` (≤ the compiled boost, so
    usage breaks ties but never overrides the epistemic hierarchy). Never a
    penalty, never creates candidates — it can only reorder pages the
    content lanes already surfaced. Boosted hits expose `signals.activation`
    and `signals.usage_boost`. Strict no-op on cold start, absent logs, or
    `EXOMEM_DISABLE_USAGE_BOOST`. Bypasses the hot find cache.
    """
    if scope not in ("kb", "vault", "kb-only"):
        raise ValueError(
            f"find: scope must be 'kb', 'vault', or 'kb-only', got {scope!r}"
        )
    if mode not in ("hybrid", "keyword", "vector"):
        raise ValueError(
            f"find: mode must be 'hybrid', 'keyword', or 'vector', got {mode!r}"
        )
    if limit < 1:
        limit = 1
    limit = min(limit, 100)
    query_norm = (query or "").lower().strip()

    # One freshness snapshot + one parsed-page memo per request: every
    # consumer below (hot cache, BM25, resolver, auto-widen, boost passes)
    # shares them instead of re-walking / re-stat'ing.
    snapshot = FreshnessSnapshot(vault_root)
    page_memo: dict[str, ParsedPage | None] = {}

    def _page_of(rel: str) -> ParsedPage | None:
        if rel not in page_memo:
            page_memo[rel] = _CACHE.get(vault_root / rel, vault_root)
        return page_memo[rel]

    # ---- Hot cache lookup (freshness-keyed; see _freshness_key above) ----
    # prefer_used bypasses the cache entirely — simplest correct interaction;
    # log freshness never has to enter the cache key.
    resolved_config = config if config is not None else _active_ranking()
    cache_size = 0 if prefer_used else _find_cache_size()
    cache_key: tuple | None = None
    if timings is not None:
        timings.cache["enabled"] = cache_size > 0
    if cache_size > 0:
        def _t(v: list | None) -> tuple | None:
            return tuple(v) if v is not None else None

        request_key = (
            str(vault_root.resolve()), query, _t(types), _t(projects), _t(tags),
            _t(speakers), _t(file_types), _t(exclude_file_types), limit, scope,
            mode, graph, rerank, auto_rerank, temporal, intent,
            updated_after, updated_before, recency_days,
            prefer_compiled, prefer_active, resolved_config,
        )
        with _span(timings, "freshness"):
            fresh = _freshness_key(
                vault_root, scope=scope, query_norm=query_norm, mode=mode,
                graph=graph, snapshot=snapshot,
            )
        cache_key = (request_key, fresh)
        with _span(timings, "cache_lookup"):
            with _FIND_CACHE_LOCK:
                cached = _FIND_CACHE.get(cache_key)
                if cached is not None:
                    _FIND_CACHE.move_to_end(cache_key)
        if cached is not None:
            if timings is not None:
                timings.cache["hit"] = True
            return copy.deepcopy(cached)

    # Track warm-window degradation even when the caller passed no list —
    # internal callers (suggest_links, evolution, note/add sweeps) must never
    # cache a lexical-only ranking that would outlive the warm.
    degraded = degraded_out if degraded_out is not None else []
    # Same rationale for POST-WARM lane failures: a BM25-only result produced
    # because the vector lane threw must not be cached and served after the
    # sidecar/model recovers. Tracked internally even when the caller passes None.
    failed = failed_out if failed_out is not None else []

    # "kb-only" is the strict opt-out (legacy KB-only behavior); "kb" walks the
    # same KB tree but auto-widens to the vault below when it underfills. Both
    # map to a KB-only walk in the underlying rankers.
    walk_scope = "vault" if scope == "vault" else "kb"

    # Empty queries always degrade to keyword behavior — there's no signal
    # to embed or score with, just "give me recent stuff that matches the
    # structured filters."
    if mode == "keyword" or not query_norm:
        with _span(timings, "keyword"):
            hits = _find_keyword(
                vault_root,
                query_norm=query_norm,
                types=types, projects=projects, tags=tags, speakers=speakers,
                file_types=file_types, exclude_file_types=exclude_file_types,
                limit=limit, scope=walk_scope,
            )
    else:
        hits = _find_semantic(
            vault_root,
            query=query, query_norm=query_norm,
            types=types, projects=projects, tags=tags, speakers=speakers,
            file_types=file_types, exclude_file_types=exclude_file_types,
            limit=limit, scope=walk_scope, mode=mode, graph=graph, rerank=rerank,
            auto_rerank=auto_rerank, temporal=temporal, intent=intent,
            prefer_compiled=prefer_compiled,
            prefer_active=prefer_active,
            prefer_used=prefer_used,
            config=resolved_config,
            timings=timings,
            snapshot=snapshot,
            page_memo=page_memo,
            degraded_out=degraded,
            failed_out=failed,
        )

    # Auto-widen: reach into the wider vault (sibling folders like Tracking/,
    # Reference/, plus curated trees) so content outside Knowledge Base/ isn't
    # silently invisible. Only for scope="kb" (not "kb-only"/"vault") and
    # non-empty queries (an empty query has no signal to widen on).
    #
    # We RESERVE a few result slots for out-of-KB hits rather than only
    # back-filling when the KB underfills. The reason is empirical: on a real
    # vault a bare query like "X3" finds 8+ KB files that literally mention the
    # term, which fills `limit` — so a count- or even quality-gated back-fill
    # never fires, and the actual out-of-KB target (e.g. `Tracking/X3 Full
    # Reps.md`, whose title IS the query) stays hidden. Reserving guarantees
    # such a match surfaces. The KB keeps the majority of slots (strong literal
    # hits first, then weak graph/recency filler); the reserve never starves
    # the KB (capped at limit-1) and is empty when nothing outside matches.
    if scope == "kb" and query_norm:
        with _span(timings, "outside_kb"):
            seen = {h.path for h in hits}
            outside = [
                h for h in _find_outside_kb(
                    vault_root,
                    query=query,
                    query_norm=query_norm,
                    types=types, projects=projects, tags=tags, speakers=speakers,
                    file_types=file_types, exclude_file_types=exclude_file_types,
                    limit=limit, snapshot=snapshot,
                )
                if h.path not in seen
            ]
            if outside:
                strong: list[Hit] = []
                weak: list[Hit] = []
                for h in hits:
                    page = _page_of(h.path)
                    # Word/stem-level, not substring: a bare "x3" query must not
                    # treat files that merely contain "x3" inside a longer token
                    # (a hash, "max3...", a log copy) as strong topical matches.
                    if page is not None and _stem_tokens_present(page, query_norm):
                        strong.append(h)
                    else:
                        weak.append(h)
                reserve = min(len(outside), max(1, limit // 5), max(0, limit - 1))
                kb_keep = limit - reserve
                hits = ((strong + weak)[:kb_keep] + outside)[:limit]

    # Explicit recency window (off by default) — drop out-of-window hits last,
    # after auto-widen, so it governs every mode uniformly.
    with _span(timings, "date_filter"):
        hits = _filter_by_date(
            hits,
            updated_after=updated_after,
            updated_before=updated_before,
            recency_days=recency_days,
        )

    # ---- Hot cache store (deep copies both ways; bounded LRU eviction) ----
    # A result produced with warm-deferred lanes is lexical-only — caching it
    # would keep serving the degraded ranking after the warm completes. A
    # post-warm lane FAILURE (`failed`) is skipped for the same reason: the
    # failure may be transient, so don't pin a BM25-only result in the cache.
    if cache_key is not None and not degraded and not failed:
        with _FIND_CACHE_LOCK:
            _FIND_CACHE[cache_key] = copy.deepcopy(hits)
            _FIND_CACHE.move_to_end(cache_key)
            while len(_FIND_CACHE) > cache_size:
                _FIND_CACHE.popitem(last=False)
    return hits



def _collapse_frame_children(
    ranking: list[str],
    vault_root: Path,
    attribution: dict[str, tuple[str, float | None]],
    *aux_maps: dict,
) -> list[str]:
    """Compatibility wrapper for candidate-lane scene-frame collapsing."""
    return find_candidates.collapse_frame_children(
        ranking,
        vault_root,
        lambda rel: _CACHE.get(vault_root / rel, vault_root),
        attribution,
        *aux_maps,
    )


def _find_keyword(
    vault_root: Path,
    *,
    query_norm: str,
    types: list[str] | None,
    projects: list[str] | None,
    tags: list[str] | None,
    speakers: list[str] | None = None,
    file_types: list[str] | None = None,
    exclude_file_types: list[str] | None = None,
    limit: int,
    scope: str,
) -> list[Hit]:
    """Original keyword-mode find. Preserved for backward compat."""
    if scope == "kb":
        kb = vault_root / kb_dirname()
        if not kb.is_dir():
            log.error("KB directory missing: %s", kb)
            return []
        walk = _walk_md(kb)
    else:
        from .vault import walk_vault_md
        walk = walk_vault_md(vault_root)

    hits: list[tuple[str, Hit]] = []
    by_path: dict[str, Hit] = {}
    for path in walk:
        if path.name.lower() in _NAVIGATION_BASENAMES:
            continue
        page = _CACHE.get(path, vault_root)
        if page is None:
            continue
        excerpt = _make_excerpt(page, query_norm)
        if query_norm and excerpt is None:
            continue
        # A scene-frame child groups under its parent video: the parent becomes
        # the hit (carrying the matched frame + timestamp); an orphan frame
        # (parent gone) surfaces standalone. Filters apply to the EMITTED page.
        scene_frame: str | None = None
        scene_frame_ts: float | None = None
        if page.parent_media:
            parent_page = _CACHE.get(vault_root / (page.parent_media + ".md"), vault_root)
            if parent_page is not None:
                existing = by_path.get(parent_page.rel_path)
                if existing is not None:
                    if existing.scene_frame is None:
                        existing.scene_frame = page.media_file
                        existing.scene_frame_ts = page.frame_ts
                    continue
                scene_frame, scene_frame_ts = page.media_file, page.frame_ts
                page = parent_page
        if page.rel_path in by_path:
            continue
        if not _passes_filters(page, vault_root=vault_root, types=types, projects=projects, tags=tags, speakers=speakers,
                               file_types=file_types, exclude_file_types=exclude_file_types):
            continue
        hit = Hit(
            path=page.rel_path,
            type=page.page_type,
            scope=page.scope,
            title=page.title,
            updated=page.updated,
            excerpt=excerpt or "",
            media_type=page.media_type,
            media_file=page.media_file,
            status=page.status,
            superseded_by=page.superseded_by,
            scene_frame=scene_frame,
            scene_frame_ts=scene_frame_ts,
        )
        hit.transcript_ts = _transcript_ts_for_hit(page, None, query_norm)
        if hit.scene_frame is None and page.media_type == "video" and page.media_file and hit.transcript_ts is not None:
            from . import scene_frames  # lazy: keyword mode stays import-light
            nf = scene_frames.nearest_frame(vault_root, page.media_file, hit.transcript_ts)
            if nf is not None:
                hit.scene_frame, hit.scene_frame_ts = nf
        by_path[page.rel_path] = hit
        hits.append((page.updated or "0000-00-00", hit))

    hits.sort(key=lambda t: (t[0], t[1].path), reverse=True)
    return [h for _, h in hits[:limit]]


def _find_semantic(
    vault_root: Path,
    *,
    query: str,
    query_norm: str,
    types: list[str] | None,
    projects: list[str] | None,
    tags: list[str] | None,
    speakers: list[str] | None = None,
    file_types: list[str] | None = None,
    exclude_file_types: list[str] | None = None,
    limit: int,
    scope: str,
    mode: str,
    graph: bool = True,
    rerank: bool | None = False,
    auto_rerank: bool = False,
    temporal: bool = True,
    intent: str | None = None,
    prefer_compiled: bool = True,
    prefer_active: bool = True,
    prefer_used: bool = False,
    config: RankingConfig = DEFAULT_RANKING,
    timings: FindTimings | None = None,
    snapshot: FreshnessSnapshot | None = None,
    page_memo: dict[str, ParsedPage | None] | None = None,
    degraded_out: list[str] | None = None,
    failed_out: list[str] | None = None,
) -> list[Hit]:
    """Hybrid (BM25+vector) or vector-only mode.

    `failed_out` (distinct from `degraded_out`): a POST-WARM lane FAILURE — the
    vector or CLIP `except`, or the all-lanes-empty keyword fallback — appends
    the failed lane name here and bumps the process degradation counter. This is
    the "the lane broke and we silently served a weaker ranking" signal, versus
    `degraded_out`'s "the lane was deferred while a model preload is warming".
    """
    # Lazy imports — keep keyword-mode users out of the torch import path.
    from . import embeddings, readiness, scene_frames

    if snapshot is None:
        snapshot = FreshnessSnapshot(vault_root)
    if page_memo is None:
        page_memo = {}

    def _page_of(rel: str) -> ParsedPage | None:
        if rel not in page_memo:
            page_memo[rel] = _CACHE.get(vault_root / rel, vault_root)
        return page_memo[rel]

    bundle = find_candidates.collect_candidates(
        vault_root,
        query=query,
        query_norm=query_norm,
        limit=limit,
        scope=scope,
        mode=mode,
        graph=graph,
        temporal=temporal,
        intent=intent,
        prefer_compiled=prefer_compiled,
        prefer_active=prefer_active,
        prefer_used=prefer_used,
        config=config,
        timings=timings,
        snapshot=snapshot,
        page_of=_page_of,
        keyword_match_paths=_keyword_match_paths,
        outbound_wikilink_paths=_outbound_wikilink_paths,
        get_query_resolver=_get_query_resolver,
        record_degradation=_record_degradation,
        degraded_out=degraded_out,
        failed_out=failed_out,
    )

    if not bundle.had_rankings:
        # Both rankers failed or produced nothing. Degrade to keyword.
        log.info("semantic search produced no candidates; falling back to keyword")
        _record_degradation("no_candidates")
        if failed_out is not None:
            failed_out.append("keyword")
        return _find_keyword(
            vault_root,
            query_norm=query_norm,
            types=types, projects=projects, tags=tags, speakers=speakers,
            file_types=file_types, exclude_file_types=exclude_file_types,
            limit=limit, scope=scope,
        )

    fused = bundle.fused
    vector_ranking = bundle.vector_ranking
    bm25_ranking = bundle.bm25_ranking
    keyword_ranking = bundle.keyword_ranking
    clip_ranking = bundle.clip_ranking
    graph_ranking = bundle.graph_ranking
    chunk_text_by_path = bundle.chunk_text_by_path
    vector_score_by_path = bundle.vector_score_by_path
    clip_score_by_path = bundle.clip_score_by_path
    clip_frame_ts_by_path = bundle.clip_frame_ts_by_path
    frame_attribution = bundle.frame_attribution
    graph_in_degree_by_path = bundle.graph_in_degree_by_path
    graph_provenance_by_path = bundle.graph_provenance_by_path
    usage_map = bundle.usage_map

    # Pre-compute per-mode rank lookups so we can tag each Hit's signals.
    vector_rank_by_path = {p: i + 1 for i, p in enumerate(vector_ranking)}
    bm25_rank_by_path = {p: i + 1 for i, p in enumerate(bm25_ranking)}
    keyword_rank_by_path = {p: i + 1 for i, p in enumerate(keyword_ranking)}
    clip_rank_by_path = {p: i + 1 for i, p in enumerate(clip_ranking)}
    keyword_set: set[str] = set(keyword_ranking)
    clip_set: set[str] = set(clip_ranking)
    graph_set = set(graph_ranking)
    vector_paths: set[str] = set(vector_ranking)

    # Resolve fused paths back to ParsedPage, filter, build hits in fused order.
    # BM25-only candidates must still satisfy the keyword all-tokens-present
    # gate — without it, BM25's word-level tokenizer surfaces files that share
    # any single token with the query (false positives). Vector-ranked
    # candidates skip that gate by design: surfacing semantically-similar
    # files that don't contain the literal tokens is the whole point.
    # When reranking, we over-fetch then trim post-rerank. `rerank` may be
    # unset (None) with `auto_rerank` on — in that case we don't yet know
    # whether we'll rerank (should_rerank inspects the built hits), so over-fetch
    # whenever reranking is even possible.
    may_rerank = rerank is True or (rerank is None and auto_rerank)
    target_n = limit * 3 if may_rerank else limit
    hits: list[Hit] = []
    seen: set[str] = set()
    _filter_t0 = time.perf_counter()
    for rel_path, _score in fused:
        if rel_path in seen:
            continue
        seen.add(rel_path)
        if rel_path.rsplit("/", 1)[-1].lower() in _NAVIGATION_BASENAMES:
            continue
        page = _page_of(rel_path)
        if page is None:
            continue
        if not _passes_filters(page, vault_root=vault_root, types=types, projects=projects, tags=tags, speakers=speakers,
                               file_types=file_types, exclude_file_types=exclude_file_types):
            continue
        keyword_excerpt = _make_excerpt(page, query_norm)
        if (
            rel_path not in vector_paths
            and rel_path not in graph_set
            and rel_path not in keyword_set
            and rel_path not in clip_set
            and rel_path not in frame_attribution
            and keyword_excerpt is None
        ):
            # No literal match, not a graph hop, not vector-ranked, not in
            # the keyword scan. Try stem match before dropping — recovers
            # morphology ("regulation" matching a "regulator" page).
            if not _stem_tokens_present(page, query_norm):
                continue
            keyword_excerpt = _stem_anchored_excerpt(page, query_norm)
        elif (
            rel_path in graph_set
            or rel_path in clip_set
            or rel_path in frame_attribution
        ) and keyword_excerpt is None:
            # Graph-hop neighbour, CLIP visual match, or frame-collapsed parent:
            # no all-tokens-present requirement (the reason for surfacing is
            # connectivity / visual similarity / a child frame's text, not this
            # page's lexical overlap). Prefer the matched frame's OCR text as the
            # "why", else the sidecar's leading body.
            attr = frame_attribution.get(rel_path)
            if attr is not None:
                fpage = _CACHE.get(vault_root / (attr[0] + ".md"), vault_root)
                if fpage is not None:
                    keyword_excerpt = _make_excerpt(fpage, query_norm)
            if keyword_excerpt is None:
                body = page.body.strip()
                keyword_excerpt = _collapse(body[:EXCERPT_MAX_LEN]) if body else ""
        chunk = chunk_text_by_path.get(rel_path)
        excerpt = _semantic_excerpt(page, query_norm, chunk, keyword_excerpt)
        is_graph_only = (
            rel_path in graph_set
            and rel_path not in vector_rank_by_path
            and rel_path not in bm25_rank_by_path
        )
        hit_activation: float | None = None
        hit_usage_mult: float | None = None
        if usage_map:
            from . import usage as usage_module
            hit_activation = usage_map.get(usage_module.canon(rel_path))
            if hit_activation is not None:
                hit_usage_mult = usage_module.usage_multiplier(
                    hit_activation, config
                )
        attr = frame_attribution.get(rel_path)
        hit = Hit(
            path=page.rel_path,
            type=page.page_type,
            scope=page.scope,
            title=page.title,
            updated=page.updated,
            excerpt=excerpt or "",
            media_type=page.media_type,
            media_file=page.media_file,
            status=page.status,
            superseded_by=page.superseded_by,
            bm25_rank=bm25_rank_by_path.get(rel_path),
            vector_rank=vector_rank_by_path.get(rel_path),
            vector_score=vector_score_by_path.get(rel_path),
            clip_rank=clip_rank_by_path.get(rel_path),
            clip_score=clip_score_by_path.get(rel_path),
            clip_frame_ts=clip_frame_ts_by_path.get(rel_path),
            graph_hop=is_graph_only,
            graph_in_degree=graph_in_degree_by_path.get(rel_path, 0),
            graph_provenance=graph_provenance_by_path.get(rel_path),
            keyword_rank=keyword_rank_by_path.get(rel_path),
            activation=hit_activation,
            usage_boost_applied=hit_usage_mult,
            scene_frame=attr[0] if attr else None,
            scene_frame_ts=attr[1] if attr else None,
        )
        hit.transcript_ts = _transcript_ts_for_hit(page, chunk, query_norm)
        if hit.scene_frame is None and page.media_type == "video" and page.media_file:
            # A localized match on a video — CLIP keyframe first (existing), else a
            # timed-transcript match — attaches the nearest PERSISTED frame so the
            # moment is viewable, not just timestamped.
            anchor_ts = hit.clip_frame_ts if hit.clip_frame_ts is not None else hit.transcript_ts
            if anchor_ts is not None:
                nf = scene_frames.nearest_frame(vault_root, page.media_file, anchor_ts)
                if nf is not None:
                    hit.scene_frame, hit.scene_frame_ts = nf
        hits.append(hit)
        if len(hits) >= target_n:
            break
    if timings is not None:
        timings.stages.setdefault("filter_hits", {})["ms"] = round(
            (time.perf_counter() - _filter_t0) * 1000.0, 3
        )

    # Resolve the rerank decision. An explicit rerank=True/False always wins;
    # otherwise (rerank is None) auto_rerank consults should_rerank on the built
    # hits. Keeps the reranker model out of the default/test path.
    if rerank is None:
        do_rerank = auto_rerank and should_rerank(hits, query, config)
    else:
        do_rerank = rerank

    if do_rerank and not embeddings.ranking_enabled():
        do_rerank = False  # EXOMEM_DISABLE_RANKING — hard off, even for explicit rerank=True

    if do_rerank and hits and readiness.should_defer("reranker"):
        # Background warm-up owns the reranker load right now — calling
        # rerank_pairs would block on the singleton lock. Skip; caller marks
        # the response as warming.
        if degraded_out is not None:
            degraded_out.append("reranker")
        do_rerank = False

    if timings is not None and not (do_rerank and hits):
        timings.skipped("rerank")
    if do_rerank and hits:
        _rerank_t0 = time.perf_counter()
        try:
            from . import embeddings as emb
            # Best passage for each hit: the matched chunk when we have one,
            # else the leading body slice.
            passages: list[str] = []
            for h in hits:
                ctext = chunk_text_by_path.get(h.path)
                if ctext:
                    passages.append(ctext)
                else:
                    pg = _page_of(h.path)
                    body = (pg.body if pg else "") or h.excerpt
                    passages.append(body[:1500])  # CrossEncoder caps at 512 tokens
            scores = emb.rerank_pairs(query, passages)
            for h, s in zip(hits, scores):
                h.rerank_score = float(s)
            # Re-apply the type boost to rerank scores so prefer_compiled
            # survives the post-rerank sort. This rescues compiled material
            # that bge-reranker-base demotes — e.g. a "thoughts on..." query
            # where the reranker preferred raw Source discussion over
            # compiled Insights.
            if prefer_compiled:
                for h in hits:
                    if h.rerank_score is not None:
                        h.rerank_score *= _type_multiplier(h.type, config)
            # Re-apply the supersession demotion to rerank scores too, so a
            # superseded tombstone the reranker liked can't float back above
            # its successor in the final sort.
            if prefer_active:
                for h in hits:
                    if h.rerank_score is not None:
                        h.rerank_score *= _status_multiplier(h.status, config)
            # Re-apply the usage boost too, mirroring type/status, so an
            # opted-in boost survives the post-rerank sort.
            if usage_map:
                for h in hits:
                    if h.rerank_score is not None and h.usage_boost_applied:
                        h.rerank_score *= h.usage_boost_applied
            hits.sort(key=lambda h: -(h.rerank_score if h.rerank_score is not None else float("-inf")))
        except ImportError as e:
            log.warning("rerank requested but reranker unavailable: %s", e)
            if timings is not None:
                timings.error("rerank", e)
        except Exception as e:
            log.warning("rerank failed: %s; returning fused order", e)
            if timings is not None:
                timings.error("rerank", e)
        finally:
            if timings is not None:
                timings.stages.setdefault("rerank", {})["ms"] = round(
                    (time.perf_counter() - _rerank_t0) * 1000.0, 3
                )

    return hits[:limit]


def _find_outside_kb(
    vault_root: Path,
    *,
    query: str,
    query_norm: str,
    types: list[str] | None,
    projects: list[str] | None,
    tags: list[str] | None,
    speakers: list[str] | None = None,
    file_types: list[str] | None = None,
    exclude_file_types: list[str] | None = None,
    limit: int,
    snapshot: FreshnessSnapshot | None = None,
) -> list[Hit]:
    """BM25/keyword recall over the vault, RESTRICTED to paths outside
    `Knowledge Base/`. Powers scope="kb" auto-widening.

    Recall here is BM25-only (the vector lane already searches the WHOLE sidecar,
    so under `EXOMEM_INDEX_SCOPE=vault` out-of-KB notes surface semantically via
    that lane — this widener adds lexical out-of-KB recall on top), with a
    RELAXED gate: a candidate survives when at least one query stem is present,
    not the strict all-tokens-present gate the KB path enforces. Terse,
    frontmatter-less files (e.g. a numbers-heavy workout tracker) would
    otherwise be filtered out by any natural-language query that includes a
    word they don't literally contain.
    """
    if not query_norm or limit < 1:
        return []
    from . import bm25

    # Over-fetch: KB files dominate the corpus, so pull a generous slice then
    # filter to out-of-KB paths. Auto-widen only fires when the KB underfilled
    # — i.e. the query was already rare in the KB — so the out-of-KB target
    # won't be buried under hundreds of KB matches.
    bm25_k = max(limit * 5, 100)
    candidates: list[str] = []
    try:
        for path, _score in bm25.search(
            vault_root, query, k=bm25_k, scope="vault",
            freshness=snapshot.vault() if snapshot is not None else None,
        ):
            if not path.startswith(kb_prefix()):
                candidates.append(path)
    except ImportError:
        candidates = _outside_kb_keyword_paths(vault_root, query_norm)
    except Exception as e:  # noqa: BLE001 — widening must never break find
        log.warning("auto-widen BM25 failed: %s; falling back to keyword", e)
        candidates = _outside_kb_keyword_paths(vault_root, query_norm)

    hits: list[Hit] = []
    seen: set[str] = set()
    for rel_path in candidates:
        if rel_path in seen:
            continue
        seen.add(rel_path)
        if rel_path.rsplit("/", 1)[-1].lower() in _NAVIGATION_BASENAMES:
            continue
        page = _CACHE.get(vault_root / rel_path, vault_root)
        if page is None:
            continue
        if not _passes_filters(page, vault_root=vault_root, types=types, projects=projects, tags=tags, speakers=speakers,
                               file_types=file_types, exclude_file_types=exclude_file_types):
            continue
        # Relaxed gate: BM25 score>0 already implies a token match, but the
        # keyword fallback path needs this explicit check.
        if not _any_stem_present(page, query_norm):
            continue
        excerpt = _stem_anchored_excerpt(page, query_norm)
        hits.append(Hit(
            path=page.rel_path,
            type=page.page_type,
            scope=page.scope,
            title=page.title,
            updated=page.updated,
            excerpt=excerpt or "",
            media_type=page.media_type,
            media_file=page.media_file,
            status=page.status,
            superseded_by=page.superseded_by,
            outside_kb=True,
        ))
        if len(hits) >= limit:
            break
    return hits


def _any_stem_present(page: ParsedPage, query_norm: str) -> bool:
    """True if at least ONE query stem appears in title+body.

    The relaxed counterpart to `_stem_tokens_present` (which requires ALL).
    Tokenizes the query the SAME way BM25 tokenizes text (split on `[a-z0-9]+`,
    then stem) so a hyphenated query like `cognitive-core-marker-xyz` matches a
    body that contains those words split on the hyphens.
    """
    if not query_norm:
        return False
    from . import bm25 as bm25_module
    return any(qs in page.stem_set for qs in bm25_module.tokenize(query_norm))


def _outside_kb_keyword_paths(vault_root: Path, query_norm: str) -> list[str]:
    """BM25-unavailable fallback: walk vault .md outside Knowledge Base/, keep
    files where >=1 query stem is present, ordered most-recent first."""
    from .vault import walk_vault_md
    vault_resolved = vault_root.resolve()
    matches: list[tuple[str, str]] = []
    for path in walk_vault_md(vault_root):
        try:
            rel = path.resolve().relative_to(vault_resolved).as_posix()
        except ValueError:
            continue
        if rel.startswith(kb_prefix()):
            continue
        page = _CACHE.get(path, vault_root)
        if page is None:
            continue
        if _any_stem_present(page, query_norm):
            matches.append((page.updated or "0000-00-00", rel))
    matches.sort(reverse=True)
    return [p for _, p in matches]


# KB epistemic hierarchy: compiled distillations are the intentional output,
# raw sources are inputs. Surfaced via prefer_compiled=True post-RRF boost.
# Multipliers are small — designed as tie-breakers between similar fused
# scores, not as dominators. Tune in one place if needed.
_COMPILED_TYPES = find_policy.COMPILED_TYPES
_SOURCE_TYPES = find_policy.SOURCE_TYPES
_COMPILED_BOOST = find_policy.COMPILED_BOOST
_SOURCE_PENALTY = find_policy.SOURCE_PENALTY
_SUPERSEDED_PENALTY = find_policy.SUPERSEDED_PENALTY
_type_multiplier = find_policy.type_multiplier
_status_multiplier = find_policy.status_multiplier
_is_temporal_query = find_policy.is_temporal_query
_classify_intent = find_policy.classify_intent
_parse_date = find_policy.parse_date
_recency_multiplier = find_policy.recency_multiplier
_filter_by_date = find_policy.filter_by_date
should_rerank = find_policy.should_rerank


def _page_of(vault_root: Path):
    return lambda path: _CACHE.get(vault_root / path, vault_root)


def _apply_type_boost(
    fused: list[tuple[str, float]],
    vault_root: Path,
    config: RankingConfig = DEFAULT_RANKING,
) -> list[tuple[str, float]]:
    return find_policy.apply_type_boost(fused, _page_of(vault_root), config)


def _apply_status_demotion(
    fused: list[tuple[str, float]],
    vault_root: Path,
    config: RankingConfig = DEFAULT_RANKING,
) -> list[tuple[str, float]]:
    return find_policy.apply_status_demotion(fused, _page_of(vault_root), config)


def _apply_post_rrf_multipliers(
    fused: list[tuple[str, float]],
    query: str,
    config: RankingConfig,
    *,
    prefer_compiled: bool,
    prefer_active: bool,
    temporal: bool,
    page_of,
    usage_map: dict[str, float] | None = None,
) -> list[tuple[str, float]]:
    return find_policy.apply_post_rrf_multipliers(
        fused,
        query,
        config,
        prefer_compiled=prefer_compiled,
        prefer_active=prefer_active,
        temporal=temporal,
        page_of=page_of,
        usage_map=usage_map,
    )


def _apply_temporal_boost(
    fused: list[tuple[str, float]],
    vault_root: Path,
    query: str,
    config: RankingConfig = DEFAULT_RANKING,
) -> list[tuple[str, float]]:
    return find_policy.apply_temporal_boost(
        fused, query, _page_of(vault_root), config
    )


def _recency_ranking(
    candidate_paths: list[str], vault_root: Path, cap: int
) -> list[str]:
    return find_policy.recency_ranking(
        candidate_paths, _page_of(vault_root), cap
    )


def _keyword_match_paths(
    vault_root: Path, query_norm: str, scope: str, freshness: tuple | None = None
) -> list[str]:
    """Return paths that satisfy keyword mode's all-tokens-present gate.

    Sorted by `updated:` desc to mirror keyword-mode's ordering, so RRF's
    rank reflects keyword's own preference. Walks the same tree the keyword
    flow would, honors the navigation-file filter, and skips pages that
    can't be parsed.

    Backend ladder: the trigram index in the lexical sidecar serves the lane
    at posting-list cost when available; its gate is EXACT parity with this
    function's scan (the parity suite), so falling through changes nothing
    but latency. The scan below remains the reference implementation and the
    `EXOMEM_LEXICAL_BACKEND=python` target.
    """
    if not query_norm:
        return []
    from . import lexstore

    indexed = lexstore.search_substring(
        vault_root, query_norm, scope=scope, freshness=freshness
    )
    if indexed is not None:
        return indexed
    if scope == "kb":
        kb = vault_root / kb_dirname()
        if not kb.is_dir():
            return []
        walk = _walk_md(kb)
    else:
        from .vault import walk_vault_md
        walk = walk_vault_md(vault_root)
    matches: list[tuple[str, str]] = []  # (updated, rel_path)
    for path in walk:
        if path.name.lower() in _NAVIGATION_BASENAMES:
            continue
        page = _CACHE.get(path, vault_root)
        if page is None:
            continue
        if _make_excerpt(page, query_norm) is None:
            continue
        matches.append((page.updated or "0000-00-00", page.rel_path))
    matches.sort(reverse=True)  # most-recent first
    return [p for _, p in matches]


def _outbound_wikilink_paths(
    page: ParsedPage, vault_root: Path, resolver=None
) -> list[str]:
    """Vault-relative POSIX paths (no .md) that this page's body links to.

    Skips matches inside fenced code blocks and inline code (delegates to
    vault.find_body_wikilinks). Targets are normalised through
    `normalize_wikilink` so bare / KB-stripped / aliased forms all resolve to
    the same canonical path. Unresolvable targets and folder-hub links
    (trailing `/`) are dropped. `#anchor` is stripped — anchors are intra-
    page jumps, not separate files. Pass `resolver` to reuse one across a
    request (the graph lane does); None builds/reuses the process cache.
    """
    from .vault import (
        find_body_wikilinks,
        normalize_wikilink,
    )
    if resolver is None:
        resolver = _get_query_resolver(vault_root)
    out: list[str] = []
    seen: set[str] = set()
    for m in find_body_wikilinks(page.body):
        inner = m.group(0)[2:-2]
        target = inner.split("|", 1)[0].strip()
        if not target or target.endswith("/"):
            continue
        try:
            canonical, warning = normalize_wikilink(
                target, vault_root, resolver=resolver, strict=False
            )
        except Exception:
            continue
        if warning:
            continue  # unresolved — don't pollute the ranking
        rel = canonical.split("#", 1)[0].strip()
        if not rel:
            continue
        rel_with_md = rel if rel.endswith(".md") else rel + ".md"
        # Sanity: only walk into the KB itself for graph expansion; curated
        # trees are intentional out-of-graph references.
        if not rel_with_md.startswith(kb_prefix()):
            continue
        if rel_with_md in seen:
            continue
        seen.add(rel_with_md)
        out.append(rel_with_md)
    return out


_RESOLVER_CACHE: dict[Path, tuple[tuple, "object"]] = {}
_RESOLVER_LOCK = threading.Lock()


def _get_query_resolver(vault_root: Path, freshness: tuple | None = None):
    """Per-process WikilinkResolver cache, invalidated when the vault changes.

    Freshness is the digest-strength `_walk_freshness_key` triple — the old
    (count, max-mtime) pair missed pure renames, which change the resolver's
    stem/title maps without touching count or any mtime. Pass `freshness`
    (from the request's FreshnessSnapshot) to skip the walk; None computes it
    here for out-of-request callers.

    The build is serialized by a double-checked lock so the background warm
    thread and a racing request build the resolver once, not twice. Once built,
    the file watcher keeps it warm across vault edits via
    `on_resolver_files_changed` (incremental patch), so a single note change no
    longer forces a full-vault re-read + YAML reparse on the next graph-lane
    query — the ~14s-per-query cost this used to pay on a large, actively-synced
    vault (every edit moved the freshness digest and invalidated this cache).
    """
    from .vault import WikilinkResolver
    if freshness is None:
        freshness = FreshnessSnapshot(vault_root).vault()
    cached = _RESOLVER_CACHE.get(vault_root)
    if cached and cached[0] == freshness:
        return cached[1]
    with _RESOLVER_LOCK:
        cached = _RESOLVER_CACHE.get(vault_root)
        if cached and cached[0] == freshness:
            return cached[1]
        resolver = WikilinkResolver(vault_root)
        _RESOLVER_CACHE[vault_root] = (freshness, resolver)
    return resolver


def shared_resolver(vault_root: Path):
    """The process-shared, freshness-checked WikilinkResolver — for WRITERS.

    The same instance the graph lane uses (`_get_query_resolver`), exposed
    under a public name so write ops stop constructing a fresh
    `WikilinkResolver(vault_root)` per call — a full vault read + YAML parse
    that measured ~2.1s of a 4.6s note() on a ~1,900-file vault (cProfile,
    2026-07-04) and dominated every write tool's latency.

    Contract for writers:
    - `resolver.add_pending(...)` MAY be called for about-to-land paths; after
      the batch write, `index_sync.upsert_after_write` re-syncs those entries
      from disk (and restamps the freshness key, closing the async watcher
      window where the next query would miss the cache and rebuild).
    - A FAILED write must purge its pending registration via
      `on_resolver_files_changed(vault_root, [rel + ".md"], [])` — the file
      never landed, so the disk re-read drops the phantom entry.
    """
    return _get_query_resolver(vault_root)


def on_resolver_files_changed(
    vault_root: Path,
    changed_rels,
    deleted_rels,
) -> None:
    """Patch the process-cached wikilink resolver for one batch of changes.

    This is the resolver's arm of the event-maintained index family (it sits
    beside `freshness.on_files_changed` and `vault.on_inbound_files_changed`,
    and the file watcher calls all three for the same batch). Mirrors the
    inbound index:

    - **Live-only.** If no resolver is cached for this vault, this is a no-op —
      the next `_get_query_resolver` builds one from current disk state, so
      skipping here is correct, not just cheap. It only ever mutates an index
      that already exists.
    - **Re-syncs the freshness key.** After patching the maps in place it
      restamps the cache entry with the vault's current freshness triple, so
      the next graph-lane query sees a cache HIT instead of re-triggering a
      full-vault rebuild. Without this restamp the incremental patch would be
      pointless — the moved digest would still force a rebuild.

    Keyed on `vault_root` exactly like `_get_query_resolver`, so the watcher's
    patch and a request's lookup share one cache entry. No-op when the
    event-index kill switch is set (reverts to pure digest-keyed
    rebuild-on-change, matching freshness/inbound rollback).
    """
    if not freshness.event_indexes_enabled():
        return
    changed_list = list(changed_rels)
    deleted_list = list(deleted_rels)
    if not (changed_list or deleted_list):
        return
    with _RESOLVER_LOCK:
        cached = _RESOLVER_CACHE.get(vault_root)
        if cached is None:
            return
        _, resolver = cached
        resolver.on_files_changed(vault_root, changed_list, deleted_list)
        _RESOLVER_CACHE[vault_root] = (FreshnessSnapshot(vault_root).vault(), resolver)



def unload_ram_caches() -> dict[str, int]:
    """Evict rebuildable find RAM caches without clearing freshness/inbound metadata."""
    page_entries = len(_CACHE.entries)
    _CACHE.clear()
    with _RESOLVER_LOCK:
        resolver_entries = len(_RESOLVER_CACHE)
        _RESOLVER_CACHE.clear()
    with _FIND_CACHE_LOCK:
        hot_entries = len(_FIND_CACHE)
        _FIND_CACHE.clear()
    return {"pages": page_entries, "resolvers": resolver_entries, "hot_find": hot_entries}


def cache_status() -> dict:
    """No-allocation residency status for find's rebuildable RAM caches."""
    page_entries = list(_CACHE.entries.values())
    with _RESOLVER_LOCK:
        resolver_entries = len(_RESOLVER_CACHE)
    with _FIND_CACHE_LOCK:
        hot_entries = len(_FIND_CACHE)
        hot_hits = sum(len(v) for v in _FIND_CACHE.values())
    return {
        "pages": {
            "entries": len(page_entries),
            "body_chars": sum(len(p.body) for p in page_entries),
        },
        "resolvers": {"entries": resolver_entries},
        "hot_find": {"entries": hot_entries, "hits": hot_hits},
    }


def clear_cache() -> None:
    """Test hook: flush every in-process find cache between tests — parsed
    pages, the wikilink resolver, the hot find-result cache, and the vault
    inbound-link index."""
    unload_ram_caches()
    freshness.clear()
    from . import vault as vault_module
    vault_module.clear_inbound_index()
