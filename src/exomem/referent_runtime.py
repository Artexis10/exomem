"""Read-only composition seam for referents in the find envelope."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import epistemic_graph, memory_refs
from .entity_registry import load_entity_registry
from .find import FreshnessSnapshot
from .governance import egress
from .referent_resolution import EdgeFact, HitFact, detect_cue, resolve_referents

_TRUE = frozenset({"1", "true", "yes", "on"})


def should_resolve_for_find(*, query: str, mode: str) -> bool:
    """Cheap trigger used before opening the optional timing span."""
    if mode not in {"hybrid", "vector"}:
        return False
    if os.environ.get("EXOMEM_DISABLE_REFERENTS", "").strip().casefold() in _TRUE:
        return False
    return detect_cue(query) is not None


def _hit_facts(hits: list[Any]) -> tuple[HitFact, ...]:
    return tuple(
        HitFact(
            path=str(getattr(hit, "path", "")),
            type=getattr(hit, "type", None),
            title=str(getattr(hit, "title", "")),
            status=str(getattr(hit, "status", None) or "active"),
            rank=index,
            bm25_rank=getattr(hit, "bm25_rank", None),
            vector_rank=getattr(hit, "vector_rank", None),
            keyword_rank=getattr(hit, "keyword_rank", None),
        )
        for index, hit in enumerate(hits, 1)
        if getattr(hit, "path", None)
    )


def _edge_facts(
    vault_root: Path,
    *,
    anchors: list[str],
    entity_paths: frozenset[str],
    graph: bool,
) -> tuple[EdgeFact, ...]:
    if not graph or not anchors:
        return ()
    index = epistemic_graph.EpistemicGraphIndex(vault_root)
    if not index.available():
        return ()
    facts: set[EdgeFact] = set()
    for neighbor in index.neighbors_for(anchors[:10]):
        if neighbor.other_rel in entity_paths:
            facts.add(
                EdgeFact(
                    seed_path=neighbor.seed_rel,
                    candidate_path=neighbor.other_rel,
                    relation_type=neighbor.relation_type,
                    direction=neighbor.direction,
                    family=neighbor.family,
                )
            )
        elif neighbor.seed_rel in entity_paths:
            facts.add(
                EdgeFact(
                    seed_path=neighbor.other_rel,
                    candidate_path=neighbor.seed_rel,
                    relation_type=neighbor.relation_type,
                    direction="inbound" if neighbor.direction == "outbound" else "outbound",
                    family=neighbor.family,
                )
            )
    return tuple(
        sorted(
            facts,
            key=lambda item: (
                item.candidate_path,
                item.seed_path,
                item.relation_type or "",
                item.direction,
            ),
        )
    )


def resolve_for_find(
    vault_root: Path,
    *,
    query: str,
    hits: list[Any],
    mode: str,
    graph: bool,
    release: Any,
    purpose: str | None,
) -> dict[str, Any] | None:
    """Resolve a bounded referent block; every exception soft-fails."""
    try:
        if not should_resolve_for_find(query=query, mode=mode):
            return None
        cue = detect_cue(query)
        if cue is None:
            return None
        freshness_key = FreshnessSnapshot(vault_root).projection_key("kb")
        registry = load_entity_registry(vault_root, freshness_key=freshness_key)
        hit_facts = _hit_facts(hits)
        edges = _edge_facts(
            vault_root,
            anchors=[item.path for item in hit_facts[:10]],
            entity_paths=frozenset(registry),
            graph=graph,
        )
        resolution = resolve_referents(
            cue=cue,
            hits=hit_facts,
            entities=tuple(registry.values()),
            edges=edges,
        )
        block = resolution.as_dict()
        if not block["resolved"] and not block["candidates"] and cue.expected_count is None:
            return None
        guarded = egress.guard_referents(
            vault_root,
            block,
            release,
            purpose=purpose,
        )
        if guarded is None:
            return None
        paths = [
            str(item.get("path") or "")
            for section in ("resolved", "candidates")
            for item in guarded.get(section, [])
            if isinstance(item, dict) and item.get("path")
        ]
        refs = memory_refs.ReferenceIndex(vault_root).refs_for_paths(paths)
        for section in ("resolved", "candidates"):
            for item in guarded.get(section, []):
                if ref := refs.get(str(item.get("path") or "")):
                    item["ref"] = ref
        return guarded
    except Exception:  # noqa: BLE001 - the additive stage is contractually fail-open
        return None
