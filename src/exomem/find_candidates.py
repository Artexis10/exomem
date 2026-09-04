"""Candidate lane acquisition and fusion for semantic find()."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import find_policy, find_results, find_types
from .find_types import FindTimings, GraphProvenance, ParsedPage
from .ranking_config import LANE_ORDER, RankingConfig

log = logging.getLogger(__name__)
_span = find_types.timing_span
_mark_source = find_types.timing_mark_source

PageOf = Callable[[str], ParsedPage | None]


@dataclass
class CandidateBundle:
    """All candidate-lane state needed to build semantic find hits."""

    fused: list[tuple[str, float]]
    had_rankings: bool
    vector_ranking: list[str]
    bm25_ranking: list[str]
    keyword_ranking: list[str]
    clip_ranking: list[str]
    graph_ranking: list[str]
    temporal_ranking: list[str]
    chunk_text_by_path: dict[str, str]
    bm25_score_by_path: dict[str, float]
    vector_score_by_path: dict[str, float]
    clip_score_by_path: dict[str, float]
    clip_frame_ts_by_path: dict[str, float | None]
    frame_attribution: dict[str, tuple[str, float | None]]
    graph_in_degree_by_path: dict[str, int]
    graph_provenance_by_path: dict[str, GraphProvenance]
    usage_map: dict[str, float]
    lane_rankings: dict[str, list[str]]
    lane_weights: dict[str, float]
    lane_statuses: dict[str, dict[str, Any]]
    rrf_k: int
    raw_fused_score_by_path: dict[str, float]
    adjusted_score_by_path: dict[str, float]
    multiplier_chain_by_path: dict[str, list[dict[str, float | str]]] | None


def empty_bundle(
    *,
    usage_map: dict[str, float] | None = None,
    lane_statuses: dict[str, dict[str, Any]] | None = None,
    rrf_k: int = 60,
) -> CandidateBundle:
    return CandidateBundle(
        fused=[],
        had_rankings=False,
        vector_ranking=[],
        bm25_ranking=[],
        keyword_ranking=[],
        clip_ranking=[],
        graph_ranking=[],
        temporal_ranking=[],
        chunk_text_by_path={},
        bm25_score_by_path={},
        vector_score_by_path={},
        clip_score_by_path={},
        clip_frame_ts_by_path={},
        frame_attribution={},
        graph_in_degree_by_path={},
        graph_provenance_by_path={},
        usage_map=usage_map or {},
        lane_rankings={name: [] for name in LANE_ORDER},
        lane_weights={name: 1.0 for name in LANE_ORDER},
        lane_statuses=lane_statuses or {},
        rrf_k=rrf_k,
        raw_fused_score_by_path={},
        adjusted_score_by_path={},
        multiplier_chain_by_path=None,
    )


def collapse_frame_children(
    ranking: list[str],
    vault_root: Path,
    page_of: PageOf,
    attribution: dict[str, tuple[str, float | None]],
    *aux_maps: dict,
    parent_hints: Mapping[str, str | None] | None = None,
    recall_paths: AbstractSet[str] | None = None,
) -> list[str]:
    """Remap scene-frame sidecar candidates onto their parent video sidecar."""
    if not ranking:
        return ranking
    out: list[str] = []
    seen: set[str] = set()
    from . import recall_policy

    def admitted(rel: str) -> bool:
        return (
            rel in recall_paths
            if recall_paths is not None
            else recall_policy.is_recall_candidate(vault_root, vault_root / rel)
        )

    for rel in ranking:
        if not admitted(rel):
            continue
        # A ready catalog's explicit NULL proves this is an ordinary page, so
        # avoid its otherwise pointless Markdown hydration. A non-NULL hint is
        # only a hydration selector: the parsed child remains authoritative if
        # it was deleted, became malformed, or changed parent_media after the
        # catalog snapshot.
        page = (
            None
            if parent_hints is not None and parent_hints.get(rel) is None and rel in parent_hints
            else page_of(rel)
        )
        parent = page.parent_media if page is not None else None
        if parent:
            parent_sidecar = parent + ".md"
            if (
                admitted(parent_sidecar)
                and recall_policy.is_recall_candidate(vault_root, vault_root / parent_sidecar)
                and (vault_root / parent_sidecar).exists()
            ):
                attribution.setdefault(parent_sidecar, (page.media_file or rel, page.frame_ts))
                for m in aux_maps:
                    if rel in m:
                        v = m.pop(rel)
                        m.setdefault(parent_sidecar, v)
                rel = parent_sidecar
        if rel in seen:
            continue
        seen.add(rel)
        out.append(rel)
    return out


def collect_candidates(
    vault_root: Path,
    *,
    query: str,
    query_norm: str,
    limit: int,
    scope: str,
    mode: str,
    graph: bool,
    temporal: bool,
    intent: str | None,
    prefer_compiled: bool,
    prefer_active: bool,
    prefer_used: bool,
    config: RankingConfig,
    timings: FindTimings | None,
    snapshot: Any,
    page_of: PageOf,
    keyword_match_paths: Callable[..., list[str]],
    outbound_wikilink_paths: Callable[..., list[str]],
    get_query_resolver: Callable[..., object],
    record_degradation: Callable[[str], None],
    degraded_out: list[str] | None,
    failed_out: list[str] | None,
    recall_paths: AbstractSet[str],
    lexical_repair: bool = True,
    eligible_paths: set[str] | None = None,
    capture_trace: bool = False,
    query_vector_provider: Callable[[], Any] | None = None,
    shadow: Callable[[list[str]], list[str]] | None = None,
) -> CandidateBundle:
    """Collect vector/BM25/keyword/CLIP/graph/temporal lanes and fuse them.

    `shadow` optionally removes identities no lane may attest -- the caller's
    exact pending-visibility overlay owns those generations -- and is applied to
    every lane this collector builds itself, at its source, before rank lookups,
    frame collapsing, eligibility, graph seeding, fusion and every lane cap
    consume it. The keyword lane is excluded because it arrives from the
    caller's own provider already shadowed. Default None is a strict no-op.
    """
    from . import bm25, embeddings, epistemic_graph, fusion, lexstore, readiness, runtime_resources

    usage_map: dict[str, float] = {}
    if prefer_used:
        from . import usage as usage_module
        usage_map = usage_module.activation_map(config)

    candidate_k = max(
        limit * config.candidate_multiplier,
        config.candidate_floor,
        len(eligible_paths) if eligible_paths is not None else 0,
    )
    semantic_paths = (
        recall_paths if eligible_paths is None else (recall_paths & eligible_paths)
    )

    def _eligible(ranking: list[str]) -> list[str]:
        return [
            path
            for path in ranking
            if path in recall_paths and (eligible_paths is None or path in eligible_paths)
        ]
    frame_attribution: dict[str, tuple[str, float | None]] = {}
    lane_statuses: dict[str, dict[str, Any]] = {}

    vector_ranking: list[str] = []
    chunk_text_by_path: dict[str, str] = {}
    vector_score_by_path: dict[str, float] = {}
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        if capture_trace:
            lane_statuses["vector"] = {
                "status": "disabled",
                "reason": "embeddings_disabled",
                "model": embeddings.MODEL_NAME,
            }
        if timings is not None:
            timings.skipped("vector")
    elif readiness.should_defer("embeddings"):
        if capture_trace:
            lane_statuses["vector"] = {
                "status": "warming",
                "reason": "model_warming",
                "model": embeddings.MODEL_NAME,
            }
        if timings is not None:
            timings.skipped("vector")
        if degraded_out is not None:
            degraded_out.append("embeddings")
    else:
        try:
            with _span(timings, "vector"):
                with _span(timings, "vector.index", source=find_types.SOURCE_INDEX):
                    idx = embeddings.get_embedding_index(vault_root)
                with _span(timings, "vector.embed"):
                    query_vec = (
                        query_vector_provider()
                        if query_vector_provider is not None
                        else embeddings.embed_texts([query], is_query=True)[0]
                    )
                with _span(timings, "vector.search"):
                    chunk_hits = idx.search(
                        query_vec,
                        k=candidate_k * 3,
                        allowed_paths=semantic_paths,
                    )
                best_per_file: dict[str, tuple[float, str]] = {}
                for fp, _idx, ctext, score in chunk_hits:
                    existing = best_per_file.get(fp)
                    if existing is None or score > existing[0]:
                        best_per_file[fp] = (score, ctext)
                vector_ranking = sorted(
                    best_per_file.keys(), key=lambda p: -best_per_file[p][0]
                )[:candidate_k]
                chunk_text_by_path = {p: best_per_file[p][1] for p in vector_ranking}
                vector_score_by_path = {p: best_per_file[p][0] for p in vector_ranking}
                if capture_trace:
                    lane_statuses["vector"] = {
                        "status": "participated" if vector_ranking else "available_nonmatching",
                        "backend": type(idx).__name__,
                        "model": embeddings.MODEL_NAME,
                        "metric": {
                            "name": "cosine_similarity",
                            "direction": "higher",
                            "range": [-1.0, 1.0],
                            "rounding": 6,
                        },
                    }
        except ImportError as e:
            if capture_trace:
                lane_statuses["vector"] = {
                    "status": "unavailable",
                    "reason": "dependency_unavailable",
                    "model": embeddings.MODEL_NAME,
                }
            log.info("vector search unavailable (%s); keyword/BM25-only ranking", e)
            if timings is not None:
                timings.error("vector", e)
        except runtime_resources.ModelBusyError:
            raise
        except Exception as e:  # noqa: BLE001 - vector search is best-effort
            if capture_trace:
                lane_statuses["vector"] = {
                    "status": "failed",
                    "reason": "search_failed",
                    "model": embeddings.MODEL_NAME,
                }
            log.warning("vector search failed: %s; falling back to BM25-only", e)
            record_degradation("vector")
            if failed_out is not None:
                failed_out.append("vector")
            if timings is not None:
                timings.error("vector", e)
    clip_ranking: list[str] = []
    clip_score_by_path: dict[str, float] = {}
    clip_frame_ts_by_path: dict[str, float | None] = {}
    if embeddings.clip_enabled() and query.strip() and readiness.should_defer("clip"):
        if capture_trace:
            lane_statuses["clip"] = {
                "status": "warming",
                "reason": "model_warming",
                "model": embeddings.CLIP_MODEL_NAME,
            }
        if timings is not None:
            timings.skipped("clip")
        if degraded_out is not None:
            degraded_out.append("clip")
    elif embeddings.clip_enabled() and query.strip():
        try:
            with _span(timings, "clip"):
                clip_idx = embeddings.get_clip_index(vault_root)
                clip_qvec = embeddings.embed_clip_text(query)
                allowed_images = {
                    path.removesuffix(".md")
                    for path in semantic_paths
                    if path.endswith(".md")
                }
                clip_hits = clip_idx.search(
                    clip_qvec,
                    k=candidate_k * 8,
                    allowed_paths=allowed_images,
                )
                for img_rel, frame_ts, score in clip_hits:
                    if len(clip_ranking) >= candidate_k:
                        break
                    sidecar_rel = img_rel + ".md"
                    if sidecar_rel not in clip_score_by_path and (vault_root / sidecar_rel).exists():
                        clip_ranking.append(sidecar_rel)
                        clip_score_by_path[sidecar_rel] = score
                        clip_frame_ts_by_path[sidecar_rel] = frame_ts
                if capture_trace:
                    lane_statuses["clip"] = {
                        "status": "participated" if clip_ranking else "available_nonmatching",
                        "backend": type(clip_idx).__name__,
                        "model": embeddings.CLIP_MODEL_NAME,
                        "metric": {
                            "name": "cosine_similarity",
                            "direction": "higher",
                            "range": [-1.0, 1.0],
                            "rounding": 6,
                        },
                    }
        except embeddings.ClipUnavailable as e:
            if capture_trace:
                lane_statuses["clip"] = {
                    "status": "unavailable",
                    "reason": "dependency_unavailable",
                    "model": embeddings.CLIP_MODEL_NAME,
                }
            log.warning("CLIP search unavailable (%s); skipping image search", e)
            record_degradation("clip")
            if failed_out is not None:
                failed_out.append("clip")
            if timings is not None:
                timings.error("clip", e)
        except runtime_resources.ModelBusyError:
            raise
        except Exception as e:  # noqa: BLE001 - image search is best-effort
            if capture_trace:
                lane_statuses["clip"] = {
                    "status": "failed",
                    "reason": "search_failed",
                    "model": embeddings.CLIP_MODEL_NAME,
                }
            log.warning("CLIP search failed: %s; skipping image search", e)
            record_degradation("clip")
            if failed_out is not None:
                failed_out.append("clip")
            if timings is not None:
                timings.error("clip", e)
    elif timings is not None:
        timings.skipped("clip")
    if capture_trace and "clip" not in lane_statuses:
        lane_statuses["clip"] = {
            "status": "disabled",
            "reason": "clip_disabled",
            "model": embeddings.CLIP_MODEL_NAME,
        }
    bm25_ranking: list[str] = []
    bm25_score_by_path: dict[str, float] = {}
    keyword_ranking: list[str] = []
    if mode == "vector":
        if capture_trace:
            lane_statuses["bm25"] = {
                "status": "non_applicable",
                "reason": "requested_mode_vector",
            }
            lane_statuses["keyword"] = {
                "status": "non_applicable",
                "reason": "requested_mode_vector",
            }
        if timings is not None:
            timings.skipped("bm25")
            timings.skipped("keyword")
        rankings = [r for r in (vector_ranking, clip_ranking) if r]
    else:
        try:
            with _span(timings, "bm25"):
                if not lexical_repair and lexstore.maintained_content_index_enabled():
                    catalog_result = lexstore.search_bm25_result(
                        vault_root,
                        query,
                        k=candidate_k,
                        scope=scope,
                        freshness=snapshot.for_scope(scope),
                        allowed_paths=eligible_paths,
                    )
                    if not catalog_result.readiness.complete:
                        _mark_source(timings, "bm25", find_types.SOURCE_DECLINED)
                        raise lexstore.CatalogUnavailable(catalog_result.readiness)
                    _mark_source(timings, "bm25", find_types.SOURCE_INDEX)
                    bm25_hits = list(catalog_result.value or [])
                else:
                    # The in-process corpus path: it builds from the scope when
                    # cold, so it is `computed`, never `index`.
                    _mark_source(timings, "bm25", find_types.SOURCE_COMPUTED)
                    bm25_hits = bm25.search(
                        vault_root,
                        query,
                        k=candidate_k,
                        scope=scope,
                        freshness=snapshot.for_scope(scope),
                        allowed_paths=eligible_paths,
                        repair=lexical_repair,
                    )
                bm25_ranking = [p for p, _ in bm25_hits]
                bm25_score_by_path = {p: float(score) for p, score in bm25_hits}
                if capture_trace:
                    lane_statuses["bm25"] = {
                        "status": "participated" if bm25_ranking else "available_nonmatching",
                        "backend": lexstore.cache_token(vault_root),
                        "metric": {
                            "name": "raw_bm25_score",
                            "direction": "higher",
                            "range": "backend_dependent",
                            "rounding": 6,
                            "caveat": "diagnostic; not comparable across backends or corpora",
                        },
                    }
        except lexstore.CatalogUnavailable:
            raise
        except ImportError as e:
            if capture_trace:
                lane_statuses["bm25"] = {
                    "status": "unavailable",
                    "reason": "dependency_unavailable",
                }
            log.warning("BM25 unavailable (%s); using vector-only", e)
            if timings is not None:
                timings.error("bm25", e)
        except Exception as e:  # noqa: BLE001 - BM25 search is best-effort
            if capture_trace:
                lane_statuses["bm25"] = {
                    "status": "failed",
                    "reason": "search_failed",
                }
            log.warning("BM25 search failed: %s; using vector-only", e)
            if timings is not None:
                timings.error("bm25", e)
        with _span(timings, "keyword"):
            # Hydration is one of the four stages the contract names, so this
            # lane reports its source instead of carrying the static `computed`
            # default. `index` is the basis, not a courtesy: this lane's only
            # source of paths is the injected `keyword_match_paths` provider,
            # which resolves from the maintained lexical index and never
            # enumerates the scope. The branch that CAN enumerate is the
            # empty-query one in `find._find_keyword`, which marks itself
            # `computed` when it takes the walk.
            _mark_source(timings, "keyword", find_types.SOURCE_INDEX)
            # Over-fetch by the same factor the BM25 lane uses, and for the same
            # reason: `collapse_frame_children` and `_eligible` below can drop
            # members, so a lane that supplied exactly the fused depth could
            # arrive under it. Before this the lane supplied *everything* --
            # every matching page in the vault, on every call (#516).
            keyword_ranking = keyword_match_paths(
                vault_root,
                query_norm,
                scope,
                freshness=snapshot.for_scope(scope),
                repair=lexical_repair,
                k=candidate_k * 3,
            )
    if shadow is not None:
        # Before anything downstream reads a lane: a shadowed identity must not
        # seed the graph lane, reach the parent-hint seam, occupy a bounded lane
        # window, or take a fused position derived from its stale rank.
        #
        # Deliberately not `keyword_ranking`: that lane comes from the caller's
        # own `keyword_match_paths` provider, which has already suppressed the
        # identities the caller shadows AND merged back the generations only it
        # can attest. Shadowing it again here would delete that attestation.
        vector_ranking = shadow(vector_ranking)
        clip_ranking = shadow(clip_ranking)
        bm25_ranking = shadow(bm25_ranking)
    raw_rankings = (vector_ranking, clip_ranking, bm25_ranking, keyword_ranking)
    admitted_raw_paths = set().union(*raw_rankings) & recall_paths
    parent_hints: Mapping[str, str | None] | None = None
    if admitted_raw_paths:
        recall_checkpoint = snapshot.recall_checkpoint(scope)
        hint_result = lexstore.emitted_parent_hints_result(
            vault_root,
            admitted_raw_paths,
            scope=scope,
            freshness=snapshot.for_scope(scope),
            recall_checkpoint=recall_checkpoint,
        )
        if hint_result.readiness.complete:
            parent_hints = hint_result.value

    vector_ranking = collapse_frame_children(
        vector_ranking,
        vault_root,
        page_of,
        frame_attribution,
        chunk_text_by_path,
        vector_score_by_path,
        parent_hints=parent_hints,
        recall_paths=recall_paths,
    )
    vector_ranking = _eligible(vector_ranking)
    clip_ranking = collapse_frame_children(
        clip_ranking,
        vault_root,
        page_of,
        frame_attribution,
        clip_score_by_path,
        clip_frame_ts_by_path,
        parent_hints=parent_hints,
        recall_paths=recall_paths,
    )
    clip_ranking = _eligible(clip_ranking)
    bm25_ranking = collapse_frame_children(
        bm25_ranking,
        vault_root,
        page_of,
        frame_attribution,
        bm25_score_by_path,
        parent_hints=parent_hints,
        recall_paths=recall_paths,
    )
    bm25_ranking = _eligible(bm25_ranking)
    keyword_ranking = collapse_frame_children(
        keyword_ranking,
        vault_root,
        page_of,
        frame_attribution,
        parent_hints=parent_hints,
        recall_paths=recall_paths,
    )
    keyword_ranking = _eligible(keyword_ranking)
    if capture_trace and mode != "vector":
        lane_statuses["keyword"] = {
            "status": "participated" if keyword_ranking else "available_nonmatching",
            "backend": "case_insensitive_substring",
            "metric": {"name": "rank", "direction": "lower", "rounding": "none"},
        }
    rankings = [
        r
        for r in (vector_ranking, bm25_ranking, keyword_ranking, clip_ranking)
        if r
    ]

    graph_ranking: list[str] = []
    graph_in_degree_by_path: dict[str, int] = {}
    graph_provenance_by_path: dict[str, GraphProvenance] = {}
    if not graph and timings is not None:
        timings.skipped("graph")
    if not graph:
        if capture_trace:
            lane_statuses["graph"] = {
                "status": "non_applicable",
                "reason": "request_disabled",
            }
    if graph:
        with _span(timings, "graph"):
            primary_set: set[str] = set(vector_ranking) | set(bm25_ranking)
            vector_set: set[str] = set(vector_ranking)
            graph_seeds: list[str] = []
            seen_seed: set[str] = set()
            with _span(timings, "graph.seeds"):
                for r in (vector_ranking, bm25_ranking):
                    for p in r[:config.graph_seed_cap]:
                        if p in seen_seed:
                            continue
                        seen_seed.add(p)
                        if p in vector_set:
                            graph_seeds.append(p)
                            continue
                        page = page_of(p)
                        if page is None:
                            continue
                        if (
                            find_results.make_excerpt(page, query_norm) is not None
                            or find_results.stem_tokens_present(page, query_norm)
                        ):
                            graph_seeds.append(p)
            graph_index = epistemic_graph.EpistemicGraphIndex(vault_root)
            if graph_index.available():
                # Hybrid: seeds with a sidecar file node get typed expansion; seeds
                # outside the indexed scope (e.g. an out-of-KB page under
                # scope="vault" — rebuild_all only walks the KB tree) have no node
                # at all, so typed expansion alone would silently drop them. Those
                # seeds fall back to the legacy 1-hop wikilink expansion instead,
                # preserving pre-change recall for out-of-KB seeds.
                with _span(timings, "graph.sidecar", source=find_types.SOURCE_INDEX):
                    indexed = graph_index.indexed_paths(graph_seeds)
                    typed_seeds = [s for s in graph_seeds if s in indexed]
                    legacy_seeds = [s for s in graph_seeds if s not in indexed]
                    neighbors = graph_index.neighbors_for(typed_seeds) if typed_seeds else []

                # Family precedence MUST be decided BEFORE target dedup: when a
                # target is reached by both a typed relation and a plain
                # links_to/unregistered edge, first-seen-wins would let arbitrary
                # edge order misclassify the target's tier and provenance. Group
                # every edge touching a target, then keep the highest-precedence
                # (lowest tier) edge as the surfacing/provenance edge. in-degree is
                # still tallied for EVERY edge, matching the existing invariant.
                with _span(timings, "graph.expand"):
                    best_tier_for_target: dict[str, int] = {}
                    best_neighbor_for_target: dict[str, epistemic_graph.GraphNeighbor] = {}
                    first_pos_for_target: dict[str, int] = {}
                    for pos, neighbor in enumerate(neighbors):
                        target_rel = neighbor.other_rel
                        if target_rel not in recall_paths:
                            continue
                        graph_in_degree_by_path[target_rel] = (
                            graph_in_degree_by_path.get(target_rel, 0) + 1
                        )
                        if target_rel in primary_set:
                            continue
                        if eligible_paths is not None and target_rel not in eligible_paths:
                            continue
                        family = neighbor.family
                        tier = 0 if (neighbor.relation_type and family and family != "link") else 1
                        current_best = best_tier_for_target.get(target_rel)
                        if current_best is None or tier < current_best:
                            best_tier_for_target[target_rel] = tier
                            best_neighbor_for_target[target_rel] = neighbor
                            first_pos_for_target[target_rel] = pos
                    typed_targets = sorted(
                        best_tier_for_target,
                        key=lambda t: (best_tier_for_target[t], first_pos_for_target[t]),
                    )
                    for target_rel in typed_targets:
                        neighbor = best_neighbor_for_target[target_rel]
                        graph_provenance_by_path[target_rel] = GraphProvenance(
                            relation_type=neighbor.relation_type,
                            direction=neighbor.direction,
                            seed=neighbor.seed_rel,
                        )
                    seen_target = set(typed_targets)

                    legacy_targets: list[str] = []
                    if legacy_seeds:
                        resolver = get_query_resolver(
                            vault_root, freshness=snapshot.projection_key("vault")
                        )
                        for seed_rel in legacy_seeds:
                            page = page_of(seed_rel)
                            if page is None:
                                continue
                            for target_rel in outbound_wikilink_paths(
                                page,
                                vault_root,
                                resolver=resolver,
                                allowed_paths=recall_paths,
                            ):
                                if target_rel not in recall_paths:
                                    continue
                                graph_in_degree_by_path[target_rel] = (
                                    graph_in_degree_by_path.get(target_rel, 0) + 1
                                )
                                if target_rel in primary_set or target_rel in seen_target:
                                    continue
                                if eligible_paths is not None and target_rel not in eligible_paths:
                                    continue
                                seen_target.add(target_rel)
                                legacy_targets.append(target_rel)

                    graph_ranking = typed_targets + legacy_targets
                if capture_trace:
                    lane_statuses["graph"] = {
                        "status": "participated" if graph_ranking else "available_nonmatching",
                        "backend": "epistemic_graph",
                        "metric": {"name": "rank", "direction": "lower", "rounding": "none"},
                    }
                if graph_ranking:
                    rankings.append(graph_ranking)
            else:
                # Fallback: the pre-existing 1-hop outbound-wikilink expansion,
                # byte-identical to the pre-change ordering. Do not refactor.
                # `graph.expand` is reported by BOTH branches, and the manual
                # timers this replaced split the fallback at exactly this point:
                # `graph.resolver` was resolver construction only, and everything
                # after it was `graph.expand`. Wrapping the whole branch in one
                # `graph.resolver` span both drops a stage other code reads and
                # bills the expansion loop as resolver time.
                with _span(timings, "graph.resolver"):
                    resolver = (
                        get_query_resolver(
                            vault_root, freshness=snapshot.projection_key("vault")
                        )
                        if graph_seeds else None
                    )
                with _span(timings, "graph.expand"):
                    seen_target = set()
                    for seed_rel in graph_seeds:
                        page = page_of(seed_rel)
                        if page is None:
                            continue
                        for target_rel in outbound_wikilink_paths(
                            page,
                            vault_root,
                            resolver=resolver,
                            allowed_paths=recall_paths,
                        ):
                            if target_rel not in recall_paths:
                                continue
                            graph_in_degree_by_path[target_rel] = (
                                graph_in_degree_by_path.get(target_rel, 0) + 1
                            )
                            if target_rel in primary_set or target_rel in seen_target:
                                continue
                            if eligible_paths is not None and target_rel not in eligible_paths:
                                continue
                            seen_target.add(target_rel)
                            graph_ranking.append(target_rel)
                if graph_ranking:
                    rankings.append(graph_ranking)
                if capture_trace:
                    lane_statuses["graph"] = {
                        "status": "participated" if graph_ranking else "available_nonmatching",
                        "backend": "wikilink_fallback",
                        "metric": {"name": "rank", "direction": "lower", "rounding": "none"},
                    }

    if not rankings:
        return empty_bundle(
            usage_map=usage_map,
            lane_statuses=lane_statuses,
            rrf_k=config.rrf_k,
        )

    temporal_ranking: list[str] = []
    if temporal and find_policy.is_temporal_query(query):
        with _span(timings, "temporal"):
            pool: list[str] = []
            for lane in (vector_ranking, bm25_ranking, keyword_ranking, clip_ranking):
                pool.extend(lane)
            temporal_ranking = find_policy.recency_ranking(pool, page_of, candidate_k)
        if capture_trace:
            lane_statuses["temporal"] = {
                "status": "participated" if temporal_ranking else "available_nonmatching",
                "backend": "updated_frontmatter",
                "metric": {"name": "rank", "direction": "lower", "rounding": "none"},
            }
    elif timings is not None:
        timings.skipped("temporal")
    if capture_trace and "temporal" not in lane_statuses:
        lane_statuses["temporal"] = {
            "status": "non_applicable",
            "reason": "query_not_temporal" if temporal else "request_disabled",
        }

    with _span(timings, "fusion"):
        intent_label = intent or find_policy.classify_intent(query)
        weights = config.intent_weights(intent_label)
        if shadow is not None:
            graph_ranking = shadow(graph_ranking)
            temporal_ranking = shadow(temporal_ranking)
        lane_rankings = [
            vector_ranking,
            bm25_ranking,
            keyword_ranking,
            clip_ranking,
            graph_ranking,
            temporal_ranking,
        ]
        active_lists: list[list[str]] = []
        active_weights: list[float] = []
        for lane, w in zip(lane_rankings, weights, strict=True):
            if lane:
                active_lists.append(lane)
                active_weights.append(w)
        fused = fusion.reciprocal_rank_fusion_weighted(
            active_lists, active_weights, k=config.rrf_k
        )
        raw_fused_score_by_path = dict(fused) if capture_trace else {}
        multiplier_chain_by_path: (
            dict[str, list[dict[str, float | str]]] | None
        ) = ({} if capture_trace else None)
        fused = find_policy.apply_post_rrf_multipliers(
            fused,
            query,
            config,
            prefer_compiled=prefer_compiled,
            prefer_active=prefer_active,
            temporal=temporal,
            page_of=page_of,
            usage_map=usage_map,
            evidence_out=multiplier_chain_by_path,
            # Bound the pass at the depth this system already says it needs.
            # `candidate_k` is at least `limit * 5` and at least the 50-entry
            # floor, while the consumer in `_find_semantic` stops building hits
            # at `limit * 3` — so the exact prefix always covers what is read,
            # with headroom for the candidates that loop skips. Without this the
            # pass loads every fused page from disk purely to rank them (#283).
            top_n=candidate_k,
        )
        adjusted_score_by_path = dict(fused) if capture_trace else {}

    trace_lane_rankings = (
        dict(zip(LANE_ORDER, lane_rankings, strict=True)) if capture_trace else {}
    )
    lane_weights = (
        dict(zip(LANE_ORDER, weights, strict=True)) if capture_trace else {}
    )

    return CandidateBundle(
        fused=fused,
        had_rankings=True,
        vector_ranking=vector_ranking,
        bm25_ranking=bm25_ranking,
        keyword_ranking=keyword_ranking,
        clip_ranking=clip_ranking,
        graph_ranking=graph_ranking,
        temporal_ranking=temporal_ranking,
        chunk_text_by_path=chunk_text_by_path,
        bm25_score_by_path=bm25_score_by_path if capture_trace else {},
        vector_score_by_path=vector_score_by_path,
        clip_score_by_path=clip_score_by_path,
        clip_frame_ts_by_path=clip_frame_ts_by_path,
        frame_attribution=frame_attribution,
        graph_in_degree_by_path=graph_in_degree_by_path,
        graph_provenance_by_path=graph_provenance_by_path,
        usage_map=usage_map,
        lane_rankings=trace_lane_rankings,
        lane_weights=lane_weights,
        lane_statuses=lane_statuses,
        rrf_k=config.rrf_k,
        raw_fused_score_by_path=raw_fused_score_by_path,
        adjusted_score_by_path=adjusted_score_by_path,
        multiplier_chain_by_path=multiplier_chain_by_path,
    )
