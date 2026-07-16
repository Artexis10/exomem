"""Bounded reasoning context assembled from canonical Markdown and derived indexes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import context_pack, epistemic_graph, get_page, memory_refs, vault
from . import find as find_module
from .find_types import Hit, ParsedPage


def assemble_context(
    vault_root: Path,
    *,
    path: str | None = None,
    query: str | None = None,
    unit_ref: str | None = None,
    categories: list[str] | None = None,
    kinds: list[str] | None = None,
    depth: int = 1,
    relation_types: list[str] | None = None,
    node_types: list[str] | None = None,
    max_nodes: int = 40,
    max_edges: int = 80,
    traversal_profile: str | None = None,
    limit: int = 5,
    max_body_chars: int = 3000,
) -> dict[str, Any]:
    """Return a single bounded context envelope for one page or a query."""
    unit_controls = unit_ref is not None or bool(categories) or bool(kinds)
    if not path and not query and not unit_controls:
        raise ValueError(
            "INVALID_CONTEXT: provide `path`, `query`, `unit_ref`, `categories`, or `kinds`"
        )
    depth = max(0, min(int(depth), 5))
    max_nodes = max(1, min(int(max_nodes), 200))
    max_edges = max(0, min(int(max_edges), 400))
    limit = max(1, min(int(limit), 10))
    max_body_chars = max(500, min(int(max_body_chars), 6000))
    graph: dict[str, Any] | None = None
    if unit_controls:
        if path:
            try:
                path = get_page.get_page(vault_root, path=path).path
            except get_page.GetError as exc:
                raise ValueError(f"{exc.code}: {exc.reason}") from exc
        if unit_ref is not None and path:
            _validate_unit_parent_path(vault_root, unit_ref=unit_ref, path=path)
        graph = epistemic_graph.graph_context(
            vault_root,
            path=path,
            query=query,
            unit_ref=unit_ref,
            categories=categories,
            kinds=kinds,
            depth=depth,
            relation_types=relation_types,
            node_types=node_types,
            max_nodes=max_nodes,
            max_edges=max_edges,
            traversal_profile=traversal_profile,
        )
        hits = _hits_for_graph_seeds(vault_root, graph, limit=limit)
    elif path or query:
        hits = _context_hits(vault_root, path=path, query=query, limit=limit)
    else:
        hits = []
    pages = [
        page
        for hit in hits
        if (page := find_module._CACHE.get(vault_root / hit.path, vault_root)) is not None
    ]
    pack = context_pack.assemble_pack(
        vault_root,
        hits,
        max_hits=limit,
        max_neighbors=max_nodes,
        max_tension=min(max_edges, 20),
    )
    ref_index = memory_refs.ReferenceIndex(vault_root)
    documents: list[dict[str, Any]] = []
    provenance: list[dict[str, Any]] = []
    supersession: list[dict[str, Any]] = []
    history: dict[str, list[dict[str, str]]] = {}
    truncation = list(pack.get("truncation", []))
    for page in pages:
        body, truncated = _bounded(page.body, max_body_chars)
        document: dict[str, Any] = {
            "path": page.rel_path,
            "title": page.title,
            "type": page.page_type,
            "status": page.status,
            "body": body,
        }
        if ref := ref_index.ref_for_path(page.rel_path):
            document["ref"] = ref
        if truncated:
            document["body_truncated"] = True
            truncation.append(f"body capped at {max_body_chars} characters: {page.rel_path}")
        documents.append(document)
        sources = _link_values(page.frontmatter.get("sources"))
        evidence = []
        for field in ("evidence", "evidences", "evidence_paths", "evidence_file"):
            evidence.extend(_link_values(page.frontmatter.get(field)))
        provenance.append(
            {"path": page.rel_path, "sources": sources, "evidence": _dedupe(evidence)}
        )
        supersession.append(
            {
                "path": page.rel_path,
                "status": page.status,
                "supersedes": _link_values(page.frontmatter.get("supersedes")),
                "superseded_by": _link_values(page.frontmatter.get("superseded_by")),
            }
        )
        page_history = vault.read_log_entries(vault_root, page.rel_path)[:10]
        if page_history:
            history[page.rel_path] = page_history
    if graph is None:
        graph = _merge_graph_contexts(
            vault_root,
            pages,
            depth=depth,
            relation_types=relation_types,
            node_types=node_types,
            max_nodes=max_nodes,
            max_edges=max_edges,
            traversal_profile=traversal_profile,
        )
    truncation.extend(str(item) for item in graph.get("truncation", []))
    resolved_seed_path = hits[0].path if path and hits else path
    seed: dict[str, Any] = {"path": resolved_seed_path, "query": query}
    if path:
        seed["ref"] = ref_index.ref_for_path(str(resolved_seed_path))
    if unit_ref is not None:
        seed["unit_ref"] = unit_ref
    if categories:
        seed["categories"] = list(categories)
    if kinds:
        seed["kinds"] = list(kinds)
    return {
        "available": bool(documents),
        "seed": seed,
        "documents": documents,
        "claims": pack.get("claims", {}),
        "semantic_blocks": pack.get("semantic_blocks", {}),
        "graph": graph,
        "provenance": provenance,
        "supersession": supersession,
        "history": history,
        "neighborhood": pack.get("neighborhood", []),
        "contradictions": pack.get("contradictions", {}),
        "truncation": _dedupe(truncation),
    }


def _context_hits(
    vault_root: Path, *, path: str | None, query: str | None, limit: int
) -> list[Hit]:
    if path:
        try:
            canonical_path = get_page.get_page(vault_root, path=path).path
        except get_page.GetError as exc:
            raise ValueError(f"{exc.code}: {exc.reason}") from exc
        page = find_module._CACHE.get(vault_root / canonical_path, vault_root)
        if page is None:
            raise ValueError(f"NOT_FOUND: no readable page at {canonical_path}")
        return [_hit_for_page(page)]
    return find_module.find(
        vault_root,
        query=query or "",
        limit=limit,
        scope="kb",
        mode="hybrid",
        graph=True,
        rerank=False,
        prefer_compiled=True,
        prefer_active=True,
        prefer_used=False,
    )


def _hit_for_page(page: ParsedPage) -> Hit:
    return Hit(
        path=page.rel_path,
        type=page.page_type,
        scope=page.scope,
        title=page.title,
        updated=page.updated,
        excerpt="",
        status=page.status,
        superseded_by=page.superseded_by,
    )


def _hits_for_graph_seeds(
    vault_root: Path, graph: dict[str, Any], *, limit: int
) -> list[Hit]:
    hits: list[Hit] = []
    paths: set[str] = set()
    for seed in graph.get("seeds", []):
        rel = str(seed.get("path") or "")
        if not rel or rel in paths:
            continue
        paths.add(rel)
        page = find_module._CACHE.get(vault_root / rel, vault_root)
        if page is not None:
            hits.append(_hit_for_page(page))
        if len(hits) >= limit:
            break
    return hits


def _validate_unit_parent_path(vault_root: Path, *, unit_ref: str, path: str) -> None:
    parent_paths, work_exhausted = epistemic_graph.indexed_unit_parent_path_resolution(
        vault_root, unit_ref
    )
    if work_exhausted:
        raise ValueError(
            "INVALID_CONTEXT: unit_ref parent validation work capped at "
            f"{epistemic_graph.UNIT_PARENT_REF_MAX_CANDIDATES}; cannot verify path"
        )
    if parent_paths and path not in parent_paths:
        raise ValueError(
            "INVALID_CONTEXT: unit_ref parent "
            f"{', '.join(parent_paths)} does not match path {path}"
        )


def _merge_graph_contexts(
    vault_root: Path,
    pages: list[ParsedPage],
    *,
    depth: int,
    relation_types: list[str] | None,
    node_types: list[str] | None,
    max_nodes: int,
    max_edges: int,
    traversal_profile: str | None = None,
) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    seeds: dict[str, dict[str, Any]] = {}
    warnings: list[Any] = []
    truncation: list[str] = []
    dropped_nodes = 0
    dropped_edges = 0
    available = False
    resolved_profile: dict[str, Any] | None = None
    registry_metadata: dict[str, Any] | None = None
    included_families: set[str] = set()
    excluded = {"profile": 0, "scope_violation": 0, "unregistered": 0}
    for page in pages:
        context = epistemic_graph.graph_context(
            vault_root,
            path=page.rel_path,
            depth=depth,
            relation_types=relation_types,
            node_types=node_types,
            max_nodes=max_nodes,
            max_edges=max_edges,
            traversal_profile=traversal_profile,
        )
        if not context.get("available"):
            warnings.append(str(context.get("reason") or "graph unavailable"))
            continue
        available = True
        resolved_profile = resolved_profile or context.get("profile")
        registry_metadata = registry_metadata or context.get("registry")
        included_families.update(context.get("included_relation_families", []))
        for key in excluded:
            excluded[key] += int(context.get("excluded", {}).get(key, 0))
        for seed in context.get("seeds", []):
            seeds.setdefault(seed["node_key"], seed)
        for node in context.get("nodes", []):
            key = node["node_key"]
            if key in nodes:
                continue
            if len(nodes) < max_nodes:
                nodes[key] = node
            else:
                dropped_nodes += 1
        for edge in context.get("edges", []):
            key = edge["edge_key"]
            if key in edges:
                continue
            if len(edges) < max_edges:
                edges[key] = edge
            else:
                dropped_edges += 1
        truncation.extend(str(item) for item in context.get("truncation", []))
        warnings.extend(context.get("warnings", []))
    if dropped_nodes:
        truncation.append(f"merged nodes capped at {max_nodes} ({dropped_nodes} more not shown)")
    if dropped_edges:
        truncation.append(f"merged edges capped at {max_edges} ({dropped_edges} more not shown)")
    return {
        "available": available,
        "reason": None if available else (warnings[0] if warnings else "no graph seeds"),
        "seeds": list(seeds.values()),
        "nodes": list(nodes.values()),
        "edges": list(edges.values()),
        "truncation": _dedupe(truncation),
        "warnings": _dedupe(warnings),
        "profile": resolved_profile,
        "registry": registry_metadata,
        "included_relation_families": sorted(included_families),
        "excluded": excluded,
    }


def _bounded(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    marker = "\n\n[truncated]"
    return text[: max(0, limit - len(marker))].rstrip() + marker, True


def _link_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return _dedupe(item for child in value for item in _link_values(child))
    if isinstance(value, dict):
        return _dedupe(item for child in value.values() for item in _link_values(child))
    raw = str(value).strip()
    if raw.startswith("[[") and raw.endswith("]]"):
        raw = raw[2:-2].split("|", 1)[0]
    return [raw] if raw else []


def _dedupe(values) -> list:
    out: list[Any] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out
