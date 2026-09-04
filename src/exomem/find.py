"""Read-only search across the Knowledge Base.

Scans every `.md` under `Knowledge Base/`, parses YAML frontmatter, filters by
structured fields, then does case-insensitive substring matching on
title + body. A typical vault is hundreds of pages — full-scan is fast enough.

Cached in-process between calls: keyed by file path, invalidated by mtime.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from collections.abc import Set as AbstractSet
from datetime import date
from pathlib import Path
from typing import Any

from . import (
    cli_ops,
    find_candidates,
    find_corpus,
    find_policy,
    find_results,
    find_types,
    freshness,
    recall_policy,
    runtime_resources,
    structured_filters,
)
from . import ranking_config as _ranking_config
from .find_types import (
    FindTimings,
    Hit,
    ParsedPage,
    SemanticUnitHit,
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
_mark_source = find_types.timing_mark_source
_nested_name = find_types.timing_nested_name

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

# Bounded-staleness disclosure. A request answered from the catalog's last
# published recall projection — rather than one proven exactly current — rides
# the existing `warming.components` envelope under this name, so a caller can
# tell "these hits may not include the newest writes" from a fully current
# answer without a new envelope field.
_RECALL_PROJECTION_STALE_COMPONENT = "recall_projection"

# Pending-derived-work disclosure. A request answered while exact durable
# receipts still cover part of the corpus is exact for direct, stable-ref,
# keyword and hybrid recall, but the vector and graph lanes may legitimately
# omit the pending generation (they contribute only proven-current ones). That
# limitation rides the same `warming.components` envelope, so the caller can
# tell a fully converged answer from one whose slower projections are behind
# without a new envelope field.
_PENDING_VISIBILITY_COMPONENT = "pending_visibility"


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

        return _accelerated_device(accel.select_device(override_env="EXOMEM_EMBED_DEVICE"))
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
_FIND_CACHE_CHECKPOINTS: dict[
    tuple, tuple[tuple[str, freshness.RecallFreshnessCheckpoint], ...]
] = {}
_DEFAULT_FIND_CACHE_SIZE = 32
# A request gets a new FreshnessSnapshot, but a live recall projection changes
# only when its exact checkpoint moves.  Rebuilding the vault-relative allowset
# from every absolute registry key on every request is O(corpus) and defeats the
# event-maintained registry.  Keep only the latest immutable projection per
# bounded vault/scope entry; checkpoint equality is the validity proof.
_RECALL_PATH_CACHE: OrderedDict[
    tuple[Path, str], tuple[freshness.RecallFreshnessCheckpoint, frozenset[str]]
] = OrderedDict()
_RECALL_PATH_CACHE_LOCK = threading.Lock()
_RECALL_PATH_CACHE_SIZE = 32
MAX_RERANK_CANDIDATES = 300
_FOREGROUND_LEXICAL_REPAIR_PAGE_CAP = 64
_FIND_CACHE_DELTA_PATH_CAP = 64


def _bounded_lexical_repair_allowed(freshness_key: tuple | None) -> bool:
    """Permit a cold inline sidecar build only for a provably small corpus.

    Never for a MANAGED reader, whatever the corpus size. A page cap bounds how
    long the walk takes; it does not stop it being a walk, and the read-path
    contract is about whether the reader thread built an index rather than
    about how long it took. A managed cell has a repair worker to hand the
    build to and a typed warming outcome to answer with; an offline caller has
    neither, so it keeps the bounded inline build.
    """
    from . import readiness

    if readiness.runtime_managed():
        return False
    return bool(
        freshness_key
        and isinstance(freshness_key[0], int)
        and freshness_key[0] <= _FOREGROUND_LEXICAL_REPAIR_PAGE_CAP
    )


def _set_rerank_timing_profile(
    timings: FindTimings | None,
    *,
    requested: int | None,
    effective: int,
    scorer_input_count: int,
    unscored_tail_count: int,
    decision: str,
    reason: str,
) -> None:
    """Record bounded-reranker diagnostics only for timing-enabled requests."""
    if timings is None:
        return
    timings.profile["rerank"] = {
        "candidate_limit_requested": requested,
        "candidate_limit_effective": effective,
        "candidate_limit_hard_max": MAX_RERANK_CANDIDATES,
        "scorer_input_count": scorer_input_count,
        "unscored_tail_count": unscored_tail_count,
        "decision": decision,
        "reason": reason,
    }


def _set_catalog_timing_profile(
    timings: FindTimings | None,
    readiness: Any | None = None,
    *,
    cache_hit: bool = False,
) -> None:
    if timings is None:
        return
    from . import lexstore

    timings.profile["catalog"] = lexstore.catalog_timing_profile(readiness, cache_hit=cache_hit)


#: How the recall-projection outcome maps onto the stage source vocabulary.
#: `offline_fallback` is `computed` on purpose: that branch rebuilds the
#: projection from a scope snapshot, which walks. Calling it `index` would let
#: the one stage that CAN still walk describe itself as index-backed.
_RECALL_PROJECTION_SOURCES = {
    "cache": find_types.SOURCE_CACHE,
    "live_cache": find_types.SOURCE_CACHE,
    "live": find_types.SOURCE_INDEX,
    "admitted": find_types.SOURCE_INDEX,
    "offline": find_types.SOURCE_COMPUTED,
    "offline_fallback": find_types.SOURCE_COMPUTED,
    "warming": find_types.SOURCE_DECLINED,
    "unavailable": find_types.SOURCE_DECLINED,
    "pending_unavailable": find_types.SOURCE_DECLINED,
}


def _set_recall_projection_timing_outcome(
    timings: FindTimings | None,
    outcome: str,
) -> None:
    if timings is not None:
        timings.profile.setdefault("recall_projection", {})["outcome"] = outcome
        source = _RECALL_PROJECTION_SOURCES.get(outcome)
        if source is not None:
            # The projection stage reports under a name qualified by whatever
            # contains it, so the source has to reach the span that is actually
            # open. The `pending_visibility` site is the exception: it reports a
            # projection outcome from inside a different stage, and must not
            # relabel that stage.
            open_stage = timings.current_stage()
            if open_stage is None or not open_stage.endswith("recall_projection"):
                open_stage = "recall_projection"
            timings.mark_source(open_stage, source)


def _record_filter_eligibility_cache_hit(timings: FindTimings | None) -> None:
    """Record the exact-catalog hot lane as the (near-zero) interval it is.

    This used to write the stage straight into the table, which is exactly the
    shape `unattributed_ms` double-counts. The lane genuinely costs almost
    nothing, so registering its real interval keeps the number honest without
    inventing clock noise, and the source says why it was free.
    """
    with _span(timings, "filter_eligibility", source=find_types.SOURCE_CACHE, cache_hit=True):
        pass


def _order_reranked_prefix(hits: list[Any], *, prefix_count: int) -> list[Any]:
    """Sort only the scored prefix, preserving the fused tail byte-for-byte."""
    prefix = hits[:prefix_count]
    prefix.sort(
        key=lambda hit: -(hit.rerank_score if hit.rerank_score is not None else float("-inf"))
    )
    return prefix + hits[prefix_count:]


def _find_cache_size() -> int:
    raw = os.environ.get("EXOMEM_FIND_CACHE_SIZE")
    if raw is None or not raw.strip():
        return _DEFAULT_FIND_CACHE_SIZE
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning("EXOMEM_FIND_CACHE_SIZE=%r is not an int; using default", raw)
        return _DEFAULT_FIND_CACHE_SIZE


def _trim_find_cache(size: int) -> None:
    """Trim the hot cache and its delta-validation metadata together.

    Caller holds ``_FIND_CACHE_LOCK``.
    """
    while len(_FIND_CACHE) > size:
        evicted, _hits = _FIND_CACHE.popitem(last=False)
        _FIND_CACHE_CHECKPOINTS.pop(evicted, None)


def _recall_checkpoints_for_cache(
    snapshot: FreshnessSnapshot,
    *,
    scope: str,
    query_norm: str,
) -> tuple[tuple[str, freshness.RecallFreshnessCheckpoint], ...]:
    scopes = ["kb"] if scope in ("kb", "kb-only") else []
    if scope == "vault" or (scope == "kb" and query_norm):
        scopes.append("vault")
    return tuple((item, snapshot.recall_checkpoint(item)) for item in scopes)


def _freshness_correctness_fences(key: tuple) -> tuple:
    """Return the all-or-nothing parts of a query freshness key.

    Recall projections and the keyword catalog may be advanced by the same
    bounded markdown delta. Everything else remains an unconditional fence:
    date, access policy, semantic registries, and semantic/graph sidecars.
    """
    delta_backed = {"kb", "vault", "lexical"}
    return tuple(
        part
        for part in key
        if not (
            isinstance(part, tuple)
            and part
            and isinstance(part[0], str)
            and part[0] in delta_backed
        )
    )


def _keyword_cache_delta_is_safe(
    vault_root: Path,
    *,
    query_norm: str,
    cached_hits: list[Hit],
    cached_freshness: tuple,
    current_freshness: tuple,
    checkpoints: tuple[tuple[str, freshness.RecallFreshnessCheckpoint], ...],
    snapshot: FreshnessSnapshot,
) -> tuple[bool, tuple[tuple[str, freshness.RecallFreshnessCheckpoint], ...]]:
    """Prove that a bounded recall delta cannot alter one keyword answer.

    This deliberately supports only the exact keyword contract: all query
    tokens must occur in a page and matches are ordered by page ``updated``.
    A changed page already in the answer, any deletion, any newly matching
    page, a scene-frame dependency, an incomplete delta, or a changed global
    correctness fence misses. Hybrid/vector results retain their sidecar and
    whole-projection invalidation because a textually unrelated page may still
    move semantic or corpus-wide ranking.
    """
    if _freshness_correctness_fences(cached_freshness) != _freshness_correctness_fences(
        current_freshness
    ):
        return False, ()
    if freshness.external_pending(vault_root):
        return False, ()

    changed: set[str] = set()
    deleted: set[str] = set()
    target_signatures: dict[str, freshness.FileSignature] = {}
    advanced: list[tuple[str, freshness.RecallFreshnessCheckpoint]] = []
    for scope, checkpoint in checkpoints:
        current_checkpoint = snapshot.recall_checkpoint(scope)
        delta = freshness.recall_delta_since(vault_root, scope, checkpoint)
        if not delta.complete or delta.to != current_checkpoint:
            return False, ()
        changed.update(delta.changed)
        deleted.update(delta.deleted)
        if len(changed) + len(deleted) > _FIND_CACHE_DELTA_PATH_CAP:
            return False, ()
        for path, signature in delta.target_signatures:
            prior = target_signatures.get(path)
            if prior is not None and prior != signature:
                return False, ()
            target_signatures[path] = signature
        advanced.append((scope, delta.to))

    # A catalog-only generation change has no recall delta proving what moved.
    # Keep the in-band sidecar token as the unconditional fallback in that case.
    if not changed and not deleted:
        return False, ()
    # Deleted bytes cannot be inspected for a former scene-frame or other
    # dependency, so deletion remains conservative.
    if deleted:
        return False, ()

    root = vault_root.absolute()
    cached_paths = {hit.path for hit in cached_hits}
    for raw_path in changed:
        expected = target_signatures.get(raw_path)
        if expected is None:
            return False, ()
        path = Path(raw_path)
        try:
            rel_path = path.absolute().relative_to(root).as_posix()
            if freshness.stat_signature(path) != expected:
                return False, ()
        except (OSError, ValueError):
            return False, ()
        if rel_path in cached_paths:
            return False, ()
        if any(part.lower().endswith(".frames") for part in Path(rel_path).parts[:-1]):
            return False, ()
        if path.name.lower() in _NAVIGATION_BASENAMES:
            continue
        if not recall_policy.is_recall_candidate(vault_root, path):
            continue
        page = _CACHE.get(path, vault_root)
        try:
            if freshness.stat_signature(path) != expected:
                return False, ()
        except OSError:
            return False, ()
        if page is None:
            continue
        if page.parent_media or _make_excerpt(page, query_norm) is not None:
            return False, ()
    if freshness.external_pending(vault_root):
        return False, ()
    for scope, checkpoint in advanced:
        if freshness.recall_checkpoint(vault_root, scope) != checkpoint:
            return False, ()
    return True, tuple(advanced)


class FreshnessSnapshot:
    """Per-request corpus freshness: each markdown scope is resolved at most
    once per `find()` call, and every consumer (hot-cache key, BM25 rebuild
    check, wikilink-resolver reuse, auto-widen's vault BM25) shares the result
    instead of recomputing. Lazy — a `scope="kb-only"` request never pays the
    vault cost.

    Reads the event-maintained `freshness` registry when it is live for the
    scope (sub-ms, syscall-free); otherwise falls back to a full stat-walk that
    yields a byte-identical triple."""

    def __init__(
        self,
        vault_root: Path,
        *,
        require_live_recall: bool = False,
        timings: FindTimings | None = None,
        expected_recall_checkpoints: dict[
            str, freshness.RecallFreshnessCheckpoint
        ]
        | None = None,
        stale_recall_scopes: frozenset[str] = frozenset(),
        pending: Any | None = None,
    ) -> None:
        self._root = vault_root
        self._require_live_recall = require_live_recall
        self._timings = timings
        # Exact pending custody this request may serve from. It is part of the
        # request's projection, not of the maintained registry: a committed page
        # whose derived components have not converged is admitted here, and a
        # committed tombstone is withdrawn here, before any lane scores.
        self._pending = pending
        self._expected_recall_checkpoints = expected_recall_checkpoints or {}
        # Scopes admitted at the catalog's published projection rather than an
        # exactly-current live one. For those the live registry has no
        # checkpoint to require, so liveness is not a precondition and the
        # published projection is the answer.
        self._stale_recall_scopes = stale_recall_scopes
        self._kb: tuple[int, int, str] | None = None
        self._vault: tuple[int, int, str] | None = None
        self._recall: dict[str, freshness.RecallFreshnessCheckpoint] = {}
        self._recall_paths: dict[str, frozenset[str]] = {}

    @property
    def requires_live_recall(self) -> bool:
        return self._require_live_recall

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
        """Projected freshness for ordinary recall only.

        Generic ``kb``/``vault`` freshness above remains the identity source for
        resolver and inbound-link consumers.  Recall ignores raw Records edits
        and binds every cache/sidecar key to the policy identity.
        """
        return self.recall_checkpoint(scope).triple

    def recall_checkpoint(self, scope: str) -> freshness.RecallFreshnessCheckpoint:
        checkpoint = self._recall.get(scope)
        if checkpoint is None:
            self._load_recall_projection(scope)
            checkpoint = self._recall[scope]
        return checkpoint

    def projection_key(self, scope: str) -> tuple:
        checkpoint = self.recall_checkpoint(scope)
        return (
            checkpoint.triple,
            checkpoint.policy_version,
            checkpoint.access_policy_fingerprint,
        )

    def recall_paths(self, scope: str) -> frozenset[str]:
        """Stable request-local ordinary-recall path projection.

        Vector and visual KNN must receive this set before scoring: filtering a
        ranked raw Records window afterwards can starve admitted pages.  A live
        freshness registry supplies it without a walk; CLI/cold callers rebuild
        the same projection from the human-owned Markdown files.
        """
        cached = self._recall_paths.get(scope)
        if cached is None:
            self._load_recall_projection(scope)
            cached = self._recall_paths[scope]
        return self._with_pending(cached)

    def _with_pending(self, projected: frozenset[str]) -> frozenset[str]:
        """Admit committed pending identities and withdraw committed tombstones.

        The maintained registry describes the last projection the watcher
        published. A page committed since then is proven by exact durable
        custody instead, so it belongs in this request's projection; a path the
        same custody tombstones does not, however the registry still lists it.
        """
        pending = self._pending
        if pending is None or pending.empty:
            return projected
        admitted = {
            rel_path
            for rel_path in pending.current_paths()
            if recall_policy.is_recall_candidate(self._root, self._root / rel_path)
        }
        return (projected | admitted) - pending.tombstoned_paths()

    def _load_recall_projection(self, scope: str) -> None:
        if scope in self._recall and scope in self._recall_paths:
            return
        # Reached at three depths — top level, inside `freshness`, and inside
        # `graph.resolver` — so it reports under a name qualified by whatever
        # holds it. A single shared name accumulated all three into one scalar
        # that was simultaneously a root stage and nested inside two others.
        stage = _nested_name(self._timings, "recall_projection")
        with _span(self._timings, stage):
            try:
                root = self._root.absolute()
                cache_key = (root, scope)
                # A scope admitted as stale is answered from the published
                # projection the admission already identity-checked, so it does
                # not additionally require a live registry.
                stale = scope in self._stale_recall_scopes
                require_live = self._require_live_recall and not stale
                projection_live = freshness.recall_is_live(self._root, scope)
                checkpoint = (
                    freshness.live_recall_checkpoint(self._root, scope)
                    if require_live
                    else freshness.recall_checkpoint(self._root, scope)
                    if projection_live
                    else None
                )
                if checkpoint is None and require_live:
                    raise freshness.RecallProjectionUnavailable(
                        f"maintained recall projection is not live for scope={scope!r}"
                    )
                expected = self._expected_recall_checkpoints.get(scope)
                if (
                    not stale
                    and checkpoint is not None
                    and expected is not None
                    and checkpoint != expected
                ):
                    raise freshness.RecallProjectionUnavailable(
                        f"maintained recall projection advanced for scope={scope!r}"
                    )
                if checkpoint is not None:
                    with _RECALL_PATH_CACHE_LOCK:
                        cached = _RECALL_PATH_CACHE.get(cache_key)
                        if cached is not None and cached[0] == checkpoint:
                            _RECALL_PATH_CACHE.move_to_end(cache_key)
                            self._recall[scope] = checkpoint
                            self._recall_paths[scope] = cached[1]
                            _set_recall_projection_timing_outcome(
                                self._timings,
                                # `require_live`, not the request-wide flag: a
                                # stale-admitted scope is served from the
                                # published projection, so labelling its cache
                                # hit `live_cache` would misreport it as an
                                # exactly-current answer.
                                "live_cache" if require_live else "cache",
                            )
                            return

                if require_live:
                    checkpoint, entries = freshness.recall_projection_snapshot(
                        self._root,
                        scope,
                        allow_fallback=False,
                    )
                elif projection_live and freshness.recall_is_live(self._root, scope):
                    checkpoint, entries = freshness.recall_projection_snapshot(
                        self._root,
                        scope,
                    )
                else:
                    checkpoint, entries, scope_triple = (
                        freshness.recall_projection_scope_snapshot(self._root, scope)
                    )
                    if scope == "vault":
                        self._vault = scope_triple
                    else:
                        self._kb = scope_triple
                if not stale and expected is not None and checkpoint != expected:
                    raise freshness.RecallProjectionUnavailable(
                        f"maintained recall projection advanced for scope={scope!r}"
                    )
                paths: set[str] = set()
                for raw_path in entries:
                    try:
                        paths.add(Path(raw_path).absolute().relative_to(root).as_posix())
                    except (OSError, ValueError):
                        # Registry identities outside the literal vault spelling are
                        # never valid semantic-search parents; fail closed.
                        continue
                projected = frozenset(paths)
                self._recall[scope] = checkpoint
                self._recall_paths[scope] = projected
                with _RECALL_PATH_CACHE_LOCK:
                    _RECALL_PATH_CACHE[cache_key] = (checkpoint, projected)
                    _RECALL_PATH_CACHE.move_to_end(cache_key)
                    while len(_RECALL_PATH_CACHE) > _RECALL_PATH_CACHE_SIZE:
                        _RECALL_PATH_CACHE.popitem(last=False)
                _set_recall_projection_timing_outcome(
                    self._timings,
                    (
                        "live"
                        if self._require_live_recall
                        or freshness.recall_is_live(self._root, scope)
                        else "offline_fallback"
                    ),
                )
            except freshness.RecallProjectionUnavailable as exc:
                _set_recall_projection_timing_outcome(self._timings, "unavailable")
                raise RetrievalIndexWarming(
                    site="projection_unavailable",
                    status="temporarily_unavailable",
                ) from exc


class _EmptyPendingCoverage:
    """The no-custody projection an offline caller proceeds under.

    Structurally identical to a ready, empty overlay: it shadows nothing, merges
    nothing, and leaves every existing lane exactly as it was.
    """

    ready = True
    empty = True
    rows: dict[str, Any] = {}

    @staticmethod
    def covers(_rel_path: str) -> bool:
        return False

    @staticmethod
    def shadow(rel_paths: Iterable[str]) -> list[str]:
        return list(rel_paths)

    @staticmethod
    def page(_rel_path: str) -> ParsedPage | None:
        return None

    @staticmethod
    def current_pages() -> tuple:
        return ()

    @staticmethod
    def current_paths() -> frozenset[str]:
        return frozenset()

    @staticmethod
    def tombstoned_paths() -> frozenset[str]:
        return frozenset()


_EMPTY_PENDING_COVERAGE = _EmptyPendingCoverage()


def _vault_rel(vault_root: Path, path: Path) -> str | None:
    """Vault-relative POSIX identity for a walked candidate, or None.

    Delegates to the one helper the pending overlay, `lexstore` and
    `memory_refs` also normalize through, so a candidate and the custody that
    shadows it can never be keyed by two different spellings of one file.
    """
    from . import pending_recall

    return pending_recall.vault_rel_path(vault_root, path)


def _resolve_page(
    vault_root: Path,
    rel: str,
    pending: Any | None = None,
) -> ParsedPage | None:
    """Hydrate one candidate identity under the current request's policy.

    An identity under pending custody is served from the exact generation the
    receipt proved, never from an unproven re-read: the overlay's page is the
    only one whose after hash was checked against the receipt. Recall admission
    runs here, at request time, so a pending page inherits no earlier disclosure
    decision and a pending tombstone hydrates as absence.
    """
    target = vault_root / rel
    if not recall_policy.is_recall_candidate(vault_root, target):
        return None
    if pending is not None and pending.covers(rel):
        return pending.page(rel)
    return _CACHE.get(target, vault_root)


def _without_shadowed(by_path: dict, pending: Any) -> dict:
    """Drop per-path persistent-lane evidence a pending identity shadows."""
    return {
        rel_path: value
        for rel_path, value in by_path.items()
        if not pending.covers(rel_path)
    }


def _merge_pending_walk(
    vault_root: Path,
    walk: Iterable[Path],
    *,
    pending: Any,
    scope: str,
) -> Iterable[Path]:
    """Yield a scope walk with pending tombstones withdrawn and creates admitted.

    Bounded by the overlay: the walk itself is unchanged, and at most one extra
    candidate per pending identity is appended.
    """
    seen: set[str] = set()
    for path in walk:
        rel_path = _vault_rel(vault_root, path)
        if rel_path is not None:
            if pending.covers(rel_path):
                # The overlay owns this identity; it is re-offered below only if
                # its committed generation still exists.
                continue
            seen.add(rel_path)
        yield path
    prefix = kb_prefix()
    for row in pending.current_pages():
        if row.rel_path in seen:
            continue
        if scope != "vault" and not row.rel_path.startswith(prefix):
            continue
        yield vault_root / row.rel_path


def _pending_keyword_paths(
    vault_root: Path,
    *,
    pending: Any,
    query_norm: str,
    scope: str,
) -> list[str]:
    """Pending identities the keyword contract admits, most recent first.

    The lane's own ordering contract is `updated` descending, and a pending page
    is by construction the newest committed generation of its identity, so the
    merged rows lead. Every gate an ordinary candidate passes -- navigation
    exclusion, scope membership, current recall admission and the
    all-tokens-present excerpt -- is applied here, before the merge reaches
    scoring or any cap.
    """
    admitted: list[str] = []
    prefix = kb_prefix()
    for row in pending.current_pages():
        rel = row.rel_path
        if scope != "vault" and not rel.startswith(prefix):
            continue
        if rel.rsplit("/", 1)[-1].lower() in _NAVIGATION_BASENAMES:
            continue
        if not recall_policy.is_recall_candidate(vault_root, vault_root / rel):
            continue
        if query_norm and _make_excerpt(row.page, query_norm) is None:
            continue
        admitted.append(rel)
    return admitted


def _freshness_key(
    vault_root: Path,
    *,
    scope: str,
    query_norm: str,
    mode: str,
    graph: bool,
    snapshot: FreshnessSnapshot,
    unit_filters: bool = False,
    metadata_only_catalog: bool = False,
    relation_filter: bool = False,
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

    Plain keyword entries may advance across a complete, bounded recall delta
    only after ``_keyword_cache_delta_is_safe`` proves that every changed page is
    non-matching. The full key remains the fallback and every global fence above
    remains unconditional.
    """
    from . import access

    parts: list[Any] = [
        date.today().toordinal(),
        ("access_policy", access.policy_fingerprint(vault_root)),
    ]
    if unit_filters:
        from . import semantic_language_registry

        language = semantic_language_registry.load_registry(vault_root)
        parts.append(
            (
                "semantic_language_registry",
                language.schema_version,
                language.content_hash,
            )
        )
    if unit_filters or relation_filter:
        # A relation filter resolves participants against the relation registry
        # (canonicalization + parent roll-up), so its identity must gate the
        # cache in every mode — not only when a unit predicate is present.
        from . import relation_registry

        relations = relation_registry.load_registry(vault_root)
        parts.append(
            (
                "relation_registry",
                relations.core_version,
                relations.extension_hash,
            )
        )
    if scope in ("kb", "kb-only"):
        parts.append(("kb", *snapshot.projection_key("kb")))
    if scope == "vault" or (scope == "kb" and query_norm):
        parts.append(("vault", *snapshot.projection_key("vault")))
    if mode in ("hybrid", "vector"):
        from . import embeddings

        parts.append((".embeddings.sqlite", embeddings.EmbeddingIndex.cache_token(vault_root)))
        parts.append((".clip.sqlite", embeddings.ClipIndex.cache_token(vault_root)))
    # The typed graph lane can re-rank on sidecar content (hybrid/vector + graph),
    # and a relation filter resolves participants against the same sidecar, so its
    # in-band generation token joins the key whenever either is active — in every
    # mode for a relation filter. Absent sentinel when the sidecar is unavailable
    # keeps typed-mode and fallback-mode entries from colliding; never the sidecar
    # mtime, which a WAL checkpoint moves without a content change.
    if (mode in ("hybrid", "vector") and graph) or relation_filter:
        from . import epistemic_graph

        parts.append((".graph.sqlite", epistemic_graph.cache_token(vault_root) or "absent"))
    if mode in ("hybrid", "keyword"):
        # Which lexical backend serves (fts5 vs python) changes bm25-lane
        # scores, so a mid-process flip must not hit entries cached under the
        # other scorer. Index CONTENT changes always ride the walk triples
        # above; lexstore.cache_token explains why the sidecar's file mtime
        # is deliberately not used here.
        from . import lexstore

        token = (
            lexstore.catalog_cache_token(vault_root)
            if metadata_only_catalog
            else lexstore.cache_token(vault_root)
        )
        parts.append(("lexical", token))
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
    categories: list[str] | None = None,
    kinds: list[str] | None = None,
    source_kinds: list[str] | None = None,
    domains: list[str] | None = None,
    relations: list[str] | None = None,
    relation_of: str | None = None,
    relation_direction: str = "any",
    filters: dict[str, Any] | None = None,
    result_level: str = "auto",
    limit: int = 15,
    scope: str = "kb",
    mode: str = "hybrid",
    graph: bool = True,
    rerank: bool | None = None,
    rerank_max_candidates: int | None = None,
    auto_rerank: bool = False,
    temporal: bool = True,
    intent: str | None = None,
    updated_after: str | None = None,
    updated_before: str | None = None,
    recency_days: int | None = None,
    prefer_compiled: bool = True,
    prefer_active: bool = True,
    prefer_used: bool = False,
    widen_outside_kb: bool = False,
    config: RankingConfig | None = None,
    timings: FindTimings | None = None,
    degraded_out: list[str] | None = None,
    failed_out: list[str] | None = None,
    retrieval_trace: Any | None = None,
    catalog_proof_out: dict[str, freshness.RecallFreshnessCheckpoint] | None = None,
) -> list[Hit] | list[SemanticUnitHit] | list[Hit | SemanticUnitHit]:
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
    on/off; `None` (default) defers to `auto_rerank`. When on, runs a bounded
    prefix of the top `3 * limit` fused candidates through the reranker and
    re-sorts that prefix by reranker score. Off by default to keep the model
    out of the common path.

    `rerank_max_candidates`: optional caller-selected bound on the reranker
    prefix. Must be an integer from the effective result `limit` through 300.
    Omit it to preserve the existing `3 * limit` prefix. This bounds scorer
    input count, not wall-clock time: the synchronous model call has no safe
    cancellation boundary.

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

    `widen_outside_kb`: when True (OFF by default), `scope="kb"` reserves up
    to `limit - 1` slots for pages OUTSIDE `Knowledge Base/` (see the reserve
    note at the widening call site). Off, `scope="kb"` serves the knowledge
    base and nothing else. The reserve used to run on every default recall and
    cost the reader a whole-corpus lexical pass, so it is now something a
    caller asks for. A managed reader serves it from the maintained catalogue
    over the index-resolved out-of-KB eligible set, and declines rather than
    scanning when the catalogue cannot answer.
    """
    if catalog_proof_out is not None:
        catalog_proof_out.clear()
    if scope not in ("kb", "vault", "kb-only"):
        raise ValueError(f"find: scope must be 'kb', 'vault', or 'kb-only', got {scope!r}")
    if mode not in ("hybrid", "keyword", "vector"):
        raise ValueError(f"find: mode must be 'hybrid', 'keyword', or 'vector', got {mode!r}")
    if limit < 1:
        limit = 1
    limit = min(limit, 100)
    if rerank_max_candidates is not None and (
        isinstance(rerank_max_candidates, bool) or not isinstance(rerank_max_candidates, int)
    ):
        raise ValueError(
            "find: rerank_max_candidates must be an integer "
            f"from {limit} to {MAX_RERANK_CANDIDATES}, got "
            f"{rerank_max_candidates!r}"
        )
    if rerank_max_candidates is not None and not (
        limit <= rerank_max_candidates <= MAX_RERANK_CANDIDATES
    ):
        raise ValueError(
            "find: rerank_max_candidates must be an integer "
            f"from {limit} to {MAX_RERANK_CANDIDATES}, got "
            f"{rerank_max_candidates!r}"
        )
    effective_rerank_candidate_limit = min(
        3 * limit,
        rerank_max_candidates or MAX_RERANK_CANDIDATES,
    )
    _set_rerank_timing_profile(
        timings,
        requested=rerank_max_candidates,
        effective=effective_rerank_candidate_limit,
        scorer_input_count=0,
        unscored_tail_count=0,
        decision="skipped",
        reason="not_reached",
    )
    if retrieval_trace is not None:
        retrieval_trace.record_rerank_bound(
            requested=rerank_max_candidates,
            effective=effective_rerank_candidate_limit,
            hard_max=MAX_RERANK_CANDIDATES,
        )
    query_norm = (query or "").lower().strip()

    from . import lexstore, readiness

    managed_runtime = readiness.runtime_managed()
    # Exact pending coverage is proven before anything reads a persistent
    # catalogue. Outstanding durable custody that cannot be hydrated within its
    # bound means MANAGED recall cannot answer exactly, and the last published
    # catalogue must not be served as though no committed mutation were
    # outstanding. An offline/CLI caller is a different contract: it keeps its
    # existing exact source-walk fallback, which reads canonical Markdown
    # directly and never consults this custody, so refusing it would deny an
    # answer that is already exact.
    # The durable pending-custody rows ARE an index; reading them is the
    # alternative to proving custody by reading the corpus.
    with _span(timings, "pending_visibility", source=find_types.SOURCE_INDEX):
        pending = freshness.recall_pending_coverage(vault_root)
        if not pending.ready:
            if managed_runtime:
                _set_recall_projection_timing_outcome(timings, "pending_unavailable")
                raise RetrievalIndexWarming(
                    site="pending_visibility_incomplete",
                    status="temporarily_unavailable",
                )
            pending = _EMPTY_PENDING_COVERAGE
    # Admission is part of resolving the projection, and used to be the one
    # material region between two spans: measured at request level it looked
    # like unattributed time. Folding it into `recall_projection` reports it
    # where a reader would look for it.
    with _span(timings, "recall_projection", source=find_types.SOURCE_INDEX):
        admission = readiness.retrieval_admission()
        if managed_runtime and admission["state"] == "unavailable":
            # A background repair may have published the exact catalog after its
            # one promotion callback lost a race.  Re-prove once before scheduling
            # another whole-corpus rebuild; normal ready requests keep one proof.
            admission = readiness.retrieval_admission(vault_root)
    state = str(admission["state"]) if managed_runtime else "unverified"
    require_live_recall = managed_runtime and freshness.event_indexes_enabled()
    catalog_proof: dict[str, freshness.RecallFreshnessCheckpoint] | None = None
    stale_recall_scopes: frozenset[str] = frozenset()
    # The catalogue admission proof is an index read; the outcome below
    # narrows it (an offline caller proved nothing from an index, and a
    # warming or unavailable catalogue declined).
    with _span(timings, "recall_projection", source=find_types.SOURCE_INDEX):
        if state == "ready" and require_live_recall:
            # Bounded projection lag is served, not refused. `admission` takes
            # the strict proof when it binds and otherwise falls back to the
            # catalog's own published projection under an unchanged identity —
            # so a cold or reprojection-evicted registry answers from the last
            # published projection instead of blanking semantic recall.
            admitted = lexstore.runtime_retrieval_catalog_admission(vault_root)
            raw_proof = admitted.checkpoints if admitted is not None else None
            if raw_proof is None:
                readiness.mark_unready("retrieval_catalog")
                state = "unavailable"
            else:
                catalog_proof = {
                    scope: checkpoint
                    for scope, checkpoint in raw_proof.items()
                    if isinstance(checkpoint, freshness.RecallFreshnessCheckpoint)
                }
                if set(catalog_proof) != set(freshness.SCOPES):
                    readiness.mark_unready("retrieval_catalog")
                    catalog_proof = None
                    state = "unavailable"
                else:
                    stale_recall_scopes = frozenset(admitted.lagging_scopes)
                    if stale_recall_scopes and degraded_out is not None:
                        # Rides the existing warming disclosure: the envelope
                        # already projects `warming.components`, so a stale
                        # answer is disclosed without a new envelope field.
                        degraded_out.append(_RECALL_PROJECTION_STALE_COMPONENT)
                    if catalog_proof_out is not None:
                        catalog_proof_out.update(catalog_proof)
        if state in {"warming", "unavailable"}:
            if state == "unavailable":
                lexstore.request_repair(vault_root)
            _set_recall_projection_timing_outcome(timings, state)
            raise RetrievalIndexWarming(
                site="catalog_proof_incomplete",
                status=(
                    "temporarily_unavailable"
                    if state == "unavailable"
                    else "warming"
                ),
            )
        _set_recall_projection_timing_outcome(
            timings,
            "admitted" if require_live_recall else "offline",
        )

    language_registry = None

    def _resolve_language_value(value: str, *, namespace: str) -> str:
        nonlocal language_registry
        if language_registry is None:
            from . import semantic_language_registry

            language_registry = semantic_language_registry.load_registry(vault_root)
        resolution = (
            language_registry.resolve_category(value)
            if namespace == "category"
            else language_registry.resolve_kind(value)
        )
        if resolution.status == "registry_invalid":
            raise structured_filters.FilterError(
                "INVALID_FILTER_VALUE",
                f"$.unit.{namespace}",
                "semantic-language registry is invalid",
                expected=f"unambiguous governed {namespace}",
                remediation="Repair the semantic-language registry before filtering by aliases.",
            )
        return resolution.resolved or resolution.key

    filter_plan = structured_filters.compile_filter(
        filters,
        shortcuts=structured_filters.FilterShortcuts(
            types=tuple(types or ()),
            projects=tuple(projects or ()),
            tags=tuple(tags or ()),
            speakers=tuple(speakers or ()),
            file_types=tuple(file_types or ()),
            exclude_file_types=tuple(exclude_file_types or ()),
            categories=tuple(categories or ()),
            source_kinds=tuple(source_kinds or ()),
            domains=tuple(domains or ()),
            kinds=tuple(kinds or ()),
            updated_after=updated_after,
            updated_before=updated_before,
            recency_days=recency_days,
        ),
        resolve_category=lambda value: _resolve_language_value(value, namespace="category"),
        resolve_kind=lambda value: _resolve_language_value(value, namespace="kind"),
    )
    if filter_plan.root is not None and managed_runtime:
        # A managed reader refuses a field no index can evaluate, and refuses it
        # HERE — before the freshness key, the hot cache, any catalogue query or
        # any lane. The alternative is to discover it at the eligibility seam
        # and answer it with the scan, which is how the whole corpus gets read
        # on the request thread without anything in the response saying so.
        unsupported_fields = _unsupported_filter_fields(filter_plan)
        if unsupported_fields:
            raise _unsupported_filter_field_error(unsupported_fields)
        refused_shapes = _refused_filter_shapes(filter_plan)
        if refused_shapes:
            raise _unsupported_filter_shape_error(refused_shapes)
    filter_key = json.dumps(
        filter_plan.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    effective_result_level = structured_filters.resolve_result_level(
        result_level,
        filter_plan,
    )
    if retrieval_trace is not None:
        retrieval_trace.record_plan(
            query=query,
            intent=intent or find_policy.classify_intent(query),
            effective_result_level=effective_result_level,
            normalized_filters=filter_plan.to_dict(),
        )
    # Keep the date bounds after the legacy arguments are cleared below: the
    # typed plan does the filtering, but reporting *which* bound could not be
    # decided still needs the original values.
    bound_after, bound_before = updated_after, updated_before
    if filter_plan.root is not None:
        # Every shortcut is now represented in the shared typed plan.  Clear
        # the legacy arguments so no lane applies a second, divergent filter.
        types = projects = tags = speakers = file_types = exclude_file_types = None
        updated_after = updated_before = None
        recency_days = None

    # Resolve the relation filter before any caching so a warming/disabled sidecar
    # raises the typed outcome instead of being masked by a stale cache hit, and an
    # unknown relation key is rejected up front. `relation_paths` is None when no
    # filter is active, a (possibly empty, authoritative) participant set otherwise.
    relation_active = bool(relations or relation_of)
    # `_relation_findings` (deprecated-key advisories) is computed but not yet
    # surfaced to the response envelope — a documented follow-up; the deprecated
    # key still resolves and matches correctly.
    relation_paths, relation_provenance, _relation_findings = _resolve_relation_filter(
        vault_root,
        relations=relations,
        relation_of=relation_of,
        relation_direction=relation_direction,
    )

    # One freshness snapshot + one parsed-page memo per request: every
    # consumer below (hot cache, BM25, resolver, auto-widen, boost passes)
    # shares them instead of re-walking / re-stat'ing.
    snapshot = FreshnessSnapshot(
        vault_root,
        require_live_recall=require_live_recall,
        timings=timings,
        expected_recall_checkpoints=catalog_proof,
        stale_recall_scopes=stale_recall_scopes,
        pending=pending,
    )
    page_memo: dict[str, ParsedPage | None] = {}

    def _page_of(rel: str) -> ParsedPage | None:
        if rel not in page_memo:
            page_memo[rel] = _resolve_page(vault_root, rel, pending)
        return page_memo[rel]

    walk_scope = "vault" if scope == "vault" else "kb"
    resolved_config = config if config is not None else _active_ranking()
    degraded = degraded_out if degraded_out is not None else []
    failed = failed_out if failed_out is not None else []
    pending_active = not pending.empty
    if pending_active:
        # Disclosed, never silent: the answer is exact for direct, stable-ref,
        # keyword and hybrid recall, while the vector and graph lanes contribute
        # only proven-current generations. Appending to `degraded` also keeps
        # the result out of the hot cache, which must not outlive retirement.
        degraded.append(_PENDING_VISIBILITY_COMPONENT)
    if effective_result_level == "unit":
        unit_algebra = structured_filters.plan_index_candidates(filter_plan)
        cache_size = (
            0
            if retrieval_trace is not None or pending_active
            else _find_cache_size()
        )
        if timings is not None:
            timings.cache["enabled"] = cache_size > 0
        _set_rerank_timing_profile(
            timings,
            requested=rerank_max_candidates,
            effective=effective_rerank_candidate_limit,
            scorer_input_count=0,
            unscored_tail_count=0,
            decision="skipped",
            reason="result_level_unit",
        )
        unit_cache_key: tuple | None = None
        if cache_size > 0:
            with _span(timings, "freshness"):
                unit_fresh = _freshness_key(
                    vault_root,
                    scope=scope,
                    query_norm=query_norm,
                    mode=mode,
                    graph=False,
                    snapshot=snapshot,
                    unit_filters=True,
                    metadata_only_catalog=unit_algebra.status == "complete",
                    relation_filter=relation_active,
                )
            unit_request_key = (
                "unit",
                str(vault_root.resolve()),
                query,
                limit,
                scope,
                mode,
                filter_key,
                _relation_key(relations, relation_of, relation_direction),
                prefer_active,
                resolved_config,
            )
            unit_cache_key = (unit_request_key, unit_fresh)
            with _span(timings, "cache_lookup", source=find_types.SOURCE_CACHE):
                with _FIND_CACHE_LOCK:
                    cached_units = _FIND_CACHE.get(unit_cache_key)
                    if cached_units is not None:
                        _FIND_CACHE.move_to_end(unit_cache_key)
            if cached_units is not None:
                if timings is not None:
                    timings.cache["hit"] = True
                if unit_algebra.status == "complete":
                    _record_filter_eligibility_cache_hit(timings)
                    _set_catalog_timing_profile(timings, cache_hit=True)
                # The copy is what a cache hit actually costs — the hit list is
                # returned by value so a caller cannot mutate the cached one —
                # and it is proportional to `limit`. Unspanned it was reported
                # as unattributed remainder on every hot request.
                with _span(timings, "cache_copy", source=find_types.SOURCE_CACHE):
                    return copy.deepcopy(cached_units)
        unit_hits = _find_semantic_units(
            vault_root,
            query=query,
            limit=limit,
            scope=walk_scope,
            plan=filter_plan,
            snapshot=snapshot,
            prefer_active=prefer_active,
            config=resolved_config,
            mode=mode,
            degraded_out=degraded,
            failed_out=failed,
            retrieval_trace=retrieval_trace,
            timings=timings,
        )
        if relation_paths is not None:
            # A unit qualifies through its parent page (per-unit edge anchoring is
            # deferred); annotate the survivors with why the page participated.
            parents = set(relation_paths)
            unit_hits = [u for u in unit_hits if u.parent_path in parents]
            for unit in unit_hits:
                match = relation_provenance.get(unit.parent_path)
                if match is not None:
                    unit.relation_match = _relation_match_dict(match, matched="parent")
        if unit_cache_key is not None and not degraded and not failed:
            with _FIND_CACHE_LOCK:
                _FIND_CACHE[unit_cache_key] = copy.deepcopy(unit_hits)
                _FIND_CACHE_CHECKPOINTS.pop(unit_cache_key, None)
                _FIND_CACHE.move_to_end(unit_cache_key)
                _trim_find_cache(cache_size)
        return unit_hits
    mixed = effective_result_level == "mixed"
    query_vector: Any = None
    query_vector_ready = False

    def _query_vector() -> Any:
        nonlocal query_vector, query_vector_ready
        if not query_vector_ready:
            from . import embeddings

            query_vector = embeddings.embed_texts([query], is_query=True)[0]
            query_vector_ready = True
        return query_vector

    # ---- Hot cache lookup (freshness-keyed; see _freshness_key above) ----
    # prefer_used bypasses the cache entirely — simplest correct interaction;
    # log freshness never has to enter the cache key.
    # A pending overlay is deliberately uncacheable: its rows retire as soon
    # as the persistent lanes publish, and an entry keyed only by the
    # persistent projections would outlive that retirement.
    cache_size = (
        0
        if prefer_used or mixed or retrieval_trace is not None or pending_active
        else _find_cache_size()
    )
    cache_key: tuple | None = None
    cache_checkpoints: tuple[
        tuple[str, freshness.RecallFreshnessCheckpoint], ...
    ] | None = None
    if timings is not None:
        timings.cache["enabled"] = cache_size > 0
    if cache_size > 0:

        def _t(v: list | None) -> tuple | None:
            return tuple(v) if v is not None else None

        request_key = (
            str(vault_root.resolve()),
            query,
            _t(types),
            _t(projects),
            _t(tags),
            _t(speakers),
            _t(file_types),
            _t(exclude_file_types),
            limit,
            scope,
            mode,
            graph,
            rerank,
            rerank_max_candidates,
            auto_rerank,
            temporal,
            intent,
            updated_after,
            updated_before,
            recency_days,
            filter_key,
            _relation_key(relations, relation_of, relation_direction),
            effective_result_level,
            prefer_compiled,
            prefer_active,
            widen_outside_kb,
            resolved_config,
        )
        with _span(timings, "freshness"):
            fresh = _freshness_key(
                vault_root,
                scope=scope,
                query_norm=query_norm,
                mode=mode,
                graph=graph,
                snapshot=snapshot,
                unit_filters=filter_plan.has_unit_predicate,
                metadata_only_catalog=(
                    not query_norm
                    and structured_filters.plan_index_candidates(filter_plan).status == "complete"
                ),
                relation_filter=relation_active,
            )
        delta_cache_eligible = bool(
            mode == "keyword"
            and query_norm
            and filter_plan.root is None
            and not relation_active
            and types is None
            and projects is None
            and tags is None
            and speakers is None
            and file_types is None
            and exclude_file_types is None
            and updated_after is None
            and updated_before is None
            and recency_days is None
        )
        if delta_cache_eligible:
            cache_checkpoints = _recall_checkpoints_for_cache(
                snapshot,
                scope=scope,
                query_norm=query_norm,
            )
        cache_key = (request_key, fresh)
        prior_key: tuple | None = None
        prior_hits: list[Hit] | None = None
        prior_checkpoints: tuple[
            tuple[str, freshness.RecallFreshnessCheckpoint], ...
        ] | None = None
        with _span(timings, "cache_lookup", source=find_types.SOURCE_CACHE):
            with _FIND_CACHE_LOCK:
                cached = _FIND_CACHE.get(cache_key)
                if cached is not None:
                    _FIND_CACHE.move_to_end(cache_key)
                elif delta_cache_eligible:
                    for candidate_key in reversed(_FIND_CACHE):
                        if candidate_key[0] != request_key:
                            continue
                        candidate_checkpoints = _FIND_CACHE_CHECKPOINTS.get(candidate_key)
                        if candidate_checkpoints is None:
                            continue
                        prior_key = candidate_key
                        prior_hits = _FIND_CACHE[candidate_key]
                        prior_checkpoints = candidate_checkpoints
                        break
        if (
            cached is None
            and prior_key is not None
            and prior_hits is not None
            and prior_checkpoints is not None
        ):
            safe, advanced = _keyword_cache_delta_is_safe(
                vault_root,
                query_norm=query_norm,
                cached_hits=prior_hits,
                cached_freshness=prior_key[1],
                current_freshness=fresh,
                checkpoints=prior_checkpoints,
                snapshot=snapshot,
            )
            if safe:
                with _FIND_CACHE_LOCK:
                    current = _FIND_CACHE.get(cache_key)
                    if current is not None:
                        cached = current
                        _FIND_CACHE.move_to_end(cache_key)
                    elif _FIND_CACHE.get(prior_key) is prior_hits:
                        cached = _FIND_CACHE.pop(prior_key)
                        _FIND_CACHE_CHECKPOINTS.pop(prior_key, None)
                        _FIND_CACHE[cache_key] = cached
                        _FIND_CACHE_CHECKPOINTS[cache_key] = advanced
                        _FIND_CACHE.move_to_end(cache_key)
        if cached is not None:
            if timings is not None:
                timings.cache["hit"] = True
            _set_rerank_timing_profile(
                timings,
                requested=rerank_max_candidates,
                effective=effective_rerank_candidate_limit,
                scorer_input_count=0,
                unscored_tail_count=0,
                decision="skipped",
                reason="cache_hit",
            )
            if (
                filter_plan.root is not None
                and structured_filters.plan_index_candidates(filter_plan).status == "complete"
            ):
                _record_filter_eligibility_cache_hit(timings)
                _set_catalog_timing_profile(timings, cache_hit=True)
            with _span(timings, "cache_copy", source=find_types.SOURCE_CACHE):
                return copy.deepcopy(cached)

    # Track warm-window degradation even when the caller passed no list —
    # internal callers (suggest_links, evolution, note/add sweeps) must never
    # cache a lexical-only ranking that would outlive the warm.
    # Same rationale for POST-WARM lane failures: a BM25-only result produced
    # because the vector lane threw must not be cached and served after the
    # sidecar/model recovers. Tracked internally even when the caller passes None.
    mixed_unit_hits: list[SemanticUnitHit] = []
    if mixed:
        with _span(timings, "semantic_units"):
            mixed_unit_hits = _find_semantic_units(
                vault_root,
                query=query,
                limit=None,
                scope=walk_scope,
                plan=filter_plan,
                snapshot=snapshot,
                prefer_active=prefer_active,
                config=resolved_config,
                mode=mode,
                degraded_out=degraded,
                failed_out=failed,
                retrieval_trace=retrieval_trace,
                query_vector_provider=_query_vector,
            )
        if retrieval_trace is not None:
            retrieval_trace.snapshot_result_plan("unit")
        if relation_paths is not None:
            # The mixed unit half is produced before the eligibility seam, so gate
            # it here through the parent page exactly as the unit-only branch does.
            parents = set(relation_paths)
            mixed_unit_hits = [u for u in mixed_unit_hits if u.parent_path in parents]
            for unit in mixed_unit_hits:
                match = relation_provenance.get(unit.parent_path)
                if match is not None:
                    unit.relation_match = _relation_match_dict(match, matched="parent")

    # "kb-only" is the strict opt-out (legacy KB-only behavior); "kb" walks the
    # same KB tree but auto-widens to the vault below when it underfills. Both
    # map to a KB-only walk in the underlying rankers.
    eligible_paths: set[str] | None = None
    if filter_plan.root is not None:
        with _span(timings, "filter_eligibility"):
            eligible_paths = _resolve_eligible_filter_paths(
                vault_root,
                scope=walk_scope,
                plan=filter_plan,
                snapshot=snapshot,
                timings=timings,
                pending=pending,
            )
    # Intersect the relation participants into the eligibility seam so the filter
    # composes AND with every structured filter and gates every ranking lane and
    # empty-query recall. An authoritative empty participant set yields no hits.
    if relation_paths is not None:
        relation_set = set(relation_paths)
        eligible_paths = relation_set if eligible_paths is None else (eligible_paths & relation_set)

    # Empty queries always degrade to keyword behavior — there's no signal
    # to embed or score with, just "give me recent stuff that matches the
    # structured filters."
    if mode == "keyword" or not query_norm:
        with _span(timings, "keyword"):
            hits = _find_keyword(
                vault_root,
                query_norm=query_norm,
                types=types,
                projects=projects,
                tags=tags,
                speakers=speakers,
                file_types=file_types,
                exclude_file_types=exclude_file_types,
                limit=limit,
                scope=walk_scope,
                eligible_paths=eligible_paths,
                freshness_key=snapshot.for_scope(walk_scope),
                failed_out=failed,
                pending=pending,
                timings=timings,
            )
    else:
        with _span(timings, "semantic.search"):
            hits = _find_semantic(
                vault_root,
                query=query,
                query_norm=query_norm,
                types=types,
                projects=projects,
                tags=tags,
                speakers=speakers,
                file_types=file_types,
                exclude_file_types=exclude_file_types,
                limit=limit,
                scope=walk_scope,
                mode=mode,
                graph=graph,
                rerank=rerank,
                rerank_max_candidates=rerank_max_candidates,
                auto_rerank=auto_rerank,
                temporal=temporal,
                intent=intent,
                prefer_compiled=prefer_compiled,
                prefer_active=prefer_active,
                prefer_used=prefer_used,
                config=resolved_config,
                timings=timings,
                snapshot=snapshot,
                page_memo=page_memo,
                degraded_out=degraded,
                failed_out=failed,
                eligible_paths=eligible_paths,
                recall_scope="kb" if scope == "kb-only" else "vault",
                retrieval_trace=retrieval_trace,
                query_vector_provider=_query_vector if mixed else None,
                pending=pending,
            )

    if retrieval_trace is not None and (mode == "keyword" or not query_norm):
        retrieval_trace.record_keyword_hits(hits, filter_only=not query_norm)
    if mode == "keyword" or not query_norm:
        _set_rerank_timing_profile(
            timings,
            requested=rerank_max_candidates,
            effective=effective_rerank_candidate_limit,
            scorer_input_count=0,
            unscored_tail_count=0,
            decision="skipped",
            reason="empty_query" if not query_norm else "requested_mode_keyword",
        )

    # Requested widening: reach into the wider vault (sibling folders like
    # Tracking/, Reference/, plus curated trees) so content outside
    # Knowledge Base/ isn't silently invisible. Only for scope="kb" (not
    # "kb-only"/"vault"), non-empty queries (an empty query has no signal to
    # widen on), and only when the caller asked for it.
    #
    # OPT-IN since `accelerate-governed-recall`. It used to run on every
    # default recall and cost 7.6 s on the live cell, because it resolved
    # eligibility a second time at vault scope and then ranked the whole
    # non-KB corpus — building a Python BM25 corpus whenever the maintained
    # catalogue was not fresh enough to serve it. The behaviour is reachable,
    # not free: a caller that relies on the reserve asks for it, and the
    # default stops paying for a lane most callers never read.
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
    if scope == "kb" and query_norm and widen_outside_kb:
        with _span(timings, "outside_kb"):
            seen = {h.path for h in hits}
            outside = [
                h
                for h in _find_outside_kb(
                    vault_root,
                    query=query,
                    query_norm=query_norm,
                    types=types,
                    projects=projects,
                    tags=tags,
                    speakers=speakers,
                    file_types=file_types,
                    exclude_file_types=exclude_file_types,
                    limit=limit,
                    snapshot=snapshot,
                    filter_plan=filter_plan if filter_plan.root is not None else None,
                    exclude_paths=seen,
                    failed_out=failed,
                    retrieval_trace=retrieval_trace,
                    timings=timings,
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
                if retrieval_trace is not None:
                    retrieval_trace.record_auto_widen(
                        hits,
                        strong_paths=[hit.path for hit in strong[:kb_keep]],
                        weak_paths=[hit.path for hit in weak[: max(0, kb_keep - len(strong))]],
                        outside_paths=[hit.path for hit in outside],
                        reserve=reserve,
                        kb_keep=kb_keep,
                    )
    elif timings is not None:
        # Silence and "ran, found nothing" are the same shape in a stage table,
        # and the spec asks for the difference: a default recall must SAY the
        # widening was skipped. `skipped` carries no `ms`, so the attribution
        # partition is untouched.
        timings.skipped("outside_kb")

    # Explicit recency window (off by default) — drop out-of-window hits last,
    # after the requested widening, so it governs every mode uniformly.
    with _span(timings, "date_filter"):
        hits = _filter_by_date(
            hits,
            updated_after=updated_after,
            updated_before=updated_before,
            recency_days=recency_days,
        )

    # A hit kept on a bound that could not actually be ordered is reported as
    # such rather than presented as a clean match. Runs after every filtering
    # lane so it sees exactly the hits the caller will receive.
    if bound_after is not None or bound_before is not None:
        bound_shortcuts = structured_filters.FilterShortcuts(
            updated_after=bound_after, updated_before=bound_before
        )
        for hit in hits:
            vague = structured_filters.indeterminate_bounds(
                {"updated": hit.updated}, shortcuts=bound_shortcuts
            )
            if vague:
                hit.order_indeterminate = list(vague)

    if retrieval_trace is not None:
        retrieval_trace.finalize_page_results(
            hits,
            updated_after=updated_after,
            updated_before=updated_before,
            recency_days=recency_days,
        )
        if mixed:
            retrieval_trace.snapshot_result_plan("page")

    if filter_plan.has_unit_predicate:
        with _span(timings, "matched_units"):
            _annotate_matched_units(vault_root, hits, filter_plan)

    if relation_paths is not None:
        for hit in hits:
            match = relation_provenance.get(hit.path)
            if match is not None:
                hit.relation_match = _relation_match_dict(match, matched="self")

    if mixed:
        return _merge_mixed_hits(
            hits,
            mixed_unit_hits,
            limit=limit,
            config=resolved_config,
            retrieval_trace=retrieval_trace,
        )

    # ---- Hot cache store (deep copies both ways; bounded LRU eviction) ----
    # A result produced with warm-deferred lanes is lexical-only — caching it
    # would keep serving the degraded ranking after the warm completes. A
    # post-warm lane FAILURE (`failed`) is skipped for the same reason: the
    # failure may be transient, so don't pin a BM25-only result in the cache.
    if cache_key is not None and not degraded and not failed:
        with _FIND_CACHE_LOCK:
            _FIND_CACHE[cache_key] = copy.deepcopy(hits)
            if cache_checkpoints is not None:
                _FIND_CACHE_CHECKPOINTS[cache_key] = cache_checkpoints
            else:
                _FIND_CACHE_CHECKPOINTS.pop(cache_key, None)
            _FIND_CACHE.move_to_end(cache_key)
            _trim_find_cache(cache_size)
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


_MATCHED_UNITS_CAP = 5
_MIXED_UNITS_PER_PARENT_CAP = 3
_MIXED_PAGE_WEIGHT = 1.0
_MIXED_UNIT_WEIGHT = 1.0
_UNIT_EXCERPT_MAX = 320


def _unit_excerpt(content: str) -> str:
    compact = " ".join(content.split())
    if len(compact) <= _UNIT_EXCERPT_MAX:
        return compact
    return compact[: _UNIT_EXCERPT_MAX - 1].rstrip() + "…"


def _unit_span(unit: Any) -> dict[str, int]:
    """Coordinates only; authored text is carried once through content/excerpt."""
    return {
        "start_line": unit.span.start_line,
        "start_column": unit.span.start_column,
        "end_line": unit.span.end_line,
        "end_column": unit.span.end_column,
        "start_offset": unit.span.start_offset,
        "end_offset": unit.span.end_offset,
    }


def _eligible_unit_records(
    vault_root: Path,
    *,
    scope: str,
    plan: structured_filters.FilterPlan,
) -> dict[str, tuple[ParsedPage, Any, int]]:
    """Return current unit identities satisfying one exact `(page, unit)` plan."""
    if scope == "kb":
        root = vault_root / kb_dirname()
        walk = _walk_md(root) if root.is_dir() else ()
    else:
        from .vault import walk_vault_md

        walk = walk_vault_md(vault_root)

    from . import semantic_index

    eligible: dict[str, tuple[ParsedPage, Any, int]] = {}
    for path in walk:
        if path.name.lower() in _NAVIGATION_BASENAMES:
            continue
        if not recall_policy.is_recall_candidate(vault_root, path):
            continue
        page = _CACHE.get(path, vault_root)
        if page is None or not _passes_filters(
            page,
            vault_root=vault_root,
            types=None,
            projects=None,
            tags=None,
            speakers=None,
            file_types=None,
            exclude_file_types=None,
        ):
            continue
        try:
            state = semantic_index.current_parent_index_state(vault_root, page.path)
        except (OSError, UnicodeError, ValueError) as error:
            log.warning(
                "semantic-unit retrieval parse failed for %s: %s",
                page.rel_path,
                error,
            )
            continue
        page_value = structured_filters.page_view(page)
        for source_order, unit in enumerate(state.document.units):
            if unit.unit_ref is None:
                continue
            if structured_filters.evaluate_filter(
                plan,
                page=page_value,
                unit=structured_filters.unit_view(unit),
            ):
                eligible[unit.unit_ref] = (page, unit, source_order)
    return eligible


def _indexed_unit_constraints(
    plan: structured_filters.FilterPlan,
) -> tuple[list[str] | None, list[str] | None]:
    """Extract safe conjunctive category/kind constraints for SQL pushdown."""
    values: dict[str, set[str] | None] = {"unit.category": None, "unit.kind": None}

    def visit(node: structured_filters.FilterNode | None) -> None:
        if node is None:
            return
        if isinstance(node, structured_filters.AllOf):
            for child in node.children:
                visit(child)
            return
        if not isinstance(node, structured_filters.Predicate):
            return
        field = node.field.name
        if field not in values:
            return
        for operator, operand in node.operators:
            if operator == "$eq":
                candidates = {str(operand.value)}
            elif operator == "$in":
                candidates = {str(item.value) for item in operand}
            else:
                continue
            current = values[field]
            values[field] = candidates if current is None else current & candidates

    visit(plan.root)
    categories = values["unit.category"]
    kinds = values["unit.kind"]
    return (
        sorted(categories) if categories else None,
        sorted(kinds) if kinds else None,
    )


def _hydrate_indexed_unit_records(
    vault_root: Path,
    indexed: list[Any],
    *,
    plan: structured_filters.FilterPlan,
    stale_out: list[str] | None = None,
) -> dict[str, tuple[ParsedPage, Any, int]]:
    """Hydrate only sidecar-selected parents, rejecting any generation race."""
    from . import semantic_index

    parents: dict[str, tuple[ParsedPage, Any] | None] = {}
    records: dict[str, tuple[ParsedPage, Any, int]] = {}
    for hit in indexed:
        parent = parents.get(hit.parent_path)
        if hit.parent_path not in parents:
            if not recall_policy.is_recall_candidate(vault_root, vault_root / hit.parent_path):
                parents[hit.parent_path] = None
                continue
            page = _CACHE.get(vault_root / hit.parent_path, vault_root)
            if page is None or not _passes_filters(
                page,
                vault_root=vault_root,
                types=None,
                projects=None,
                tags=None,
                speakers=None,
                file_types=None,
                exclude_file_types=None,
            ):
                if page is None and stale_out is not None:
                    stale_out.append(hit.unit_ref)
                parents[hit.parent_path] = None
                continue
            try:
                state = semantic_index.current_parent_index_state(vault_root, hit.parent_path)
            except (OSError, UnicodeError, ValueError) as error:
                log.warning(
                    "semantic-unit candidate hydration failed for %s: %s",
                    hit.parent_path,
                    error,
                )
                if stale_out is not None:
                    stale_out.append(hit.unit_ref)
                parents[hit.parent_path] = None
                continue
            if state.parent_generation != hit.parent_generation:
                if stale_out is not None:
                    stale_out.append(hit.unit_ref)
                parents[hit.parent_path] = None
                continue
            parent = (page, state)
            parents[hit.parent_path] = parent
        if parent is None:
            continue
        page, state = parent
        located = next(
            (
                (source_order, candidate)
                for source_order, candidate in enumerate(state.document.units)
                if candidate.unit_ref == hit.unit_ref
            ),
            None,
        )
        if located is None:
            if stale_out is not None:
                stale_out.append(hit.unit_ref)
            continue
        source_order, unit = located
        if not structured_filters.evaluate_filter(
            plan,
            page=structured_filters.page_view(page),
            unit=structured_filters.unit_view(unit),
        ):
            continue
        records[hit.unit_ref] = (page, unit, getattr(hit, "source_order", source_order))
    return records


def _python_unit_scores(
    records: dict[str, tuple[ParsedPage, Any, int]],
    query: str,
) -> dict[str, float]:
    """Deterministic in-process lexical rung when the FTS sidecar is absent."""
    from . import bm25

    query_tokens = bm25.tokenize(query)
    if not query_tokens:
        return {}
    refs = list(records)
    corpus = [bm25.tokenize(records[unit_ref][1].content) for unit_ref in refs]
    if not any(corpus):
        return {}
    from rank_bm25 import BM25Okapi

    scores = BM25Okapi(corpus).get_scores(query_tokens)
    wanted = set(query_tokens)
    return {
        unit_ref: float(score)
        for unit_ref, tokens, score in zip(refs, corpus, scores, strict=True)
        if wanted.intersection(tokens)
    }


def _unit_text_match_refs(
    records: dict[str, tuple[ParsedPage, Any, int]],
    query: str,
) -> set[str]:
    """Exact OR/stemming membership shared with both lexical rungs."""
    from . import bm25

    wanted = set(bm25.tokenize(query))
    if not wanted:
        return set()
    return {
        unit_ref
        for unit_ref, (_page, unit, _source_order) in records.items()
        if wanted.intersection(bm25.tokenize(unit.content))
    }


def _unit_rank_score(
    raw_score: float,
    *,
    page: ParsedPage,
    prefer_active: bool,
    config: RankingConfig,
) -> float:
    if not prefer_active or page.status != "superseded":
        return raw_score
    penalty = config.superseded_penalty
    return raw_score * penalty if raw_score >= 0 else raw_score / penalty


def _semantic_unit_hit(
    page: ParsedPage,
    unit: Any,
    *,
    bm25_rank: int | None,
    bm25_score: float | None,
    vector_rank: int | None = None,
    vector_score: float | None = None,
) -> SemanticUnitHit:
    return SemanticUnitHit(
        unit_ref=unit.unit_ref,
        form=unit.form,
        category_raw=unit.category_raw,
        category_key=unit.category_key,
        category=unit.category,
        kind=unit.kind,
        content=unit.content,
        excerpt=_unit_excerpt(unit.content),
        tags=list(unit.tags),
        context=unit.context,
        verdict=getattr(unit, "verdict", None),
        check_by=getattr(unit, "check_by", None),
        relations=[relation.to_dict() for relation in unit.relations],
        source_anchor=unit.anchor,
        source_span=_unit_span(unit),
        source_hash=unit.source_hash,
        parent_path=page.rel_path,
        parent_ref=unit.parent_ref,
        parent_title=page.title,
        parent_type=page.page_type,
        parent_status=page.status,
        parent_updated=page.updated,
        parent_superseded_by=page.superseded_by,
        snapshot_hash=page.snapshot_hash,
        bm25_rank=bm25_rank,
        bm25_score=bm25_score,
        vector_rank=vector_rank,
        vector_score=vector_score,
    )


def _vector_unit_candidates(
    vault_root: Path,
    *,
    query: str,
    candidate_limit: int,
    allowed_unit_refs: set[str] | None,
    allowed_parent_paths: set[str],
    degraded_out: list[str] | None,
    failed_out: list[str] | None,
    timings: FindTimings | None,
    query_vector_provider: Callable[[], Any] | None = None,
) -> tuple[list[Any], dict[str, Any], str]:
    """Return bounded vector candidates without opening every Markdown parent."""
    model_name = "BAAI/bge-base-en-v1.5"
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        return (
            [],
            {"status": "disabled", "reason": "embeddings_disabled", "model": model_name},
            "kb",
        )

    from . import readiness

    if readiness.should_defer("embeddings"):
        if degraded_out is not None:
            degraded_out.append("embeddings")
        return [], {"status": "warming", "reason": "model_warming", "model": model_name}, "kb"
    try:
        from . import embeddings

        index = embeddings.get_embedding_index(vault_root)
        with _span(timings, "vector.unit.embed"):
            query_vector = (
                query_vector_provider()
                if query_vector_provider is not None
                else embeddings.embed_texts([query], is_query=True)[0]
            )
        hits = index.search_semantic_units(
            query_vector,
            k=candidate_limit,
            allowed_unit_refs=allowed_unit_refs,
            allowed_parent_paths=allowed_parent_paths,
            validate=False,
        )
        profile = {
            "status": "participated" if hits else "available_nonmatching",
            "backend": type(index).__name__,
            "model": embeddings.MODEL_NAME,
            "metric": {
                "name": "cosine_similarity",
                "direction": "higher",
                "range": [-1.0, 1.0],
                "rounding": 6,
            },
        }
        return hits, profile, embeddings.index_scope()
    except ImportError as error:
        log.info("semantic-unit vector search unavailable (%s); using lexical ranking", error)
        return (
            [],
            {"status": "unavailable", "reason": "dependency_unavailable", "model": model_name},
            "kb",
        )
    except runtime_resources.ModelBusyError:
        raise
    except Exception as error:  # noqa: BLE001 - vector lane soft-falls back
        log.warning("semantic-unit vector search failed: %s; using lexical ranking", error)
        _record_degradation("vector")
        if failed_out is not None:
            failed_out.append("vector")
        return [], {"status": "failed", "reason": "search_failed", "model": model_name}, "kb"


def _find_semantic_units(
    vault_root: Path,
    *,
    query: str,
    limit: int | None,
    scope: str,
    plan: structured_filters.FilterPlan,
    snapshot: FreshnessSnapshot,
    prefer_active: bool,
    config: RankingConfig,
    mode: str,
    degraded_out: list[str] | None,
    failed_out: list[str] | None,
    retrieval_trace: Any | None = None,
    timings: FindTimings | None = None,
    query_vector_provider: Callable[[], Any] | None = None,
) -> list[SemanticUnitHit]:
    """Rank current, exactly eligible units through lexical and vector lanes."""
    from . import lexstore

    # Empty queries carry no vector signal. Match the page lane's contract and
    # treat them as filter-only keyword recall before deciding whether exact
    # catalog rows can stay cold for vector ranking.
    if not query.strip():
        mode = "keyword"

    indexed: list[Any] | None = None
    candidate_window_exhausted = False
    # The unit lane consumes the same branch-preserving DNF the page lane uses:
    # an exact category/kind plan seeds candidates through `algebra.clauses`
    # (same-row `(category... AND kind...) OR ...`), never the flattened
    # category/kind cross-product. An unsupported plan carries no clauses and
    # keeps the existing scan/fallback behavior.
    algebra = structured_filters.plan_index_candidates(plan)
    if algebra.status == "complete" and algebra.definitely_empty:
        # Contradictory positive seeds prove a finitely empty candidate set:
        # return no units WITHOUT any catalog query or corpus walk, matching the
        # page lane's definitely-empty guarantee.
        _record_filter_eligibility_cache_hit(timings)
        _set_catalog_timing_profile(timings, cache_hit=True)
        return []
    dnf_clauses = algebra.clauses if algebra.status == "complete" else None
    requested_limit = 20 if limit is None else max(1, limit)
    candidate_limit = max(20, min(200, requested_limit * 8))
    if mode in {"keyword", "hybrid", "vector"}:
        if dnf_clauses is not None:
            # Exact eligibility is normal-table metadata only. Hybrid/vector
            # content ranking consumes this finite candidate set independently,
            # so FTS absence cannot change the catalog outcome.
            with _span(timings, "filter_eligibility", source=find_types.SOURCE_INDEX):
                exact_freshness = snapshot.for_scope(scope)
                exact_repair = _bounded_lexical_repair_allowed(exact_freshness)
                bounded_filter_only = (
                    not query.strip() and limit is not None and not algebra.post_filter_required
                )
                if bounded_filter_only:
                    # SQL and final filter-only ordering are identical. Read a
                    # small leading prefix, apply access/current-parent checks,
                    # and expand only when canonical rejection underfills it.
                    # Re-reading from zero makes each prefix self-contained if a
                    # concurrent catalog writer inserts, deletes, or reorders a
                    # row between reads; offset pagination could skip across that
                    # moving boundary. The common eligible path still opens only
                    # max(8, requested_limit) rows.
                    prefix_size = max(8, requested_limit)
                    indexed = []
                    records = {}
                    while True:
                        catalog_result = lexstore.search_semantic_units_result(
                            vault_root,
                            query,
                            k=prefix_size,
                            clauses=dnf_clauses,
                            scope=scope,
                            freshness=exact_freshness,
                            _repair_stale=True,
                            repair=exact_repair,
                        )
                        _set_catalog_timing_profile(timings, catalog_result.readiness)
                        if not catalog_result.readiness.complete:
                            _raise_catalog_outcome(catalog_result.readiness)
                        indexed = list(catalog_result.value or [])
                        records = _hydrate_indexed_unit_records(vault_root, indexed, plan=plan)
                        if len(records) >= requested_limit or len(indexed) < prefix_size:
                            break
                        prefix_size *= 2
                else:
                    # Category/kind rows are only the exact seed. Page
                    # predicates and other canonical filters run after
                    # hydration, so limiting this seed before post-evaluation
                    # can false-empty when the first window is ineligible.
                    exact_limit = 2_147_483_647
                    catalog_result = lexstore.search_semantic_units_result(
                        vault_root,
                        query,
                        k=exact_limit,
                        clauses=dnf_clauses,
                        scope=scope,
                        freshness=exact_freshness,
                        literal_all=mode == "keyword" and bool(query.strip()),
                        _repair_stale=True,
                        repair=exact_repair,
                    )
                    _set_catalog_timing_profile(timings, catalog_result.readiness)
                    if not catalog_result.readiness.complete:
                        _raise_catalog_outcome(catalog_result.readiness)
                    indexed = list(catalog_result.value or [])
                    records = (
                        {}
                        if mode == "vector"
                        else _hydrate_indexed_unit_records(vault_root, indexed, plan=plan)
                    )
        else:
            indexed = lexstore.search_semantic_units(
                vault_root,
                query,
                k=candidate_limit + 1,
                scope=scope,
                freshness=snapshot.for_scope(scope),
                literal_all=mode == "keyword" and bool(query.strip()),
                _repair_stale=True,
                repair=_bounded_lexical_repair_allowed(snapshot.for_scope(scope)),
            )
        if indexed is None:
            if lexstore.backend() != "python":
                # A safe exact plan the maintained index cannot yet answer must
                # not degrade to an empty result: the lexical layer already
                # scheduled the single-flight repair, so raise the typed,
                # non-cacheable warming outcome (page-level eligibility parity).
                if algebra.status == "complete":
                    raise RetrievalIndexWarming(site="semantic_unit_index")
                if failed_out is not None:
                    failed_out.append("semantic_units_lexical")
                _record_degradation("semantic_units_lexical")
                # Content-only unit recall may use its established Python rung
                # for a provably small corpus.  A catalog-identity change can
                # invalidate every stored semantic row even though the text
                # query remains answerable from current Markdown; do not turn
                # that bounded fallback into an authoritative empty result.
                records = (
                    _eligible_unit_records(vault_root, scope=scope, plan=plan)
                    if _bounded_lexical_repair_allowed(snapshot.for_scope(scope))
                    else {}
                )
            else:
                records = _eligible_unit_records(vault_root, scope=scope, plan=plan)
        elif dnf_clauses is None:
            if len(indexed) > candidate_limit:
                candidate_window_exhausted = True
                indexed = indexed[:candidate_limit]
            records = (
                {}
                if mode == "vector"
                else _hydrate_indexed_unit_records(vault_root, indexed, plan=plan)
            )
    else:
        records = _eligible_unit_records(vault_root, scope=scope, plan=plan)

    if not query.strip():
        if not records:
            return []
        ordered = sorted(
            records.values(),
            key=lambda record: (
                record[0].updated or "0000-00-00",
                record[0].rel_path,
                -record[2],
            ),
            reverse=True,
        )
        selected = ordered if limit is None else ordered[:limit]
        ordered_hits = [
            _semantic_unit_hit(page, unit, bm25_rank=None, bm25_score=None)
            for page, unit, _source_order in selected
        ]
        if retrieval_trace is not None:
            retrieval_trace.record_unit_filter_only(selected)
        return ordered_hits

    if mode == "keyword":
        if not records:
            return []
        tokens = query.lower().split()
        ordered = sorted(
            (
                record
                for record in records.values()
                if all(token in record[1].content.lower() for token in tokens)
            ),
            key=lambda record: (
                record[0].updated or "0000-00-00",
                record[0].rel_path,
                -record[2],
            ),
            reverse=True,
        )
        if prefer_active:
            ordered.sort(key=lambda record: record[0].status == "superseded")
        selected = ordered if limit is None else ordered[:limit]
        hits = [
            _semantic_unit_hit(page, unit, bm25_rank=None, bm25_score=None)
            for page, unit, _source_order in selected
        ]
        if retrieval_trace is not None:
            retrieval_trace.record_unit_keyword(selected)
        return hits

    vector_hits: list[Any] = []
    vector_profile: dict[str, Any] = {
        "status": "non_applicable",
        "reason": "requested_mode_keyword",
    }
    if mode in {"hybrid", "vector"}:
        vector_allowed_refs: set[str] | None = None
        if dnf_clauses is not None:
            # The allow-set is branch-correlated and bounded: it pushes the same
            # DNF clauses down so only rows satisfying a real `(category AND
            # kind)` branch are admitted — never the giant all-unit query under a
            # flattened cross-product filter.
            vector_allowed_refs = {row.unit_ref for row in indexed or ()}
        vector_candidate_limit = (
            len(vector_allowed_refs) if vector_allowed_refs is not None else candidate_limit + 1
        )
        vector_hits, vector_profile, _indexed_scope = _vector_unit_candidates(
            vault_root,
            query=query,
            candidate_limit=vector_candidate_limit,
            allowed_unit_refs=vector_allowed_refs,
            allowed_parent_paths=snapshot.recall_paths(scope),
            degraded_out=degraded_out,
            failed_out=failed_out,
            timings=timings,
            query_vector_provider=query_vector_provider,
        )
        if (
            vector_allowed_refs is not None
            and {hit.unit_ref for hit in vector_hits} != vector_allowed_refs
        ):
            # A partial embedding window cannot prove an exact filtered miss:
            # an omitted exact ref may be the first one to survive canonical
            # page/unit post-filters. Fall back to the complete catalog seed.
            vector_hits = []
            vector_profile = {
                "status": "failed",
                "reason": "incomplete_exact_candidates",
                "model": vector_profile.get("model", "BAAI/bge-base-en-v1.5"),
            }
            _record_degradation("vector")
            if failed_out is not None and "vector" not in failed_out:
                failed_out.append("vector")
        elif vector_allowed_refs is None and len(vector_hits) > candidate_limit:
            candidate_window_exhausted = True
            vector_hits = vector_hits[:candidate_limit]
    if vector_hits:
        stale_vector_refs: list[str] = []
        records.update(
            _hydrate_indexed_unit_records(
                vault_root,
                vector_hits,
                plan=plan,
                stale_out=stale_vector_refs,
            )
        )
        if stale_vector_refs:
            vector_hits = []
            vector_profile = {
                "status": "failed",
                "reason": "stale_candidates",
                "model": vector_profile.get("model", "BAAI/bge-base-en-v1.5"),
            }
            _record_degradation("vector")
            if failed_out is not None and "vector" not in failed_out:
                failed_out.append("vector")

    # Pure vector mode keeps lexical rows cold unless the vector lane could not
    # produce a trustworthy ranking. This avoids reparsing the same candidate
    # parents twice while preserving the deterministic lexical fallback.
    if mode == "vector" and not vector_hits and indexed:
        records.update(_hydrate_indexed_unit_records(vault_root, indexed, plan=plan))

    if not records:
        if candidate_window_exhausted and failed_out is not None:
            failed_out.append("semantic_units_candidate_window")
            _record_degradation("semantic_units_candidate_window")
        return []

    if indexed is None:
        indexed = lexstore.search_semantic_units(
            vault_root,
            query,
            k=len(records),
            scope=scope,
            freshness=snapshot.for_scope(scope),
            allowed_unit_refs=set(records),
            _repair_stale=True,
            repair=_bounded_lexical_repair_allowed(snapshot.for_scope(scope)),
        )
    if indexed is None:
        scores = _python_unit_scores(records, query)
    else:
        indexed_scores = {
            hit.unit_ref: hit.lexical_score
            for hit in indexed
            if hit.unit_ref in records and hit.lexical_score is not None
        }
        # Registry edits can change the current parent generation without
        # moving the Markdown corpus freshness key. The sidecar correctly
        # rejects those stale rows; if that makes its match set incomplete,
        # score the already-parsed live eligible records through the Python
        # rung rather than returning a false negative.
        scores = (
            indexed_scores
            if set(indexed_scores) == _unit_text_match_refs(records, query)
            else _python_unit_scores(records, query)
        )

    def _raw_ranking(lane_scores: dict[str, float]) -> list[str]:
        return sorted(
            lane_scores,
            key=lambda unit_ref: (
                -lane_scores[unit_ref],
                records[unit_ref][0].rel_path,
                records[unit_ref][2],
                unit_ref,
            ),
        )

    def _preferred_ranking(lane_scores: dict[str, float]) -> list[str]:
        return sorted(
            lane_scores,
            key=lambda unit_ref: (
                -_unit_rank_score(
                    lane_scores[unit_ref],
                    page=records[unit_ref][0],
                    prefer_active=prefer_active,
                    config=config,
                ),
                bool(prefer_active and records[unit_ref][0].status == "superseded"),
                records[unit_ref][0].rel_path,
                records[unit_ref][2],
                unit_ref,
            ),
        )

    lexical_ranking = _raw_ranking(scores)
    lexical_rank = {unit_ref: rank for rank, unit_ref in enumerate(lexical_ranking, 1)}

    vector_scores = {hit.unit_ref: hit.cosine for hit in vector_hits if hit.unit_ref in records}

    vector_ranking = _raw_ranking(vector_scores)
    vector_rank = {unit_ref: rank for rank, unit_ref in enumerate(vector_ranking, 1)}
    raw_fused_score_by_ref: dict[str, float] = {}

    if not vector_ranking:
        final_ranking = _preferred_ranking(scores)
    elif mode == "vector" or not lexical_ranking:
        final_ranking = _preferred_ranking(vector_scores)
    else:
        from . import fusion

        weights = config.intent_weights(find_policy.classify_intent(query))
        fused = fusion.reciprocal_rank_fusion_weighted(
            [vector_ranking, lexical_ranking],
            [weights[0], weights[1]],
            k=config.rrf_k,
        )
        raw_fused_score_by_ref = dict(fused)
        final_ranking = [
            unit_ref
            for unit_ref, _fused_score in sorted(
                fused,
                key=lambda item: (
                    -_unit_rank_score(
                        item[1],
                        page=records[item[0]][0],
                        prefer_active=prefer_active,
                        config=config,
                    ),
                    bool(prefer_active and records[item[0]][0].status == "superseded"),
                    records[item[0]][0].rel_path,
                    records[item[0]][2],
                    item[0],
                ),
            )
        ]

    if limit is not None:
        final_ranking = final_ranking[:limit]
    if candidate_window_exhausted and (limit is None or len(final_ranking) < limit):
        if failed_out is not None:
            failed_out.append("semantic_units_candidate_window")
        _record_degradation("semantic_units_candidate_window")
    vector_succeeded = bool(vector_ranking)
    if retrieval_trace is not None:
        intent_weights = config.intent_weights(find_policy.classify_intent(query))
        retrieval_trace.record_unit_ranked(
            records=records,
            lexical_ranking=lexical_ranking,
            lexical_scores=scores,
            lexical_backend=lexstore.cache_token(vault_root),
            vector_ranking=vector_ranking,
            vector_scores=vector_scores,
            vector_profile=vector_profile,
            final_ranking=final_ranking,
            raw_fused_score_by_ref=raw_fused_score_by_ref,
            weights=(intent_weights[0], intent_weights[1]),
            rrf_k=config.rrf_k,
            prefer_active=prefer_active,
            superseded_penalty=config.superseded_penalty,
            lexical_used=(mode != "vector" or not vector_succeeded),
            vector_used=vector_succeeded,
        )
    return [
        _semantic_unit_hit(
            records[unit_ref][0],
            records[unit_ref][1],
            bm25_rank=(
                lexical_rank.get(unit_ref) if mode != "vector" or not vector_succeeded else None
            ),
            bm25_score=(scores.get(unit_ref) if mode != "vector" or not vector_succeeded else None),
            vector_rank=vector_rank.get(unit_ref),
            vector_score=vector_scores.get(unit_ref),
        )
        for unit_ref in final_ranking
    ]


def _merge_mixed_hits(
    page_hits: list[Hit],
    unit_hits: list[SemanticUnitHit],
    *,
    limit: int,
    config: RankingConfig,
    retrieval_trace: Any | None = None,
) -> list[Hit | SemanticUnitHit]:
    """Fuse independent page/unit rankings after exact per-parent unit caps."""
    kept_units: list[SemanticUnitHit] = []
    units_by_parent: dict[str, int] = {}
    omitted_by_parent: dict[str, int] = {}
    for hit in unit_hits:
        count = units_by_parent.get(hit.parent_path, 0)
        if count >= _MIXED_UNITS_PER_PARENT_CAP:
            omitted_by_parent[hit.parent_path] = omitted_by_parent.get(hit.parent_path, 0) + 1
            continue
        units_by_parent[hit.parent_path] = count + 1
        kept_units.append(hit)

    ranked: list[tuple[float, int, str, Hit | SemanticUnitHit, int, str]] = []
    ranked.extend(
        (
            _MIXED_PAGE_WEIGHT / (config.rrf_k + rank),
            0,
            hit.path,
            hit,
            rank,
            "page",
        )
        for rank, hit in enumerate(page_hits, start=1)
    )
    ranked.extend(
        (
            _MIXED_UNIT_WEIGHT / (config.rrf_k + rank),
            1,
            hit.unit_ref,
            hit,
            rank,
            "unit",
        )
        for rank, hit in enumerate(kept_units, start=1)
    )
    ranked_items = sorted(ranked, key=lambda item: (-item[0], item[1], item[2]))[:limit]
    merged = [item[3] for item in ranked_items]
    if retrieval_trace is not None:
        retrieval_trace.record_mixed(
            ranked_items,
            rrf_k=config.rrf_k,
            page_weight=_MIXED_PAGE_WEIGHT,
            unit_weight=_MIXED_UNIT_WEIGHT,
            unit_parent_cap=_MIXED_UNITS_PER_PARENT_CAP,
        )

    first_unit_by_parent: dict[str, SemanticUnitHit] = {}
    page_by_path: dict[str, Hit] = {}
    for hit in merged:
        if isinstance(hit, Hit):
            hit.result_type = "page"
            page_by_path[hit.path] = hit
        else:
            first_unit_by_parent.setdefault(hit.parent_path, hit)
    for parent_path, omitted in omitted_by_parent.items():
        target = page_by_path.get(parent_path) or first_unit_by_parent.get(parent_path)
        if target is not None:
            target.mixed_units_truncated = omitted
    return merged


def _annotate_matched_units(
    vault_root: Path,
    hits: list[Hit],
    plan: structured_filters.FilterPlan,
) -> None:
    """Attach bounded same-unit matches without changing default page bytes."""
    from . import semantic_index

    for hit in hits:
        page = _CACHE.get(vault_root / hit.path, vault_root)
        if page is None:
            hit.matched_units = []
            continue
        try:
            state = semantic_index.current_parent_index_state(vault_root, page.rel_path)
        except (OSError, UnicodeError, ValueError) as error:
            log.warning("matched-unit parse failed for %s: %s", hit.path, error)
            hit.matched_units = []
            continue
        page_value = structured_filters.page_view(page)
        matched = [
            unit
            for unit in state.document.units
            if structured_filters.evaluate_filter(
                plan,
                page=page_value,
                unit=structured_filters.unit_view(unit),
            )
        ]
        hit.matched_units = [
            {
                "unit_ref": unit.unit_ref,
                "form": unit.form,
                "category": unit.category,
                "category_key": unit.category_key,
                "kind": unit.kind,
                "anchor": unit.anchor,
                "span": _unit_span(unit),
                "excerpt": _unit_excerpt(unit.content),
            }
            for unit in matched[:_MATCHED_UNITS_CAP]
        ]
        hit.matched_units_truncated = max(0, len(matched) - _MATCHED_UNITS_CAP)


def _eligible_filter_paths(
    vault_root: Path,
    *,
    scope: str,
    plan: structured_filters.FilterPlan,
    pending: Any | None = None,
) -> set[str]:
    """Resolve the one backend-independent eligible parent identity set."""
    if scope == "kb":
        root = vault_root / kb_dirname()
        walk = _walk_md(root) if root.is_dir() else ()
    else:
        from .vault import walk_vault_md

        walk = walk_vault_md(vault_root)
    if pending is not None and not pending.empty:
        walk = _merge_pending_walk(vault_root, walk, pending=pending, scope=scope)

    pages: dict[str, ParsedPage] = {}
    for path in walk:
        if path.name.lower() in _NAVIGATION_BASENAMES:
            continue
        rel_path = _vault_rel(vault_root, path)
        if rel_path is None:
            continue
        page = _resolve_page(vault_root, rel_path, pending)
        if page is not None:
            pages[page.rel_path] = page

    def _indexable(page: ParsedPage) -> bool:
        return _passes_filters(
            page,
            vault_root=vault_root,
            types=None,
            projects=None,
            tags=None,
            speakers=None,
            file_types=None,
            exclude_file_types=None,
        )

    eligibility_by_emitted_path: dict[str, bool] = {}
    eligible: set[str] = set()
    for page in pages.values():
        # Access policy always runs before caller filters, including for a
        # scene-frame child whose match is emitted as its parent video.
        if not _indexable(page):
            continue
        emitted = pages.get(page.parent_media + ".md", page) if page.parent_media else page
        if not _indexable(emitted):
            continue
        matches = eligibility_by_emitted_path.get(emitted.rel_path)
        if matches is None:
            units: tuple[dict[str, Any], ...] = ()
            if plan.has_unit_predicate:
                try:
                    from . import semantic_index

                    state = semantic_index.current_parent_index_state(vault_root, emitted.path)
                    units = tuple(
                        structured_filters.unit_view(unit) for unit in state.document.units
                    )
                except (OSError, UnicodeError, ValueError) as error:
                    log.warning(
                        "semantic-unit filter parse failed for %s: %s",
                        emitted.rel_path,
                        error,
                    )
            matches = structured_filters.page_matches(
                plan,
                page=structured_filters.page_view(emitted),
                units=units,
            )
            eligibility_by_emitted_path[emitted.rel_path] = matches
        if matches:
            # Candidate lanes address the child before frame collapsing, while
            # final hits address the emitted parent. Both identities therefore
            # belong to the exact same eligibility set.
            eligible.add(page.rel_path)
            eligible.add(emitted.rel_path)
    return eligible


_RETRIEVAL_WARMING_RETRY_MS = 250

#: Closed, content-free vocabulary of retrieval-refusal sites. Each names ONE
#: gate that can decline a maintained recall, so a live refusal is attributable
#: from its envelope alone instead of by reading the server source.
RETRIEVAL_WARMING_SITES = (
    "projection_unavailable",
    "catalog_proof_incomplete",
    "pending_visibility_incomplete",
    "semantic_unit_index",
    "semantic_unit_seed",
    "filter_eligibility_unnarrowed",
    "catalog_outcome",
    "relation_graph",
    "relation_graph_rebuilding",
    "resolver_checkpoint_stale",
    "resolver_checkpoint_absent",
    "resolver_entries_unavailable",
    "resolver_build_wait",
    # Lane 3: the reference sidecar is a maintained index like any other, and a
    # managed reader must not build it on the request thread. The lexical
    # corpus already declines under `catalog_outcome`.
    "reference_sidecar",
)


class RetrievalIndexWarming(cli_ops.OpError):
    """Typed, non-cacheable outcome for a safe exact recall plan the maintained
    semantic index cannot yet answer completely.

    Carries the warming fields both as direct attributes (``complete``,
    ``status``, ``retry_after_ms``) and inside ``OpError.details`` so the shared
    envelope projects them unchanged.
    """

    def __init__(
        self,
        *,
        site: str,
        status: str = "warming",
        retry_after_ms: int = _RETRIEVAL_WARMING_RETRY_MS,
        waited_ms: int | None = None,
        message: str = (
            "the maintained semantic recall index is still warming; retry the exact recall shortly"
        ),
    ) -> None:
        # The vocabulary is enforced HERE, not by a test reading source shapes.
        # This envelope reaches REST and MCP verbatim, so a site built from an
        # f-string could publish a vault path to every client; a single-quoted
        # literal or a construct-then-raise is invisible to any source scan.
        # No call shape can evade a constructor.
        if site not in RETRIEVAL_WARMING_SITES:
            raise ValueError(f"unknown retrieval refusal site: {site!r}")
        self.complete = False
        self.status = status
        self.retry_after_ms = retry_after_ms
        self.site = site
        self.waited_ms = waited_ms
        details: dict[str, object] = {
            "complete": False,
            "status": status,
            "retry_after_ms": retry_after_ms,
            # WHICH gate refused. Every site produced a byte-identical envelope
            # before this, so a live refusal could not be attributed without
            # reading the server's own source alongside its logs.
            "site": site,
        }
        if waited_ms is not None:
            details["waited_ms"] = waited_ms
        # Exactly one content-free line per refusal: the decision trail this
        # path never had. Names the gate, never the query or any path.
        log.info(
            "retrieval refusal: site=%s status=%s waited_ms=%s",
            site,
            status,
            "n/a" if waited_ms is None else waited_ms,
        )
        super().__init__("RETRIEVAL_INDEX_WARMING", message, details=details)


def _raise_catalog_outcome(readiness: object) -> None:
    outcome = str(getattr(readiness, "status", "stale"))
    public_status = (
        "temporarily_unavailable" if outcome in {"transient_failure", "unsupported"} else "warming"
    )
    raise RetrievalIndexWarming(site="catalog_outcome", status=public_status)


_RELATION_DIRECTIONS = ("any", "outbound", "inbound")


def _relation_key(
    relations: list[str] | None, relation_of: str | None, relation_direction: str
) -> tuple | None:
    """Stable hot-cache key fragment for the relation filter (None when inactive)."""
    if not relations and not relation_of:
        return None
    return (tuple(relations or ()), relation_of, relation_direction)


def _relation_match_dict(match: Any, *, matched: str = "self") -> dict[str, Any]:
    """Additive `relation_match` hit annotation, distinct from graph-provenance.

    `matched` is "self" when a page-level hit qualified directly, "parent" when a
    unit qualified through its parent page. `matched_via` (from the edge) is
    "relation_type" or "parent_relation" (extension roll-up).
    """
    return {
        "relation_type": match.relation_type,
        "direction": match.direction,
        "counterpart": match.counterpart,
        "matched_via": match.matched_via,
        "matched": matched,
    }


def _nearest_relation_keys(registry: object, raw: str, *, limit: int = 3) -> list[str]:
    import difflib

    from . import relation_registry

    label = relation_registry.normalize_relation(raw)
    keys = sorted(getattr(registry, "keys", frozenset()))
    return difflib.get_close_matches(label, keys, n=limit, cutoff=0.4)


def _resolve_relation_filter(
    vault_root: Path,
    *,
    relations: list[str] | None,
    relation_of: str | None,
    relation_direction: str,
) -> tuple[frozenset[str] | None, dict[str, Any], tuple[dict[str, str], ...]]:
    """Resolve the relation filter to a participant path set (None when inactive),
    plus per-path provenance and advisory findings.

    Each requested relation is canonicalized through the registry; an unknown key
    raises ``INVALID_RELATION_FILTER`` with nearest-canonical suggestions (never a
    silent empty). A missing or stale sidecar raises the typed warming outcome and
    schedules a single-flight rebuild; a disabled index raises
    ``temporarily_unavailable``. An empty participant set from a current sidecar is
    authoritative.
    """
    if not relations and not relation_of:
        return None, {}, ()
    if relation_direction not in _RELATION_DIRECTIONS:
        raise cli_ops.OpError(
            "INVALID_RELATION_FILTER",
            f"relation_direction must be one of {list(_RELATION_DIRECTIONS)}, "
            f"got {relation_direction!r}",
        )
    from . import epistemic_graph, relation_registry

    registry = relation_registry.load_registry(vault_root)
    canonical: list[str] = []
    findings: list[dict[str, str]] = []
    for raw in relations or ():
        resolution = registry.resolve(raw)
        if resolution.canonical is None or resolution.status == "unregistered":
            raise cli_ops.OpError(
                "INVALID_RELATION_FILTER",
                f"unknown relation {raw!r}",
                details={
                    "relation": raw,
                    "suggestions": _nearest_relation_keys(registry, raw),
                },
            )
        canonical.append(resolution.canonical)
        if resolution.status == "deprecated" and resolution.replacement:
            findings.append(
                {
                    "code": "relation_deprecated",
                    "relation": resolution.canonical,
                    "replaced_by": resolution.replacement,
                }
            )
    graph_index = epistemic_graph.EpistemicGraphIndex(vault_root)
    result = graph_index.relation_participants(
        canonical, anchor=relation_of, direction=relation_direction
    )
    if result.status == "temporarily_unavailable":
        raise RetrievalIndexWarming(
            site="relation_graph",
            status="temporarily_unavailable",
        )
    if result.status == "warming":
        epistemic_graph.schedule_background_rebuild(
            vault_root, mutation_coordinator=graph_index._canonical_mutation_coordinator()
        )
        raise RetrievalIndexWarming(site="relation_graph_rebuilding", status="warming")
    return result.paths, dict(result.provenance), tuple(findings)


def _resolve_eligible_filter_paths(
    vault_root: Path,
    *,
    scope: str,
    plan: structured_filters.FilterPlan,
    snapshot: FreshnessSnapshot,
    timings: FindTimings | None = None,
    pending: Any | None = None,
) -> set[str]:
    """Resolve eligible parents, preferring the maintained catalogues.

    A MANAGED reader never reaches the scan oracle. Its eligibility comes from
    `plan_index_eligibility`: every field is index-answerable or the request is
    refused by field, and the candidate set comes from the catalogue's own page
    rows — narrowed where a column can narrow, whole-scope where it cannot.
    Either way nothing enumerates the Markdown scope, which is the contract
    ("Structured Filter Eligibility Resolves From Indexes") and the 18.1 s
    stage the proposal measured.

    An OFFLINE (unmanaged) caller keeps exactly today's behaviour by design: a
    ``complete`` category/kind plan (per ``plan_index_candidates``) seeds from
    the semantic-unit sidecar, a ``complete`` plan with no live index raises
    the typed warming outcome, and an unsupported plan keeps the canonical
    full-scan oracle. A CLI user with a cold catalogue must still get an
    answer, including for the fields no index can evaluate.
    """
    from . import readiness

    if readiness.runtime_managed():
        return _managed_eligible_filter_paths(
            vault_root,
            scope=scope,
            plan=plan,
            snapshot=snapshot,
            timings=timings,
            pending=pending,
        )
    algebra = structured_filters.plan_index_candidates(plan)
    if algebra.status != "complete":
        # The canonical full-scan oracle. It reads every page's frontmatter on
        # the reader thread, which is precisely the cost this stage must never
        # be able to describe as index-backed.
        _mark_source(timings, "filter_eligibility", find_types.SOURCE_COMPUTED)
        return _eligible_filter_paths(
            vault_root, scope=scope, plan=plan, pending=pending
        )
    if algebra.definitely_empty:
        _mark_source(timings, "filter_eligibility", find_types.SOURCE_INDEX)
        _set_catalog_timing_profile(timings, cache_hit=True)
        return set()
    candidate_parents = _indexed_candidate_parent_paths(
        vault_root,
        scope=scope,
        algebra=algebra,
        freshness=snapshot.for_scope(scope),
        timings=timings,
    )
    if candidate_parents is None:
        _mark_source(timings, "filter_eligibility", find_types.SOURCE_DECLINED)
        # A ``complete`` plan must never silently regress to the scan oracle:
        # the sidecar could not answer the safe seed and the lower lexical path
        # has already scheduled the single-flight repair, so the honest outcome
        # is a typed, non-cacheable warming signal — not a false empty or a
        # divergent full-scan ranking.
        raise RetrievalIndexWarming(site="semantic_unit_seed")
    if pending is not None and not pending.empty:
        # The seed names what the maintained metadata holds, which is the
        # previous generation for every identity pending custody owns. Withdraw
        # those seeds and re-offer the committed ones, so the plan is evaluated
        # against the exact current page rather than a stale or removed row.
        candidate_parents = _merge_pending_candidates(
            candidate_parents, pending=pending, scope=scope
        )
    _mark_source(timings, "filter_eligibility", find_types.SOURCE_INDEX)
    return _indexed_eligible_filter_paths(
        vault_root,
        plan=plan,
        candidate_parent_paths=candidate_parents,
        pending=pending,
    )


def _unsupported_filter_fields(plan: structured_filters.FilterPlan) -> tuple[str, ...]:
    """Fields in ``plan`` that no maintained index can evaluate."""
    return structured_filters.plan_index_eligibility(plan).unsupported_fields


def _unsupported_filter_field_error(fields: tuple[str, ...]) -> structured_filters.FilterError:
    """The refusal a managed reader gives instead of silently walking.

    `page.frontmatter:/<pointer>` is open-ended by construction, and
    `unit.verdict` / `unit.check_by` are read off the parsed unit with no column
    anywhere. A scan fallback for them would reintroduce, for one field, exactly
    the whole-corpus cost this stage exists to remove — and would do it
    invisibly, because the result would still be correct.
    """
    named = ", ".join(fields)
    return structured_filters.FilterError(
        "UNSUPPORTED_FILTER_FIELD",
        f"$.{fields[0]}" if fields else "$",
        f"no maintained index can evaluate {named}",
        expected="a filter field resolvable from the maintained catalogues",
        remediation=(
            "Filter on an indexed field, or run the query offline where the "
            "exact source-walk fallback is available."
        ),
    )


def _unsupported_filter_shape_error(
    shapes: tuple[tuple[str, str], ...],
) -> structured_filters.FilterError:
    """The refusal for a shape no catalogue generation can express.

    Deliberately NOT the warming outcome. Warming promises that retrying will
    work — true of a catalogue behind the live projection, false of a
    complement the columns cannot describe — so a caller that retries this one
    retries forever with nothing in the envelope to say so. The fix is on the
    caller's side, so the message names the shape and the remediation names the
    bounds that are day-scoped, and therefore exact, and therefore complement.
    """
    field = shapes[0][0] if shapes else "$"
    named = "; ".join(text for _field, text in shapes)
    return structured_filters.FilterError(
        "UNSUPPORTED_FILTER_FIELD",
        f"$.{field}" if field != "$" else "$",
        f"no maintained index can evaluate {named}",
        expected="a comparison the maintained catalogue can resolve exactly",
        remediation=(
            "Use a whole-day date bound — updated_after, updated_before or "
            "recency_days, or an ISO date rather than a timestamp — which the "
            "catalogue answers exactly and can therefore negate. Or run the "
            "query offline, where the exact source-walk fallback is available."
        ),
    )


def _refused_filter_shapes(
    plan: structured_filters.FilterPlan,
) -> tuple[tuple[str, str], ...]:
    """Shapes that leave a managed reader nothing to resolve the plan by."""
    eligibility = structured_filters.plan_index_eligibility(plan)
    return eligibility.inexpressible if eligibility.refuses else ()


def _managed_eligible_filter_paths(
    vault_root: Path,
    *,
    scope: str,
    plan: structured_filters.FilterPlan,
    snapshot: FreshnessSnapshot,
    timings: FindTimings | None = None,
    pending: Any | None = None,
) -> set[str]:
    """Index-backed eligibility for a managed reader — never a scope walk."""
    eligibility = structured_filters.plan_index_eligibility(plan)
    if not eligibility.resolvable:
        _mark_source(timings, "filter_eligibility", find_types.SOURCE_DECLINED)
        raise _unsupported_filter_field_error(eligibility.unsupported_fields)
    if eligibility.refuses:
        # A shape the columns cannot express, with nothing left to narrow by.
        # Refused rather than deferred: no later generation makes it work.
        _mark_source(timings, "filter_eligibility", find_types.SOURCE_DECLINED)
        raise _unsupported_filter_shape_error(eligibility.inexpressible)
    if not eligibility.narrows:
        # A tautology over the columns. Answering it would mean hydrating the
        # whole scope on the request thread, which is the cost this stage
        # exists to remove — the same cost, reached by a different road, and
        # invisible because the answer would still be correct. Unlike the
        # refusal above this one IS transient in principle, so it keeps the
        # spec's retryable warming outcome.
        _mark_source(timings, "filter_eligibility", find_types.SOURCE_DECLINED)
        raise RetrievalIndexWarming(site="filter_eligibility_unnarrowed")
    try:
        candidate_parents = _eligibility_candidate_paths(
            vault_root,
            scope=scope,
            eligibility=eligibility,
            freshness=snapshot.for_scope(scope),
            timings=timings,
        )
    except RetrievalIndexWarming:
        # A catalogue behind the live projection is the one case where the
        # honest answers are "retry" and "wrong". Walking instead would be
        # correct and would cost the request the whole corpus, which is the
        # regression this stage is measured against.
        _mark_source(timings, "filter_eligibility", find_types.SOURCE_DECLINED)
        raise
    if pending is not None and not pending.empty:
        # The catalogue names the PREVIOUS generation for every identity
        # pending custody owns. Withdraw those rows and re-offer the proven
        # committed ones, so the plan is evaluated against the exact current
        # page rather than a stale row (Decision 2's overlay clause).
        candidate_parents = _merge_pending_candidates(
            candidate_parents, pending=pending, scope=scope
        )
    _set_eligibility_timing_profile(timings, candidates=len(candidate_parents))
    _mark_source(timings, "filter_eligibility", find_types.SOURCE_INDEX)
    return _indexed_eligible_filter_paths(
        vault_root,
        plan=plan,
        candidate_parent_paths=candidate_parents,
        pending=pending,
    )


def _merge_pending_candidates(
    candidates: set[str], *, pending: Any, scope: str
) -> set[str]:
    """Withdraw shadowed rows and re-offer committed ones, within the scope.

    The scope test is the oracle's (`_merge_pending_walk`): a pending identity
    outside the knowledge base belongs to a vault-scoped request only. Without
    it a governed write to `Reference/` becomes eligible for `scope="kb"` and
    reaches the results through the empty-query filter-only lane — a page the
    scope was asked to exclude.
    """
    prefix = kb_prefix()
    return set(pending.shadow(candidates)) | {
        row.rel_path
        for row in pending.current_pages()
        if scope == "vault" or row.rel_path.startswith(prefix)
    }


def _set_eligibility_timing_profile(
    timings: FindTimings | None, *, candidates: int
) -> None:
    """Record how many candidates the index narrowed the scope to.

    The source vocabulary says WHERE a stage's answer came from; this says how
    much the index actually did. A stage reporting `index` over a candidate set
    the size of the corpus has answered from an index and still paid for the
    corpus, and only the count makes that visible.
    """
    if timings is None:
        return
    timings.profile["filter_eligibility"] = {"candidates": candidates}


def _eligibility_candidate_paths(
    vault_root: Path,
    *,
    scope: str,
    eligibility: Any,
    freshness: tuple[int, int, str] | None,
    timings: FindTimings | None = None,
) -> set[str]:
    """Candidate parents for a resolvable plan, from the page catalogue.

    The generation binding is not optional: an incomplete readiness proof
    raises rather than returning a partial set, because at this seam a subset
    of the catalogue is indistinguishable from a correct answer.
    """
    from . import lexstore

    result = lexstore.search_eligible_parent_paths_result(
        vault_root,
        eligibility,
        scope=scope,
        freshness=freshness,
    )
    _set_catalog_timing_profile(timings, result.readiness)
    if not result.readiness.complete:
        _raise_catalog_outcome(result.readiness)
    return set(result.value or [])


def _indexed_candidate_parent_paths(
    vault_root: Path,
    *,
    scope: str,
    algebra: structured_filters.IndexCandidateAlgebra,
    freshness: tuple[int, int, str] | None,
    timings: FindTimings | None = None,
) -> set[str] | None:
    """Distinct candidate parent paths from the maintained semantic metadata.

    The positive category/kind seeds push down into the sidecar so only parents
    that carry a matching unit are returned; this seeds candidates only, so the
    caller still runs canonical access policy and full filter evaluation. None
    signals no usable index (the caller falls back to the walk oracle).
    """
    from . import lexstore

    result = lexstore.search_semantic_parent_paths_result(
        vault_root,
        algebra.clauses,
        scope=scope,
        freshness=freshness,
    )
    _set_catalog_timing_profile(timings, result.readiness)
    if not result.readiness.complete:
        # Preserve None as "incomplete" so the caller raises the typed warming
        # boundary rather than regressing to the scan oracle or a false empty.
        _raise_catalog_outcome(result.readiness)
    return set(result.value or [])


def _indexed_eligible_filter_paths(
    vault_root: Path,
    *,
    plan: structured_filters.FilterPlan,
    candidate_parent_paths: set[str],
    pending: Any | None = None,
) -> set[str]:
    """Evaluate eligibility for indexed candidate parents only — no scope walk.

    Mirrors ``_eligible_filter_paths``' per-parent rules (navigation exclusion,
    access policy on both the candidate and its emitted parent, same-unit
    ``page_matches`` evaluation, emitted-parent collapsing) but sources its
    parents from the maintained index instead of a Markdown walk.
    """
    from . import semantic_index

    def _indexable(page: ParsedPage) -> bool:
        return _passes_filters(
            page,
            vault_root=vault_root,
            types=None,
            projects=None,
            tags=None,
            speakers=None,
            file_types=None,
            exclude_file_types=None,
        )

    eligibility_by_emitted_path: dict[str, bool] = {}
    eligible: set[str] = set()
    for rel_path in candidate_parent_paths:
        if rel_path.rsplit("/", 1)[-1].lower() in _NAVIGATION_BASENAMES:
            continue
        page = _resolve_page(vault_root, rel_path, pending)
        # Access policy always runs before caller filters, including for a
        # scene-frame child whose match is emitted as its parent video.
        if page is None or not _indexable(page):
            continue
        if page.parent_media:
            emitted = (
                _resolve_page(vault_root, page.parent_media + ".md", pending) or page
            )
        else:
            emitted = page
        if not _indexable(emitted):
            continue
        matches = eligibility_by_emitted_path.get(emitted.rel_path)
        if matches is None:
            units: tuple[dict[str, Any], ...] = ()
            if plan.has_unit_predicate:
                try:
                    state = semantic_index.current_parent_index_state(vault_root, emitted.rel_path)
                    units = tuple(
                        structured_filters.unit_view(unit) for unit in state.document.units
                    )
                except (OSError, UnicodeError, ValueError) as error:
                    log.warning(
                        "semantic-unit filter parse failed for %s: %s",
                        emitted.rel_path,
                        error,
                    )
            matches = structured_filters.page_matches(
                plan,
                page=structured_filters.page_view(emitted),
                units=units,
            )
            eligibility_by_emitted_path[emitted.rel_path] = matches
        if matches:
            # Candidate lanes address the child before frame collapsing, while
            # final hits address the emitted parent. Both identities therefore
            # belong to the exact same eligibility set.
            eligible.add(page.rel_path)
            eligible.add(emitted.rel_path)
    return eligible


def _index_resolved_scope_paths(
    vault_root: Path,
    *,
    scope: str,
    freshness: tuple[int, int, str] | None,
) -> set[str] | None:
    """Every governed page in `scope`, resolved from the maintained index.

    The empty-query browse shape is the schema default — `ask_memory.query`
    ships `"default": ""` with "Empty means recent/filtered recall" — and it
    has neither keyword candidates nor a structured filter, so it used to fall
    through to `_walk_md`/`walk_vault_md` and enumerate the whole scope on the
    reader thread. Measured at 19 scope enumerations on a warm managed cell,
    which is precisely what "The Read Path Never Walks The Corpus" forbids.
    `query=""` with any filter already avoided it, so the walk was reachable
    only on the shape the tool surface recommends for browsing.

    A tautology eligibility plan asks the page catalogue for the same set the
    walk would have produced. `_widening_allowed_paths` already does this at
    vault scope; this is the same call at the requested scope, without the
    out-of-KB restriction.

    Returns None when the index cannot answer — warming, stale, or an
    incomplete catalogue — and the caller then keeps today's behaviour. An
    offline/CLI reader is unaffected by design: the scan oracle is its
    documented fallback, and only the MANAGED reader is under the contract.
    """
    from . import lexstore

    try:
        empty = structured_filters.compile_filter(None)
        result = lexstore.search_eligible_parent_paths_result(
            vault_root,
            structured_filters.plan_index_eligibility(empty),
            scope=scope,
            freshness=freshness,
        )
    except RetrievalIndexWarming:
        return None
    except structured_filters.FilterError:
        return None
    if not result.readiness.complete or result.value is None:
        return None
    return set(result.value)


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
    eligible_paths: set[str] | None = None,
    freshness_key: tuple[int, int, str] | None = None,
    failed_out: list[str] | None = None,
    pending: Any | None = None,
    timings: FindTimings | None = None,
) -> list[Hit]:
    """Keyword-mode recall, hydrating only maintained-index matches.

    Reports its own source at runtime rather than accepting the static
    `computed` default: this stage is the hydration lane, it is one of the
    stages that CAN enumerate the scope, and a stage that can walk has to
    decide its label from what it actually did. `index` when it consumed a
    resolved path set, `computed` when it enumerated.
    """
    lexical_repair = _bounded_lexical_repair_allowed(freshness_key)
    if query_norm:
        candidate_paths = _keyword_match_paths(
            vault_root,
            query_norm,
            scope,
            freshness=freshness_key,
            failed_out=failed_out,
            repair=lexical_repair,
            pending=pending,
        )
        if eligible_paths is not None:
            # A finite eligible set (a complete category/kind plan resolved
            # through the maintained index) prunes the keyword candidates before
            # any path construction or parse, so the scan never hydrates a page
            # the structured filter already excludes.
            candidate_paths = [rel for rel in candidate_paths if rel in eligible_paths]
        walk: Iterable[Path] = (vault_root / rel_path for rel_path in candidate_paths)
        _mark_source(timings, "keyword", find_types.SOURCE_INDEX)
    elif eligible_paths is not None:
        # An empty query with a finite eligible set (a complete category/kind
        # plan resolved through the maintained index) iterates those parents
        # directly rather than walking the scope to rediscover them.
        walk = (vault_root / rel_path for rel_path in eligible_paths)
        _mark_source(timings, "keyword", find_types.SOURCE_INDEX)
    elif not lexical_repair:
        from . import lexstore, readiness

        if lexstore.maintained_content_index_enabled():
            catalog = lexstore.get_store(vault_root).catalog_readiness(
                scope,
                freshness_key,
            )
            if not catalog.complete:
                _raise_catalog_outcome(catalog)
        # The empty-query browse shape with no structured filter. A managed
        # reader resolves the scope from the same page catalogue the filtered
        # arm uses instead of enumerating it; an offline reader keeps the walk,
        # which is its documented fallback.
        indexed = (
            _index_resolved_scope_paths(vault_root, scope=scope, freshness=freshness_key)
            if readiness.runtime_managed()
            else None
        )
        if indexed is not None:
            walk = (vault_root / rel_path for rel_path in indexed)
            _mark_source(timings, "keyword", find_types.SOURCE_INDEX)
        elif scope == "kb":
            kb = vault_root / kb_dirname()
            if not kb.is_dir():
                log.error("KB directory missing: %s", kb)
                return []
            walk = _walk_md(kb)
            _mark_source(timings, "keyword", find_types.SOURCE_COMPUTED)
        else:
            from .vault import walk_vault_md

            walk = walk_vault_md(vault_root)
            _mark_source(timings, "keyword", find_types.SOURCE_COMPUTED)
    elif scope == "kb":
        kb = vault_root / kb_dirname()
        if not kb.is_dir():
            log.error("KB directory missing: %s", kb)
            return []
        walk = _walk_md(kb)
        _mark_source(timings, "keyword", find_types.SOURCE_COMPUTED)
    else:
        from .vault import walk_vault_md

        walk = walk_vault_md(vault_root)
        _mark_source(timings, "keyword", find_types.SOURCE_COMPUTED)

    if pending is not None and not pending.empty and not query_norm:
        # The query branch already merged through `_keyword_match_paths`; the
        # filter-only and walk branches merge here, so an empty-query recall
        # sees the same committed identities and none of the shadowed ones.
        walk = _merge_pending_walk(vault_root, walk, pending=pending, scope=scope)

    hits: list[tuple[str, Hit]] = []
    by_path: dict[str, Hit] = {}
    for path in walk:
        if path.name.lower() in _NAVIGATION_BASENAMES:
            continue
        if not recall_policy.is_recall_candidate(vault_root, path):
            continue
        rel_path = _vault_rel(vault_root, path)
        if rel_path is None:
            continue
        page = _resolve_page(vault_root, rel_path, pending)
        if page is None:
            continue
        if eligible_paths is not None and page.rel_path not in eligible_paths:
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
            parent_page = _resolve_page(
                vault_root, page.parent_media + ".md", pending
            )
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
        if not _passes_filters(
            page,
            vault_root=vault_root,
            types=types,
            projects=projects,
            tags=tags,
            speakers=speakers,
            file_types=file_types,
            exclude_file_types=exclude_file_types,
        ):
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
            snapshot_hash=page.snapshot_hash,
        )
        hit.transcript_ts = _transcript_ts_for_hit(page, None, query_norm)
        if (
            hit.scene_frame is None
            and page.media_type == "video"
            and page.media_file
            and hit.transcript_ts is not None
        ):
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
    rerank_max_candidates: int | None = None,
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
    eligible_paths: set[str] | None = None,
    recall_scope: str | None = None,
    retrieval_trace: Any | None = None,
    query_vector_provider: Callable[[], Any] | None = None,
    pending: Any | None = None,
) -> list[Hit]:
    """Hybrid (BM25+vector) or vector-only mode.

    `failed_out` (distinct from `degraded_out`): a POST-WARM lane FAILURE — the
    vector or CLIP `except`, or the all-lanes-empty keyword fallback — appends
    the failed lane name here and bumps the process degradation counter. This is
    the "the lane broke and we silently served a weaker ranking" signal, versus
    `degraded_out`'s "the lane was deferred while a model preload is warming".
    """
    # Lazy imports — keep keyword-mode users out of the torch import path.
    from . import embeddings, lexstore, readiness, scene_frames

    if snapshot is None:
        snapshot = FreshnessSnapshot(vault_root)
    if page_memo is None:
        page_memo = {}

    lexical_freshness = snapshot.for_scope(scope)
    lexical_repair = _bounded_lexical_repair_allowed(lexical_freshness)
    if (
        mode != "vector"
        and not lexical_repair
        and lexstore.maintained_content_index_enabled()
    ):
        catalog = lexstore.get_store(vault_root).catalog_readiness(
            scope,
            lexical_freshness,
        )
        if not catalog.complete:
            _raise_catalog_outcome(catalog)

    def _page_of(rel: str) -> ParsedPage | None:
        if rel not in page_memo:
            page_memo[rel] = _resolve_page(vault_root, rel, pending)
        return page_memo[rel]

    def _keyword_lane(vault_root_arg: Path, *args: Any, **kwargs: Any) -> list[str]:
        return _keyword_match_paths(vault_root_arg, *args, pending=pending, **kwargs)

    try:
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
            keyword_match_paths=_keyword_lane,
            outbound_wikilink_paths=_outbound_wikilink_paths,
            get_query_resolver=lambda root, freshness=None: recall_resolver_snapshot(
                root,
                freshness=freshness,
                allow_fallback=not snapshot.requires_live_recall,
                expected_checkpoint=snapshot.recall_checkpoint("vault"),
            ),
            record_degradation=_record_degradation,
            degraded_out=degraded_out,
            failed_out=failed_out,
            recall_paths=snapshot.recall_paths(recall_scope or scope),
            lexical_repair=lexical_repair,
            eligible_paths=eligible_paths,
            capture_trace=retrieval_trace is not None,
            query_vector_provider=query_vector_provider,
            shadow=(
                pending.shadow
                if pending is not None and not pending.empty
                else None
            ),
        )
    except lexstore.CatalogUnavailable as error:
        _raise_catalog_outcome(error.readiness)

    if not bundle.had_rankings:
        # Both rankers failed or produced nothing. Degrade to keyword.
        log.info("semantic search produced no candidates; falling back to keyword")
        _record_degradation("no_candidates")
        if failed_out is not None:
            failed_out.append("keyword")
        fallback_hits = _find_keyword(
            vault_root,
            query_norm=query_norm,
            types=types,
            projects=projects,
            tags=tags,
            speakers=speakers,
            file_types=file_types,
            exclude_file_types=exclude_file_types,
            limit=limit,
            scope=scope,
            eligible_paths=eligible_paths,
            freshness_key=snapshot.for_scope(scope),
            failed_out=failed_out,
            pending=pending,
            timings=timings,
        )
        if retrieval_trace is not None:
            retrieval_trace.record_keyword_fallback(
                fallback_hits,
                lane_profiles=bundle.lane_statuses,
            )
        _set_rerank_timing_profile(
            timings,
            requested=rerank_max_candidates,
            effective=min(
                3 * limit,
                rerank_max_candidates or MAX_RERANK_CANDIDATES,
            ),
            scorer_input_count=0,
            unscored_tail_count=0,
            decision="skipped",
            reason="no_hits" if not fallback_hits else "candidate_lanes_empty",
        )
        return fallback_hits

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

    if pending is not None and not pending.empty:
        # The lane rankings themselves were shadowed at their source, before
        # fusion and every lane cap. What remains here are the per-path evidence
        # maps, which are tallied outside those lists -- graph in-degree is
        # counted for every neighbour edge, for instance -- and which describe
        # the generation the persistent sidecars still hold.
        chunk_text_by_path = _without_shadowed(chunk_text_by_path, pending)
        vector_score_by_path = _without_shadowed(vector_score_by_path, pending)
        clip_score_by_path = _without_shadowed(clip_score_by_path, pending)
        clip_frame_ts_by_path = _without_shadowed(clip_frame_ts_by_path, pending)
        frame_attribution = _without_shadowed(frame_attribution, pending)
        graph_in_degree_by_path = _without_shadowed(graph_in_degree_by_path, pending)
        graph_provenance_by_path = _without_shadowed(graph_provenance_by_path, pending)

    # Pre-compute per-mode rank lookups so we can tag each Hit's signals.
    vector_rank_by_path = {p: i + 1 for i, p in enumerate(vector_ranking)}
    bm25_rank_by_path = {p: i + 1 for i, p in enumerate(bm25_ranking)}
    keyword_rank_by_path = {p: i + 1 for i, p in enumerate(keyword_ranking)}
    clip_rank_by_path = {p: i + 1 for i, p in enumerate(clip_ranking)}
    keyword_set: set[str] = set(keyword_ranking)
    clip_set: set[str] = set(clip_ranking)
    graph_set = set(graph_ranking)
    vector_paths: set[str] = set(vector_ranking)
    # Degraded-corroboration retention flag — LANE-level, never per-page. When
    # the vector and CLIP lanes produced no candidate at all for this query,
    # no vector evidence exists for ANY candidate. Either the lane never ran —
    # embeddings disabled (EXOMEM_DISABLE_EMBEDDINGS), the model warming/
    # unavailable/failed, or an absent/empty embedding index — or a HEALTHY
    # lane legitimately returned nothing because none of the eligible pages
    # have indexed chunks (`available_nonmatching`: the allowed-paths search
    # had no vectors to score). The relaxed gate deliberately covers that
    # second case too: for these candidates, vector corroboration is
    # unsatisfiable either way. In that state the all-stems veto below would
    # drop every BM25-only candidate for interrogative phrasing ("How many…",
    # "What is…") whose question words never appear in stored text, returning
    # nothing even when BM25 ranks the correct page first. Retention then
    # relaxes to a STRICT MAJORITY of the query's whitespace words being
    # present, at least one of them a content word (see _stem_word_coverage):
    # question words may be missing, but half-matched two-word queries,
    # partially-matched exact-marker compounds, and function-word-only
    # overlap against long prose all stay vetoed. With the vector lane live,
    # the strict all-stems veto is unchanged.
    semantic_lanes_absent = not vector_paths and not clip_set
    # Loop-invariant for the relaxed gate: tokenize/classify the query's words
    # once per query, not once per candidate page.
    degraded_query_words = (
        _query_word_stem_groups(query_norm) if semantic_lanes_absent else []
    )

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
    rerank_candidate_limit = min(
        target_n if may_rerank else 3 * limit,
        rerank_max_candidates or MAX_RERANK_CANDIDATES,
    )
    hits: list[Hit] = []
    seen: set[str] = set()
    with _span(timings, "filter_hits"):
        # Hit construction is one of the four stages the contract names, so it
        # reports what it consumed rather than carrying the static `computed`
        # default. `fused` is a resolved candidate list produced by the ranking
        # lanes, so this is `index` whenever there is one; the label is derived
        # from the input rather than written as a constant, so a future change
        # that feeds this stage an enumeration relabels it instead of hiding
        # inside a reassuring diagnostic.
        _mark_source(
            timings,
            "filter_hits",
            find_types.SOURCE_INDEX if fused is not None else find_types.SOURCE_COMPUTED,
        )
        for rel_path, _score in fused:
            if rel_path in seen:
                continue
            seen.add(rel_path)
            if rel_path.rsplit("/", 1)[-1].lower() in _NAVIGATION_BASENAMES:
                continue
            # Final egress guard: stale/non-projected sidecars can nominate paths,
            # but they must not cause a raw Record to be hydrated even transiently.
            if not recall_policy.is_recall_candidate(vault_root, vault_root / rel_path):
                continue
            page = _page_of(rel_path)
            if page is None:
                continue
            if eligible_paths is not None and rel_path not in eligible_paths:
                continue
            if not _passes_filters(
                page,
                vault_root=vault_root,
                types=types,
                projects=projects,
                tags=tags,
                speakers=speakers,
                file_types=file_types,
                exclude_file_types=exclude_file_types,
            ):
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
                # morphology ("regulation" matching a "regulator" page). With the
                # semantic lanes absent (see flag above) the gate relaxes from
                # all-stems to a strict majority of the query's words anchored by
                # at least one content word, so BM25's own top-ranked evidence
                # survives interrogative phrasing without function-word overlap
                # alone retaining junk.
                if semantic_lanes_absent:
                    present, total, content_present = _stem_word_coverage(
                        page, degraded_query_words
                    )
                    stems_ok = 2 * present > total and content_present > 0
                else:
                    stems_ok = _stem_tokens_present(page, query_norm)
                if not stems_ok:
                    continue
                keyword_excerpt = _stem_anchored_excerpt(page, query_norm)
            elif (
                rel_path in graph_set or rel_path in clip_set or rel_path in frame_attribution
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
                    hit_usage_mult = usage_module.usage_multiplier(hit_activation, config)
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
                snapshot_hash=page.snapshot_hash,
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

    # Resolve the rerank decision. An explicit rerank=True/False always wins;
    # otherwise (rerank is None) auto_rerank consults should_rerank on the built
    # hits. Keeps the reranker model out of the default/test path.
    rerank_outcome: dict[str, Any]
    if not hits:
        do_rerank = False
        rerank_outcome = {"decision": "skipped", "reason": "no_hits"}
    elif rerank is False:
        do_rerank = False
        rerank_outcome = {"decision": "skipped", "reason": "explicit_false"}
    elif rerank is None:
        if not auto_rerank:
            do_rerank = False
            rerank_outcome = {
                "decision": "skipped",
                "reason": "auto_policy_not_allowed",
            }
        else:
            do_rerank = should_rerank(hits, query, config)
            rerank_outcome = {
                "decision": "pending" if do_rerank else "skipped",
                "reason": "auto_policy_selected" if do_rerank else "auto_policy_declined",
            }
    else:
        do_rerank = True
        rerank_outcome = {"decision": "pending", "reason": "explicit_true"}

    if do_rerank and not embeddings.ranking_enabled():
        do_rerank = False  # EXOMEM_DISABLE_RANKING — hard off, even for explicit rerank=True
        rerank_outcome = {"decision": "skipped", "reason": "hard_disabled"}

    if do_rerank and readiness.should_defer("reranker"):
        # Background warm-up owns the reranker load right now — calling
        # rerank_pairs would block on the singleton lock. Skip; caller marks
        # the response as warming.
        if degraded_out is not None:
            degraded_out.append("reranker")
        do_rerank = False
        rerank_outcome = {"decision": "deferred", "reason": "model_warming"}

    if timings is not None and not (do_rerank and hits):
        timings.skipped("rerank")
    scorer_input_count = 0
    unscored_tail_count = max(0, len(hits) - rerank_candidate_limit)
    if do_rerank and hits:
        with _span(timings, "rerank"):
            rerank_prefix = hits[:rerank_candidate_limit]
            scorer_input_count = len(rerank_prefix)
            try:
                from . import embeddings as emb

                # Best passage for each hit: the matched chunk when we have one,
                # else the leading body slice.
                passages: list[str] = []
                for h in rerank_prefix:
                    ctext = chunk_text_by_path.get(h.path)
                    if ctext:
                        passages.append(ctext)
                    else:
                        pg = _page_of(h.path)
                        body = (pg.body if pg else "") or h.excerpt
                        passages.append(body[:1500])  # CrossEncoder caps at 512 tokens
                scores = emb.rerank_pairs(query, passages)
                if len(scores) != len(rerank_prefix):
                    raise ValueError("reranker returned a score count that does not match its inputs")
                score_updates: list[
                    tuple[
                        Hit,
                        int,
                        float,
                        float,
                        list[dict[str, float | str]] | None,
                    ]
                ] = []
                for input_rank, (h, s) in enumerate(zip(rerank_prefix, scores, strict=True), start=1):
                    raw_score = float(s)
                    adjusted = raw_score
                    chain: list[dict[str, float | str]] | None = (
                        [] if retrieval_trace is not None else None
                    )
                    if prefer_compiled:
                        factor = _type_multiplier(h.type, config)
                        before = adjusted
                        adjusted *= factor
                        if chain is not None:
                            chain.append(
                                {
                                    "name": "type",
                                    "factor": factor,
                                    "before": before,
                                    "after": adjusted,
                                }
                            )
                    if prefer_active:
                        factor = _status_multiplier(h.status, config)
                        before = adjusted
                        adjusted *= factor
                        if chain is not None:
                            chain.append(
                                {
                                    "name": "status",
                                    "factor": factor,
                                    "before": before,
                                    "after": adjusted,
                                }
                            )
                    if usage_map and h.usage_boost_applied:
                        factor = h.usage_boost_applied
                        before = adjusted
                        adjusted *= factor
                        if chain is not None:
                            chain.append(
                                {
                                    "name": "usage",
                                    "factor": factor,
                                    "before": before,
                                    "after": adjusted,
                                }
                            )
                    score_updates.append((h, input_rank, raw_score, adjusted, chain))
                # Commit annotations only after every returned score and multiplier
                # has been validated. A late conversion/application failure must
                # leave the fused fallback free of partial reranker evidence.
                for h, input_rank, raw_score, adjusted, chain in score_updates:
                    h.rerank_input_rank = input_rank
                    h.rerank_raw_score = raw_score
                    h.rerank_score = adjusted
                    if chain is not None:
                        h.rerank_multiplier_chain = chain
                hits = _order_reranked_prefix(
                    hits,
                    prefix_count=len(rerank_prefix),
                )
                rerank_outcome = {"decision": "ran", "reason": "ran"}
            except ImportError as e:
                log.warning("rerank requested but reranker unavailable: %s", e)
                if timings is not None:
                    timings.error("rerank", e)
                rerank_outcome = {
                    "decision": "unavailable",
                    "reason": "dependency_unavailable",
                }
            except runtime_resources.ModelBusyError:
                raise
            except Exception as e:  # noqa: BLE001 - optional reranker must soft-fail.
                log.warning("rerank failed: %s; returning fused order", e)
                if timings is not None:
                    timings.error("rerank", e)
                rerank_outcome = {"decision": "failed", "reason": "runtime_failure"}

    _set_rerank_timing_profile(
        timings,
        requested=rerank_max_candidates,
        effective=rerank_candidate_limit,
        scorer_input_count=scorer_input_count,
        unscored_tail_count=unscored_tail_count,
        decision=rerank_outcome["decision"],
        reason=rerank_outcome["reason"],
    )

    final_hits = hits[:limit]
    if retrieval_trace is not None:
        retrieval_trace.record_page_candidates(
            bundle,
            final_hits,
            reranker_model=embeddings.RERANKER_NAME,
            rerank_outcome=rerank_outcome,
            scorer_input_count=scorer_input_count,
            unscored_tail_count=unscored_tail_count,
        )
    return final_hits


def _widening_allowed_paths(
    vault_root: Path,
    *,
    plan: structured_filters.FilterPlan | None,
    snapshot: FreshnessSnapshot,
    freshness: tuple | None,
) -> set[str] | None:
    """The out-of-KB eligible set for the reserve, or None to decline.

    One classification and one page-catalogue query, both at vault scope, so
    the reserve ranks exactly the pages a filter admits outside the knowledge
    base. Two shapes reach here:

    * a plan — the managed eligibility resolution already evaluates it exactly
      against the catalogue's candidates, so the answer is that set with the
      knowledge base removed;
    * no plan — `plan_index_eligibility` classifies the empty plan as a
      tautology, which the page catalogue answers as "every in-scope page".
      That is a legitimate resolution, not a failure: the question really is
      "everything outside the knowledge base", and answering it from `pages`
      is what keeps the lexical query off an over-fetch cap that KB pages
      dominate.

    None is the decline: a catalogue that cannot answer for this generation
    must not be substituted by a scan, and must not raise either — the KB
    results the caller already has are returned unchanged.
    """
    from . import lexstore

    try:
        if plan is not None:
            eligible = _resolve_eligible_filter_paths(
                vault_root,
                scope="vault",
                plan=plan,
                snapshot=snapshot,
            )
        else:
            empty = structured_filters.compile_filter(None)
            result = lexstore.search_eligible_parent_paths_result(
                vault_root,
                structured_filters.plan_index_eligibility(empty),
                scope="vault",
                freshness=freshness,
            )
            if not result.readiness.complete or result.value is None:
                return None
            eligible = set(result.value)
    except RetrievalIndexWarming:
        return None
    except structured_filters.FilterError:
        return None
    prefix = kb_prefix()
    return {path for path in eligible if not path.startswith(prefix)}


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
    filter_plan: structured_filters.FilterPlan | None = None,
    exclude_paths: set[str] | None = None,
    failed_out: list[str] | None = None,
    retrieval_trace: Any | None = None,
    timings: FindTimings | None = None,
) -> list[Hit]:
    """BM25/keyword recall over the vault, RESTRICTED to paths outside
    `Knowledge Base/`. Powers the requested `scope="kb"` widening.

    Recall here is BM25-only (the vector lane already searches the WHOLE sidecar,
    so under `EXOMEM_INDEX_SCOPE=vault` out-of-KB notes surface semantically via
    that lane — this widener adds lexical out-of-KB recall on top), with a
    RELAXED gate: a candidate survives when at least one query stem is present,
    not the strict all-tokens-present gate the KB path enforces. Terse,
    frontmatter-less files (e.g. a numbers-heavy workout tracker) would
    otherwise be filtered out by any natural-language query that includes a
    word they don't literally contain.

    A MANAGED reader answers from the maintained catalogue and nothing else:
    one lexical query restricted to the index-resolved out-of-KB eligible set,
    with no repair, no in-process corpus and no walk. A catalogue that cannot
    serve the query DECLINES — the widening is omitted and the knowledge-base
    results are returned unchanged, because both alternatives are worse than
    saying so: a stale answer is silently wrong, and a corpus build is the
    7.6 s this stage exists to remove.

    An OFFLINE (unmanaged) caller keeps exactly today's path, including the
    in-process rung. A CLI user against a cold vault has no catalogue to
    consult and must still get the out-of-KB page.
    """
    if not query_norm or limit < 1:
        return []
    from . import bm25, lexstore, readiness

    managed = readiness.runtime_managed()
    vault_freshness = snapshot.for_scope("vault") if snapshot is not None else None
    snapshot = snapshot or FreshnessSnapshot(vault_root)
    #: Applied per candidate AFTER ranking. Only the unrestricted rungs need
    #: it: the catalogue query is already restricted to the eligible set, and
    #: re-testing there would make the restriction unfalsifiable — a reserve
    #: query that lost its `allowed_paths` would still look correct.
    post_eligibility: set[str] | None = None

    if managed:
        # The eligible set for the reserve, resolved from the SAME page
        # catalogue the knowledge-base eligibility reads (Lane 2's
        # `plan_index_eligibility` at vault scope). With no filter plan the
        # classifier is a tautology and the set is every non-KB page the
        # catalogue holds — which is what "outside the knowledge base" means.
        allowed_outside = _widening_allowed_paths(
            vault_root,
            plan=filter_plan,
            snapshot=snapshot,
            freshness=vault_freshness,
        )
        if allowed_outside is None:
            _mark_source(timings, "outside_kb", find_types.SOURCE_DECLINED)
            return []
    else:
        eligible_paths = (
            _resolve_eligible_filter_paths(
                vault_root,
                scope="vault",
                plan=filter_plan,
                snapshot=snapshot,
            )
            if filter_plan is not None
            else None
        )
        post_eligibility = eligible_paths
        allowed_outside = (
            {path for path in eligible_paths or () if not path.startswith(kb_prefix())}
            if eligible_paths is not None
            else None
        )
    # A structured filter ranks the EXACT outside-KB eligible set, so no
    # eligible page can be buried below an over-fetch cap — the per-candidate
    # gates below reject freely, and a short k would underfill. Without a
    # filter the set is the whole non-KB corpus, so the cap stays: it is a
    # bound on work, not on recall, and now that the query itself excludes the
    # knowledge base the cap is no longer spent on KB rows.
    bm25_k = (
        max(limit, len(allowed_outside))
        if allowed_outside is not None and filter_plan is not None
        else max(limit * 5, 100)
    )
    candidates: list[str] = []
    score_by_path: dict[str, float] = {}
    lexical_backend = lexstore.cache_token(vault_root)
    catalog_served = False
    try:
        lexical_repair = (not managed) and _bounded_lexical_repair_allowed(vault_freshness)
        bm25_hits: list[tuple[str, float]] | None
        if (
            not lexical_repair
            and lexstore.maintained_content_index_enabled()
        ):
            catalog_result = lexstore.search_bm25_result(
                vault_root,
                query,
                bm25_k,
                scope="vault",
                freshness=vault_freshness,
                allowed_paths=allowed_outside,
            )
            if not catalog_result.readiness.complete:
                if managed:
                    _mark_source(timings, "outside_kb", find_types.SOURCE_DECLINED)
                    return []
                _raise_catalog_outcome(catalog_result.readiness)
            bm25_hits = list(catalog_result.value or [])
            catalog_served = True
        elif managed:
            # Only reachable with the sidecar retired outright, which leaves a
            # managed reader no index to widen from.
            _mark_source(timings, "outside_kb", find_types.SOURCE_DECLINED)
            return []
        else:
            bm25_hits = lexstore.search_bm25(
                vault_root,
                query,
                bm25_k,
                scope="vault",
                freshness=vault_freshness,
                allowed_paths=allowed_outside,
                repair=lexical_repair,
            )
        if bm25_hits is None:
            if lexstore.backend() == "python":
                # An explicit operator rollback is allowed to use the old
                # in-process corpus. Automatic sidecar failure is not.
                bm25_hits = bm25.search(
                    vault_root,
                    query,
                    k=bm25_k,
                    scope="vault",
                    freshness=vault_freshness,
                    allowed_paths=allowed_outside,
                    repair=lexical_repair,
                )
                lexical_backend = "keyword_fallback"
            else:
                if failed_out is not None:
                    failed_out.append("outside_kb_lexical")
                _record_degradation("outside_kb_lexical")
                return []
        for path, _score in bm25_hits:
            # Skip the prefix re-test only when the query was ACTUALLY
            # restricted to the out-of-KB eligible set: then its rows need no
            # second opinion, and must not get one, or the restriction stops
            # being the thing that keeps a knowledge-base page out of the
            # reserve (mutant M4). `catalog_served` alone is not that
            # condition — an OFFLINE reader above the inline-repair page cap
            # reaches the same catalogue query with `allowed_outside` None
            # whenever no filter narrowed it, and an unrestricted vault-scope
            # query returns knowledge-base rows.
            restricted = catalog_served and allowed_outside is not None
            if restricted or not path.startswith(kb_prefix()):
                candidates.append(path)
                score_by_path[path] = float(_score)
    except RetrievalIndexWarming:
        if managed:
            _mark_source(timings, "outside_kb", find_types.SOURCE_DECLINED)
            return []
        raise
    except Exception as e:  # noqa: BLE001 — widening must never break find
        log.warning("requested widening lexical sidecar failed: %s", e)
        if managed:
            # `declined` is the managed reader's contract: it asked an index
            # and the index could not answer. An offline reader that fell over
            # mid-scan DEGRADED, and says so through `failed_out` and the
            # degradation counter; calling that `declined` would report a lane
            # that broke as a lane that politely stood down.
            _mark_source(timings, "outside_kb", find_types.SOURCE_DECLINED)
        if failed_out is not None:
            failed_out.append("outside_kb_lexical")
        _record_degradation("outside_kb_lexical")
        return []
    if catalog_served:
        _mark_source(timings, "outside_kb", find_types.SOURCE_INDEX)

    hits: list[Hit] = []
    seen: set[str] = set()
    for rel_path in candidates:
        if rel_path in seen or (exclude_paths is not None and rel_path in exclude_paths):
            continue
        seen.add(rel_path)
        if rel_path.rsplit("/", 1)[-1].lower() in _NAVIGATION_BASENAMES:
            continue
        page = _CACHE.get(vault_root / rel_path, vault_root)
        if page is None:
            continue
        if post_eligibility is not None and rel_path not in post_eligibility:
            continue
        if not _passes_filters(
            page,
            vault_root=vault_root,
            types=types,
            projects=projects,
            tags=tags,
            speakers=speakers,
            file_types=file_types,
            exclude_file_types=exclude_file_types,
        ):
            continue
        # Relaxed gate: BM25 score>0 already implies a token match, but the
        # keyword fallback path needs this explicit check.
        if not _any_stem_present(page, query_norm):
            continue
        excerpt = _stem_anchored_excerpt(page, query_norm)
        hits.append(
            Hit(
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
                snapshot_hash=page.snapshot_hash,
            )
        )
        if len(hits) >= limit:
            break
    if retrieval_trace is not None:
        retrieval_trace.record_outside_candidates(
            hits,
            scores=score_by_path,
            backend=lexical_backend,
        )
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


def _query_word_stem_groups(query_norm: str) -> list[tuple[list[str], bool]]:
    """Per whitespace word: (BM25 subtoken stems, is_function_word).

    Loop-invariant precompute for `_stem_word_coverage` — the query is
    tokenized and classified once per query, not once per candidate page. A
    word is a function word only when EVERY subtoken stem is a function-word
    stem, so a compound like `state-of-the-art` stays a content word. Words
    with no `[a-z0-9]` content tokenize to nothing and are skipped; the
    tokenizer is ASCII-only, so non-ASCII words drop out of the denominator
    (known limit: mixed-script queries are gated more permissively than
    v0.36.0's all-stems veto).
    """
    return find_policy.query_word_stem_groups(query_norm)


def _stem_word_coverage(
    page: ParsedPage, word_stem_groups: list[tuple[list[str], bool]]
) -> tuple[int, int, int]:
    """(present, total, content_present) coverage over precomputed word groups.

    The coverage unit is the WHOLE whitespace word (see
    `_query_word_stem_groups`), and a word counts as present only when EVERY
    one of its BM25 subtoken stems appears in title+body: a compound like
    `alpha-beta-gamma` needs all three parts (so exact-marker queries stay
    precise), while trailing punctuation (`measure?` → `measur`) cannot mask
    a real match. `content_present` counts present words that are NOT
    function words: the degraded-corroboration gate requires a strict
    majority present (2 * present > total) AND at least one content word
    among them, so "what is the … of the …" phrasing cannot ride its
    function words into retention against long prose. Sits between
    `_any_stem_present` (>=1 stem anywhere) and `_stem_tokens_present` (ALL
    whitespace tokens, whole-token stems).
    """
    return find_policy.stem_word_coverage(page.stem_set, word_stem_groups)


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
    return find_policy.apply_temporal_boost(fused, query, _page_of(vault_root), config)


def _recency_ranking(candidate_paths: list[str], vault_root: Path, cap: int) -> list[str]:
    return find_policy.recency_ranking(candidate_paths, _page_of(vault_root), cap)


def _keyword_match_paths(
    vault_root: Path,
    query_norm: str,
    scope: str,
    freshness: tuple | None = None,
    failed_out: list[str] | None = None,
    repair: bool = True,
    k: int | None = None,
    pending: Any | None = None,
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

    `k` bounds the candidate set. Every other lane in `find_candidates` already
    takes one; this one took none, so it answered with every page in the vault
    that matched, and the fuse and the eligibility filter then paid for all of
    them on every call (#516). The bound belongs here rather than at the caller
    because the result is ordered by `updated` desc: a caller that sliced the
    return value would be slicing recency order, which is only correct if the
    order was applied first -- which is exactly what the sidecar's `LIMIT` and
    the truncation below do.

    Both backends bound the result identically, so parity holds at any `k`.

    `pending` supplies the exact committed generations the persistent catalogue
    has not published yet. Its identities shadow every catalogue row they own --
    only the overlay can attest that generation -- and are merged back ahead of
    the surviving rows: the lane orders by `updated` descending, a pending page
    is by construction the newest committed generation of its identity, and the
    catalogue's own `updated` values are not available here without a parse per
    row. The merged bound is `k` plus the number of admitted pending rows,
    and the persistent side is over-fetched by the same count: leading rows
    that displaced settled ones would shorten the catalogue half of a
    bounded lane, so each side keeps its own budget instead of competing
    for one. Callers apply their own `limit` after re-sorting.
    """
    merge_pending = pending is not None and not pending.empty
    admitted_pending: list[str] = []
    if merge_pending and query_norm:
        admitted_pending = _pending_keyword_paths(
            vault_root,
            pending=pending,
            query_norm=query_norm,
            scope=scope,
        )
    # The persistent side is over-fetched by exactly the number of pending rows
    # that will lead it, so merging cannot evict a catalogue row the unmerged
    # lane would have kept. Without this, a burst of pending writes silently
    # shortens the settled half of a bounded lane.
    persistent_k = k if k is None else max(0, int(k)) + len(admitted_pending)

    def _merged(paths: list[str]) -> list[str]:
        if not merge_pending:
            return paths
        seen = set(admitted_pending)
        merged = admitted_pending + [
            rel_path for rel_path in pending.shadow(paths) if rel_path not in seen
        ]
        return merged if k is None else merged[: max(0, int(k)) + len(admitted_pending)]

    if not query_norm:
        return []
    from . import lexstore

    if not repair and lexstore.maintained_content_index_enabled():
        catalog_result = lexstore.search_substring_result(
            vault_root,
            query_norm,
            scope=scope,
            freshness=freshness,
            k=persistent_k,
        )
        if not catalog_result.readiness.complete:
            _raise_catalog_outcome(catalog_result.readiness)
        return _merged(list(catalog_result.value or []))
    indexed = lexstore.search_substring(
        vault_root,
        query_norm,
        scope=scope,
        freshness=freshness,
        repair=repair,
        k=persistent_k,
    )
    if indexed is not None:
        return _merged(indexed)
    if lexstore.backend() != "python":
        # The sidecar declined. `search_substring` documents exactly one meaning
        # for None -- "fall back" -- and its causes are a retired store, a
        # catalog that is not on disk, a sqlite error, or a sync that could not
        # take the publication lock. Every one of those is "I could not check".
        #
        # Answering [] said "there is nothing", which is a different claim, and
        # for a page a governed write has just committed it is a false empty:
        # the lexical upsert defers while the write holds the vault lock
        # (#526), so the one moment the catalog is most likely to decline is the
        # moment right after a write, on exactly the page the caller is most
        # likely to be looking for. Recall going silently blank on the newest
        # content is the worst possible failure for a memory system, and the
        # only evidence was a trace marker nobody reads mid-query.
        #
        # So the marker stays -- the lane really did degrade and the counter
        # should say so -- and the answer comes from the reference scan below
        # instead of from an empty list. That is not a second implementation of
        # the contract: `test_keyword_lane_parity_with_reference_scan` asserts
        # the sidecar and this scan return IDENTICAL ordered lists across 15
        # query shapes, so falling through costs latency and changes nothing
        # else. On a healthy sidecar this branch never runs, so no warm read
        # pays for it.
        if failed_out is not None:
            failed_out.append("keyword_lexical")
        _record_degradation("keyword_lexical")
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
        if not recall_policy.is_recall_candidate(vault_root, path):
            continue
        rel_path = _vault_rel(vault_root, path)
        if rel_path is None:
            continue
        page = _resolve_page(vault_root, rel_path, pending)
        if page is None:
            continue
        if _make_excerpt(page, query_norm) is None:
            continue
        matches.append((page.updated or "0000-00-00", page.rel_path))
    matches.sort(reverse=True)  # most-recent first
    bounded = matches if persistent_k is None else matches[: max(0, int(persistent_k))]
    return _merged([p for _, p in bounded])


def _outbound_wikilink_paths(
    page: ParsedPage,
    vault_root: Path,
    resolver=None,
    *,
    allowed_paths: AbstractSet[str] | None = None,
) -> list[str]:
    """Vault-relative POSIX paths (no .md) that this page's body links to.

    Skips matches inside fenced code blocks and inline code (delegates to
    vault.find_body_wikilinks). Targets are normalised through
    `normalize_wikilink` so bare / KB-stripped / aliased forms all resolve to
    the same canonical path. Unresolvable targets and folder-hub links
    (trailing `/`) are dropped. `#anchor` is stripped — anchors are intra-
    page jumps, not separate files. Pass `resolver` to reuse one across a
    request (the graph lane does); None builds/reuses the process cache.
    ``allowed_paths`` may provide that request's exact checkpoint-bound recall
    projection, avoiding a filesystem policy walk for every resolved link.
    Callers without such a snapshot retain the live policy check.
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
        except Exception:  # noqa: BLE001 - malformed links are skipped during ranking.
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
        if (
            rel_with_md not in allowed_paths
            if allowed_paths is not None
            else not recall_policy.is_recall_candidate(vault_root, vault_root / rel_with_md)
        ):
            continue
        if rel_with_md in seen:
            continue
        seen.add(rel_with_md)
        out.append(rel_with_md)
    return out


_RESOLVER_CACHE: dict[Path, tuple[tuple, object]] = {}
_RECALL_RESOLVER_CACHE: dict[Path, tuple[tuple, object]] = {}
# The writer resolver has the same event-stream contract as the projected
# resolver: it may advance only from the exact broad checkpoint that built it.
_RESOLVER_CHECKPOINTS: dict[Path, freshness.FreshnessCheckpoint] = {}
# A projected resolver can advance only from the exact recall-event checkpoint
# that produced its maps.  The cache identity is deliberately kept separate
# for compatibility with callers that supply a direct-disk freshness proof.
_RECALL_RESOLVER_CHECKPOINTS: dict[Path, freshness.RecallFreshnessCheckpoint] = {}
_RESOLVER_LOCK = threading.Lock()

#: Vaults with a projected-resolver build already running.
#:
#: `recall_resolver_snapshot` builds from a full `walk_vault_md` plus a parse
#: per admitted page -- O(vault), and measured at 30.7s of a 34s `ask_memory`
#: call on a 2.4k-page vault, which is 90% of that read (#676). Nothing bounded
#: it: eviction is cheap and asynchronous while the rebuild is expensive and
#: synchronous, so the whole cost landed on whichever caller asked first, and
#: on the read path that is a reader.
#:
#: Two things use this. Concurrent callers on a cold cache wait for one build
#: instead of each running their own, and `_evict_recall_resolver` starts a
#: background rebuild so the gap between an eviction and the next warm cache is
#: closed by a daemon thread rather than by the next query. Neither changes what
#: a caller gets -- a build still returns the same resolver, and a caller that
#: arrives mid-build still waits -- so ranking is untouched.
_RECALL_RESOLVER_BUILDS: dict[Path, threading.Event] = {}
#: Its own lock, NOT `_RESOLVER_LOCK`: `_evict_recall_resolver` runs while that
#: one is held, and the build below must be able to register itself without
#: holding a lock across a whole-vault walk.
_RECALL_REBUILD_LOCK = threading.Lock()
#: Long enough that a follower waits out a real build on a large vault rather
#: than duplicating it, short enough that a wedged leader cannot hold a reader
#: indefinitely -- the follower simply builds its own after this.
#:
#: Applies to callers that may FALL BACK to building their own resolver (CLI,
#: cold, warm-up paths). A managed request cannot use it: see the follower
#: bound below.
_RECALL_RESOLVER_BUILD_WAIT_SECONDS = 120.0

#: The bound for a MANAGED request, which cannot fall back.
#:
#: The leader's build is a full vault walk plus a parse per admitted page, so
#: on a 3.3k-file vault it runs for tens of seconds. Letting a request wait the
#: full 120s above is not an optimisation: it is longer than any client
#: timeout, and it is how a warm, converged cell produced hybrid refusals at
#: exactly the 60s client deadline with the server never answering, while
#: keyword (which needs no projected resolver) served in 2.1s.
#:
#: A refusal has to come back fast enough to BE a refusal, so a managed
#: follower waits briefly, then declines with `retry_after_ms` instead of
#: either blocking or duplicating the leader's whole-vault walk.
_RECALL_RESOLVER_FOLLOWER_WAIT_SECONDS = 3.0
_RECALL_RESOLVER_REBUILDS: set[Path] = set()


def _follower_wait_seconds(*, allow_fallback: bool) -> float:
    """How long this caller may wait on the single-flight resolver leader.

    A managed request cannot fall back to its own whole-vault build, so it gets
    the short bound and declines afterwards. A caller that MAY fall back keeps
    the long wait, because for it waiting really is cheaper than duplicating
    the walk.

    Its own function so the choice is assertable without having to sit through
    the wait it selects — a guard that can only be tested by hanging is a guard
    nobody tests.
    """
    return (
        _RECALL_RESOLVER_BUILD_WAIT_SECONDS
        if allow_fallback
        else _RECALL_RESOLVER_FOLLOWER_WAIT_SECONDS
    )


def _recall_checkpoint_identity(
    checkpoint: freshness.RecallFreshnessCheckpoint,
) -> tuple[tuple[int, int, str], str, str]:
    return (
        checkpoint.triple,
        checkpoint.policy_version,
        checkpoint.access_policy_fingerprint,
    )


def _evict_recall_resolver(root: Path) -> None:
    """Drop the projected resolver, and start rebuilding it off the read path.

    Callers hold `_RESOLVER_LOCK`; the rebuild is scheduled, never run, here.

    Every eviction site in `on_resolver_files_changed` is a correctness refusal
    -- an incomplete event delta, a moved path guard, a policy identity that
    advanced -- and each is right to drop the cache. What they cannot do is
    leave the vault with no resolver and no plan to get one, because the next
    caller to need it pays a whole-vault walk and parse in the foreground.
    Scheduling the rebuild here is what makes the eviction cheap for everyone
    except a daemon thread.
    """
    _RECALL_RESOLVER_CACHE.pop(root, None)
    _RECALL_RESOLVER_CHECKPOINTS.pop(root, None)
    _schedule_recall_resolver_rebuild(root)


def _schedule_recall_resolver_rebuild(root: Path) -> None:
    """Start at most one background projected-resolver build per vault.

    Best-effort and deliberately silent on failure: this exists to make the
    next reader fast, and a vault that cannot be walked right now will simply
    be built by whoever needs it, exactly as before. Never blocks the caller,
    which may be holding `_RESOLVER_LOCK` or serving a request.
    """
    if os.environ.get("EXOMEM_DISABLE_RESOLVER_WARM"):
        return
    key = Path(root)
    with _RECALL_REBUILD_LOCK:
        if key in _RECALL_RESOLVER_REBUILDS:
            return
        _RECALL_RESOLVER_REBUILDS.add(key)

    def _run() -> None:
        try:
            recall_resolver_snapshot(key)
        except Exception as error:  # noqa: BLE001 - a daemon must not escape
            log.warning("projected resolver background rebuild skipped (%s)", error)
        finally:
            with _RECALL_REBUILD_LOCK:
                _RECALL_RESOLVER_REBUILDS.discard(key)

    # A literal thread name, like every other exomem thread: names reach the
    # log through `JsonLinesFormatter`, and hosted-cell redaction blanks a
    # record's fields rather than rebuilding it from an allowlist, so a vault
    # path encoded in a thread name would survive that boundary.
    thread = threading.Thread(
        target=_run,
        name="exomem-recall-resolver-warm",
        daemon=True,
    )
    try:
        thread.start()
    except RuntimeError:
        with _RECALL_REBUILD_LOCK:
            _RECALL_RESOLVER_REBUILDS.discard(key)


def await_recall_resolver_warm(timeout: float = 30.0) -> bool:
    """Block until no background projected-resolver build is running.

    A quiesce seam, not a convergence helper: it says nothing about whether a
    resolver is now cached, only that no daemon thread is still walking a
    vault. Production is one long-lived process where a warm thread outliving
    its trigger is the point; a test suite is many vaults in one process,
    where a thread outliving the tmp vault that started it is a cross-test
    leak. Mirrors `graph_sync.await_active_rebuild`.
    """
    deadline = time.monotonic() + timeout
    while True:
        with _RECALL_REBUILD_LOCK:
            if not _RECALL_RESOLVER_REBUILDS:
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def _evict_resolver(root: Path) -> None:
    _RESOLVER_CACHE.pop(root, None)
    _RESOLVER_CHECKPOINTS.pop(root, None)


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
    from . import freshness as freshness_module
    from .vault import WikilinkResolver

    root = Path(vault_root)
    if freshness is None:
        freshness = FreshnessSnapshot(root).vault()
    checkpoint = freshness_module.consumer_checkpoint(root, "vault")
    if checkpoint.triple != freshness:
        checkpoint = None
    cached = _RESOLVER_CACHE.get(root)
    if cached and cached[0] == freshness:
        return cached[1]
    with _RESOLVER_LOCK:
        cached = _RESOLVER_CACHE.get(root)
        if cached and cached[0] == freshness:
            return cached[1]
        resolver = WikilinkResolver(root)
        _RESOLVER_CACHE[root] = (freshness, resolver)
        if checkpoint is not None:
            _RESOLVER_CHECKPOINTS[root] = checkpoint
        else:
            _RESOLVER_CHECKPOINTS.pop(root, None)
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


def recall_resolver_snapshot(
    vault_root: Path,
    freshness: tuple | None = None,
    *,
    allow_fallback: bool = True,
    expected_checkpoint: freshness.RecallFreshnessCheckpoint | None = None,
):
    """Resolver for ordinary recall and graph expansion only.

    Unlike the writer resolver, this view is intentionally constructed from
    policy-admitted paths.  The admission check precedes cache hydration, so a
    raw Records filename/stem/title can neither resolve a link nor collide with
    an ordinary note in the graph lane.
    """
    from . import freshness as freshness_module
    from . import lexstore
    from .vault import WikilinkResolver, walk_vault_md

    root = Path(vault_root)
    # Callers that already measured disk freshness (notably the graph rebuild)
    # must not pay another broad/event-registry walk just to warm this resolver.
    # The key remains distinct from the broad writer resolver by policy identity.
    freshness_key = (
        freshness
        if freshness is not None
        else _recall_checkpoint_identity(expected_checkpoint)
        if expected_checkpoint is not None
        else FreshnessSnapshot(root).projection_key("vault")
    )
    policy_version, access_fingerprint = recall_policy.recall_policy_identity(root)
    if (
        isinstance(freshness_key, tuple)
        and len(freshness_key) == 3
        and isinstance(freshness_key[0], tuple)
        and freshness_key[1:] == (policy_version, access_fingerprint)
    ):
        # A query's projected request key is already the complete resolver
        # identity.  Reuse it verbatim rather than nesting it and accidentally
        # separating otherwise identical warmed snapshots.
        identity = freshness_key
    else:
        identity = (freshness_key, policy_version, access_fingerprint)
    checkpoint: freshness_module.RecallFreshnessCheckpoint | None = None
    candidate = expected_checkpoint
    if candidate is not None and not allow_fallback:
        if not freshness_module.recall_checkpoint_is_current(root, "vault", candidate):
            lexstore.request_repair(root)
            raise RetrievalIndexWarming(
                site="resolver_checkpoint_stale",
                status="temporarily_unavailable",
            )
    if candidate is None:
        candidate = freshness_module.live_recall_checkpoint(root, "vault")
    if candidate is None and allow_fallback:
        candidate = freshness_module.recall_checkpoint(root, "vault")
    if candidate is not None and identity == _recall_checkpoint_identity(candidate):
        checkpoint = candidate
    if not allow_fallback and checkpoint is None:
        lexstore.request_repair(root)
        raise RetrievalIndexWarming(
            site="resolver_checkpoint_absent",
            status="temporarily_unavailable",
        )
    with _RESOLVER_LOCK:
        cached = _RECALL_RESOLVER_CACHE.get(root)
        if cached and cached[0] == identity:
            return cached[1].fork()

    # Single-flight. The build below is a full vault walk plus a parse per
    # admitted page, so N callers arriving on a cold cache used to run N of
    # them concurrently -- each reading the same files, each producing a
    # resolver N-1 of them would discard. Followers wait on the leader's event
    # and then re-check the cache, which is where the leader publishes.
    #
    # A follower whose wait times out, or whose leader published a different
    # identity, falls through and builds its own. That is the pre-existing
    # behaviour and it stays correct; the wait is an optimisation, never a
    # dependency.
    leader = False
    follower_wait = _follower_wait_seconds(allow_fallback=allow_fallback)
    waited_started = time.monotonic()
    while True:
        with _RECALL_REBUILD_LOCK:
            building = _RECALL_RESOLVER_BUILDS.get(root)
            if building is None:
                building = threading.Event()
                _RECALL_RESOLVER_BUILDS[root] = building
                leader = True
        if leader:
            break
        if not building.wait(follower_wait):
            if not allow_fallback:
                # Declining beats both alternatives: blocking past the client's
                # deadline answers nobody, and building our own here would pay
                # the same whole-vault walk the leader is already paying.
                lexstore.request_repair(root)
                raise RetrievalIndexWarming(
                    site="resolver_build_wait",
                    status="temporarily_unavailable",
                    waited_ms=int((time.monotonic() - waited_started) * 1000),
                )
            break
        with _RESOLVER_LOCK:
            cached = _RECALL_RESOLVER_CACHE.get(root)
            if cached and cached[0] == identity:
                return cached[1].fork()
        # The leader finished with a different identity, or its result was
        # evicted before this thread looked. Try to become the leader.

    # The publication is INSIDE the try, so the leader's event is released only
    # once the cache actually holds the result. Signalling at the end of the
    # build instead would wake every follower onto a still-empty cache, and each
    # would then elect itself leader and rebuild -- reintroducing the stampede
    # this exists to prevent, just narrowed to a race window.
    try:
        entries = lexstore.get_store(root).recall_resolver_entries("vault", checkpoint)
        if entries is None:
            if not allow_fallback:
                lexstore.request_repair(root)
                raise RetrievalIndexWarming(
                    site="resolver_entries_unavailable",
                    status="temporarily_unavailable",
                )
            entries = []
            for path in walk_vault_md(root):
                if not recall_policy.is_recall_candidate(root, path):
                    continue
                page = _CACHE.get(path, root)
                if page is not None:
                    entries.append((page.rel_path, page.title))
        resolver = WikilinkResolver.from_entries(root, entries)
        with _RESOLVER_LOCK:
            # The supplied freshness key names this immutable resolver snapshot.
            # A later caller with changed disk/policy identity cannot reuse it;
            # graph rebuild performs its stronger direct before/after proof
            # around sidecar publication.
            _RECALL_RESOLVER_CACHE[root] = (identity, resolver)
            if checkpoint is not None:
                _RECALL_RESOLVER_CHECKPOINTS[root] = checkpoint
            else:
                _RECALL_RESOLVER_CHECKPOINTS.pop(root, None)
    finally:
        if leader:
            with _RECALL_REBUILD_LOCK:
                done = _RECALL_RESOLVER_BUILDS.pop(root, None)
            if done is not None:
                done.set()
    return resolver.fork()


def recall_resolver_snapshot_at_checkpoint(
    vault_root: Path,
    checkpoint: freshness.RecallFreshnessCheckpoint,
):
    """Borrow a read-only resolver only when exact event lineage is resident.

    Incremental graph maintenance must not rebuild a resolver from current disk
    and label it with an older live checkpoint: that loses the pre-delta
    topology needed to prove bounded edge repair. A cache miss is therefore an
    explicit signal to use the cold/full-rebuild path.

    The returned process-shared resolver must not be mutated. Authoritative
    publishers advance freshness before patching it, and graph publication
    revalidates that checkpoint after its pass. That ordering lets the common
    body-only path stay O(delta) without copying every resolver map. A caller
    that needs to mutate or reconstruct topology must fork explicitly.
    """
    root = Path(vault_root)
    identity = _recall_checkpoint_identity(checkpoint)
    with _RESOLVER_LOCK:
        cached = _RECALL_RESOLVER_CACHE.get(root)
        if (
            cached is None
            or cached[0] != identity
            or _RECALL_RESOLVER_CHECKPOINTS.get(root) != checkpoint
        ):
            return None
        return cached[1]


def prime_resolver_from_entries(
    vault_root: Path,
    entries,
    *,
    freshness_key: tuple[int, int, str] | None = None,
    expected_freshness: tuple[int, int, str] | None = None,
):
    """Install a current shared resolver from already-parsed corpus entries.

    Semantic preflight already owns exact path/title state. Reusing it avoids a
    full vault read/YAML parse when post-commit graph fanout is the first caller
    to need the resolver.
    """
    from . import freshness as freshness_module
    from .vault import WikilinkResolver

    root = Path(vault_root)
    current_freshness = (
        freshness_key if freshness_key is not None else FreshnessSnapshot(root).vault()
    )
    if expected_freshness is not None and current_freshness != expected_freshness:
        return None
    with _RESOLVER_LOCK:
        cached = _RESOLVER_CACHE.get(root)
        if cached and cached[0] == current_freshness:
            return cached[1]
        resolver = WikilinkResolver.from_entries(root, entries)
        _RESOLVER_CACHE[root] = (current_freshness, resolver)
        checkpoint = freshness_module.consumer_checkpoint(root, "vault")
        if checkpoint.triple == current_freshness:
            _RESOLVER_CHECKPOINTS[root] = checkpoint
        else:
            _RESOLVER_CHECKPOINTS.pop(root, None)
        return resolver


def writer_resolver_snapshot(
    vault_root: Path,
    *,
    freshness_key: tuple[int, int, str] | None = None,
):
    """Return a detached resolver snapshot without warming the shared cache.

    A fresh matching cached resolver is forked.  A cold/stale cache remains
    untouched and preparation gets a one-off resolver built from disk. Callers
    that already measured direct disk freshness may supply that key to bypass
    potentially stale event-registry state.
    """
    from .vault import WikilinkResolver

    root = Path(vault_root)
    current_freshness = (
        freshness_key if freshness_key is not None else FreshnessSnapshot(root).vault()
    )
    with _RESOLVER_LOCK:
        cached = _RESOLVER_CACHE.get(root)
        if cached and cached[0] == current_freshness:
            return cached[1].fork()
    return WikilinkResolver(root)


def _patch_broad_resolver(
    root: Path,
    cached: tuple[tuple, object],
) -> None:
    """Advance a writer resolver from its exact retained event suffix, or evict."""
    from . import vault as vault_module

    checkpoint = _RESOLVER_CHECKPOINTS.get(root)
    if checkpoint is None:
        with _RESOLVER_LOCK:
            if _RESOLVER_CACHE.get(root) == cached:
                _evict_resolver(root)
        return
    delta = freshness.delta_since(root, "vault", checkpoint)
    cached_identity, resolver = cached
    if not delta.complete or cached_identity != checkpoint.triple:
        with _RESOLVER_LOCK:
            if _RESOLVER_CACHE.get(root) == cached:
                _evict_resolver(root)
        return

    target_signatures = dict(delta.target_signatures)
    entries: list[tuple[str, str | None]] = []
    deleted_rels: list[str] = []
    guards: list[tuple[Path, freshness.FileSignature, vault_module.PathGuard]] = []
    root_absolute = root.absolute()
    try:
        for raw_path in delta.deleted:
            deleted_rels.append(Path(raw_path).absolute().relative_to(root_absolute).as_posix())
        for raw_path in delta.changed:
            path = Path(raw_path)
            rel = path.absolute().relative_to(root_absolute).as_posix()
            expected = target_signatures.get(str(path))
            if expected is None or freshness.stat_signature(path) != expected:
                raise vault_module.PathGuardError(
                    "PATH_GUARD_CHANGED", "writer resolver target changed"
                )
            text, guard = vault_module.read_guarded_text(root, path)
            if freshness.stat_signature(path) != expected:
                raise vault_module.PathGuardError(
                    "PATH_GUARD_CHANGED", "writer resolver target changed"
                )
            frontmatter, body, _ = vault_module.parse_frontmatter(text)
            entries.append((rel, vault_module.resolve_display_title(frontmatter, body, path)))
            guards.append((path, expected, guard))
    except (OSError, UnicodeDecodeError, ValueError, vault_module.PathGuardError):
        with _RESOLVER_LOCK:
            if _RESOLVER_CACHE.get(root) == cached:
                _evict_resolver(root)
        return

    try:
        for path, expected, guard in guards:
            if freshness.stat_signature(path) != expected:
                raise vault_module.PathGuardError(
                    "PATH_GUARD_CHANGED", "writer resolver target changed"
                )
            guard.recheck(root)
    except (OSError, vault_module.PathGuardError):
        with _RESOLVER_LOCK:
            if _RESOLVER_CACHE.get(root) == cached:
                _evict_resolver(root)
        return
    current = freshness.delta_since(root, "vault", checkpoint)
    if not current.complete or current.to != delta.to:
        with _RESOLVER_LOCK:
            if _RESOLVER_CACHE.get(root) == cached:
                _evict_resolver(root)
        return

    try:
        with _RESOLVER_LOCK:
            if _RESOLVER_CACHE.get(root) != cached or _RESOLVER_CHECKPOINTS.get(root) != checkpoint:
                return
            resolver.on_entries_changed(entries, deleted_rels)
            _RESOLVER_CACHE[root] = (delta.to.triple, resolver)
            _RESOLVER_CHECKPOINTS[root] = delta.to
    except Exception:  # noqa: BLE001 - a partial resolver must never publish.
        with _RESOLVER_LOCK:
            if _RESOLVER_CACHE.get(root) == cached:
                _evict_resolver(root)


def on_resolver_files_changed(
    vault_root: Path,
    changed_rels,
    deleted_rels,
) -> None:
    """Patch broad and ordinary-recall resolver caches for one event batch.

    This is the resolver's arm of the event-maintained index family (it sits
    beside `freshness.on_files_changed` and `vault.on_inbound_files_changed`,
    and the file watcher calls all three for the same batch). Mirrors the
    inbound index:

    - **Live-only.** A resolver cache that has not been built remains absent;
      its next caller builds from current disk state.
    - **Recall admission first.** The projected resolver receives only changed
      paths admitted by the Records/access policy. Suppressed paths are routed
      as deletes, so their body and title never enter its maps.
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
    root = Path(vault_root)
    with _RESOLVER_LOCK:
        broad_cached = _RESOLVER_CACHE.get(root)
        projected_cached = _RECALL_RESOLVER_CACHE.get(root)
        if broad_cached is None and projected_cached is None:
            return

    if broad_cached is not None:
        _patch_broad_resolver(root, broad_cached)

    if projected_cached is None:
        return
    from . import vault as vault_module

    cached_identity, projected = projected_cached
    checkpoint = _RECALL_RESOLVER_CHECKPOINTS.get(root)

    # A supplied event batch can be incomplete or arrive out of order. Advance
    # from the resolver's exact checkpoint instead of treating this callback's
    # path list as authoritative. No checkpoint means the cache was built from
    # a direct disk proof, so it cannot safely consume the live event suffix.
    if checkpoint is None or not freshness.recall_is_live(root, "vault"):
        with _RESOLVER_LOCK:
            if _RECALL_RESOLVER_CACHE.get(root) == projected_cached:
                _evict_recall_resolver(root)
        return
    delta = freshness.recall_delta_since(root, "vault", checkpoint)
    if (
        not delta.complete
        or cached_identity != _recall_checkpoint_identity(checkpoint)
        or delta.to.policy_version != checkpoint.policy_version
        or delta.to.access_policy_fingerprint != checkpoint.access_policy_fingerprint
    ):
        with _RESOLVER_LOCK:
            if _RECALL_RESOLVER_CACHE.get(root) == projected_cached:
                _evict_recall_resolver(root)
        return

    target_signatures = dict(delta.target_signatures)
    entries: list[tuple[str, str | None]] = []
    projected_deleted_rels: list[str] = []
    guards: list[tuple[Path, freshness.FileSignature, vault_module.PathGuard]] = []
    root_absolute = root.absolute()
    try:
        for raw_path in delta.deleted:
            projected_deleted_rels.append(
                Path(raw_path).absolute().relative_to(root_absolute).as_posix()
            )
        for raw_path in delta.changed:
            path = Path(raw_path)
            rel = path.absolute().relative_to(root_absolute).as_posix()
            expected = target_signatures.get(str(path))
            if (
                expected is None
                or not recall_policy.is_recall_candidate(root, path)
                or freshness.stat_signature(path) != expected
            ):
                raise vault_module.PathGuardError(
                    "PATH_GUARD_CHANGED", "projected resolver target changed"
                )
            text, guard = vault_module.read_guarded_text(root, path)
            if freshness.stat_signature(path) != expected:
                raise vault_module.PathGuardError(
                    "PATH_GUARD_CHANGED", "projected resolver target changed"
                )
            frontmatter, body, _ = vault_module.parse_frontmatter(text)
            entries.append((rel, vault_module.resolve_display_title(frontmatter, body, path)))
            guards.append((path, expected, guard))
    except (OSError, UnicodeDecodeError, ValueError, vault_module.PathGuardError):
        with _RESOLVER_LOCK:
            if _RECALL_RESOLVER_CACHE.get(root) == projected_cached:
                _evict_recall_resolver(root)
        return

    # Re-check every captured source and the event checkpoint immediately
    # before publication. A direct edit or a newly published event must evict,
    # never stamp newer bytes with this older target checkpoint.
    try:
        for path, expected, guard in guards:
            if (
                not recall_policy.is_recall_candidate(root, path)
                or freshness.stat_signature(path) != expected
            ):
                raise vault_module.PathGuardError(
                    "PATH_GUARD_CHANGED", "projected resolver target changed"
                )
            guard.recheck(root)
    except (OSError, vault_module.PathGuardError):
        with _RESOLVER_LOCK:
            if _RECALL_RESOLVER_CACHE.get(root) == projected_cached:
                _evict_recall_resolver(root)
        return
    current = freshness.recall_delta_since(root, "vault", checkpoint)
    if not current.complete or current.to != delta.to:
        with _RESOLVER_LOCK:
            if _RECALL_RESOLVER_CACHE.get(root) == projected_cached:
                _evict_recall_resolver(root)
        return

    try:
        with _RESOLVER_LOCK:
            if (
                _RECALL_RESOLVER_CACHE.get(root) != projected_cached
                or _RECALL_RESOLVER_CHECKPOINTS.get(root) != checkpoint
            ):
                return
            projected.on_entries_changed(entries, projected_deleted_rels)
            _RECALL_RESOLVER_CACHE[root] = (
                _recall_checkpoint_identity(delta.to),
                projected,
            )
            _RECALL_RESOLVER_CHECKPOINTS[root] = delta.to
    except Exception:  # noqa: BLE001 - a partial resolver must never publish.
        with _RESOLVER_LOCK:
            if _RECALL_RESOLVER_CACHE.get(root) == projected_cached:
                _evict_recall_resolver(root)


def unload_ram_caches(
    *, keep_recall_resolver: bool = False, pages: bool = False
) -> dict[str, int]:
    """Evict rebuildable find RAM caches without clearing freshness/inbound metadata.

    Two callers want different things from this. `epistemic_graph` uses it to
    force a re-derivation -- a correctness eviction, where keeping a stale
    resolver would be a wrong answer rather than a slow one. The idle reaper
    uses it to hand memory back, and should call `release_idle_ram_caches`
    instead. The default stays the correctness meaning.

    Either way the maps are cleared directly rather than through
    `_evict_recall_resolver`: that seam schedules a background rebuild, and a
    caller releasing memory does not want a thread immediately spending it
    again.

    `pages` says whether the parsed-page cache goes too, and it defaults to NOT
    going. That is the exact-custody rule: "a change in a whole-scope freshness
    key MUST NOT by itself discard a substrate cache whose paths are all
    covered by exact receipts." A correctness eviction's subject is the
    RESOLVER -- there, a stale projection is a wrong answer -- and the page
    cache cannot be stale in that sense: every entry is keyed to its file's
    content signature, and every governed write evicts its own rows through the
    custody seam. Discarding it alongside would throw away receipt-covered
    custody to fix a projection it has no part in, which on a busy cell is the
    cost `accelerate-governed-recall` exists to remove; `epistemic_graph`
    rebuilds are exactly the frequent whole-scope event that was paying it.

    A caller releasing MEMORY still wants it gone, and now says so:
    `release_idle_ram_caches` and the `clear_cache` test hook both pass
    `pages=True`.
    """
    page_entries = len(_CACHE.entries) if pages else 0
    if pages:
        # `release`, not `clear`: an explicit unload is the caller's policy
        # decision, and counting it as a custody rebuild would put a reaper's
        # noise into the number that says whether a governed write discarded
        # receipt-covered work.
        _CACHE.release()
    with _RESOLVER_LOCK:
        resolver_entries = len(_RESOLVER_CACHE)
        _RESOLVER_CACHE.clear()
        _RESOLVER_CHECKPOINTS.clear()
        if not keep_recall_resolver:
            resolver_entries += len(_RECALL_RESOLVER_CACHE)
            _RECALL_RESOLVER_CACHE.clear()
            _RECALL_RESOLVER_CHECKPOINTS.clear()
    with _FIND_CACHE_LOCK:
        hot_entries = len(_FIND_CACHE)
        _FIND_CACHE.clear()
        _FIND_CACHE_CHECKPOINTS.clear()
    with _RECALL_PATH_CACHE_LOCK:
        _RECALL_PATH_CACHE.clear()
    # Tiny (bounded at 32 paths), but this seam means "drop everything
    # rebuildable", and a memo left behind by an otherwise-complete eviction is
    # the kind of exception that later reads as an oversight.
    recall_policy.clear_resolved_roots()
    return {"pages": page_entries, "resolvers": resolver_entries, "hot_find": hot_entries}


def release_idle_ram_caches() -> dict[str, int]:
    """Hand memory back from an idle process, keeping what is dear to rebuild.

    The recall resolver stays. Measured on a 2,400-page vault it retains
    3.05 MiB -- 1,334 bytes a page -- while rebuilding it costs a vault walk,
    an admission pass, and a read of every admitted page: 39 s of page reads
    alone, charged to whichever reader asks first (#676). Releasing three
    megabytes from a process that is also holding a roughly one-gigabyte
    embedding model does not pay for that.

    Everything else still goes: the page cache is the large one, and the hot
    find cache and recall path cache are cheap to refill.
    """
    return unload_ram_caches(keep_recall_resolver=True, pages=True)


def evict_resolver_caches(vault_root: Path) -> int:
    """Withdraw both resolver projections for one vault after an event gap."""
    root = Path(vault_root)
    with _RESOLVER_LOCK:
        removed = int(root in _RESOLVER_CACHE) + int(root in _RECALL_RESOLVER_CACHE)
        _evict_resolver(root)
        _evict_recall_resolver(root)
    return removed


def reset_page_and_result_caches() -> dict[str, int]:
    """Narrow latency-harness reset: keep resolver/freshness/catalog state warm."""
    page_entries = len(_CACHE.entries)
    _CACHE.clear()
    with _FIND_CACHE_LOCK:
        hot_entries = len(_FIND_CACHE)
        _FIND_CACHE.clear()
        _FIND_CACHE_CHECKPOINTS.clear()
    return {"pages": page_entries, "hot_find": hot_entries}


def cache_status() -> dict:
    """No-allocation residency status for find's rebuildable RAM caches."""
    page_entries = list(_CACHE.entries.values())
    with _RESOLVER_LOCK:
        resolver_entries = len(_RESOLVER_CACHE) + len(_RECALL_RESOLVER_CACHE)
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
    unload_ram_caches(pages=True)
    freshness.clear()
    from . import vault as vault_module

    vault_module.clear_inbound_index()
