"""Bounded, deterministic context for one stable review item."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import (
    access,
    attention,
    context_refs,
    epistemic_graph,
    evolution,
    get_page,
    memory_refs,
    vault,
)
from . import find as find_module

_SAFE_FRONTMATTER = frozenset(
    {
        "type",
        "status",
        "created",
        "updated",
        "tags",
        "project",
        "projects",
        "sources",
        "evidence",
        "evidences",
        "evidence_paths",
        "evidence_file",
        "supersedes",
        "superseded_by",
        "exomem_id",
    }
)
_EVIDENCE_FIELDS = ("evidence", "evidences", "evidence_paths", "evidence_file")


def assemble(
    vault_root: Path,
    *,
    ref: str,
    expected_fingerprint: str | None = None,
    max_body_chars: int = 4000,
    max_related_pages: int = 8,
    max_graph_nodes: int = 30,
    max_graph_edges: int = 60,
    max_history: int = 10,
    max_evolution_versions: int = 10,
) -> dict[str, Any]:
    """Compose recorded review context without reasoning or mutation."""
    limits = _limits(
        max_body_chars=max_body_chars,
        max_related_pages=max_related_pages,
        max_graph_nodes=max_graph_nodes,
        max_graph_edges=max_graph_edges,
        max_history=max_history,
        max_evolution_versions=max_evolution_versions,
    )
    item = attention.item_by_ref(vault_root, ref)
    if expected_fingerprint and expected_fingerprint != item.fingerprint:
        raise ValueError(
            "REVIEW_ITEM_CHANGED: the review signal changed; refresh the worklist "
            f"and inspect {item.ref} again"
        )
    target_path = str(item.path)
    if not access.is_indexable(vault_root, target_path):
        raise ValueError("PERMISSION_DENIED: review target is in an excluded tree")

    try:
        page_result = get_page.get_page(vault_root, path=target_path)
    except get_page.GetError as exc:
        raise ValueError(f"{exc.code}: {exc.reason}") from exc
    parsed = find_module._CACHE.get(vault_root / page_result.path, vault_root)
    if parsed is None:
        raise ValueError(f"NOT_FOUND: no readable page at {page_result.path}")

    ref_index = memory_refs.ReferenceIndex(vault_root)
    truncation: list[str] = []
    body, body_truncated = _bounded(page_result.body, limits["max_body_chars"])
    if body_truncated:
        truncation.append(
            f"target body capped at {limits['max_body_chars']} characters"
        )
    target = {
        "path": page_result.path,
        "ref": item.target_ref or _reference_for_path(ref_index, page_result.path),
        "title": parsed.title,
        "type": parsed.page_type,
        "status": parsed.status,
        "frontmatter": {
            key: _json_value(value)
            for key, value in page_result.frontmatter.items()
            if key in _SAFE_FRONTMATTER
        },
        "body": body,
        "body_truncated": body_truncated,
        "body_chars": len(page_result.body),
        "content_hash": page_result.content_hash,
        "mtime": page_result.mtime,
    }

    graph = _graph_section(
        vault_root,
        path=page_result.path,
        ref_index=ref_index,
        max_nodes=limits["max_graph_nodes"],
        max_edges=limits["max_graph_edges"],
    )
    truncation.extend(str(value) for value in graph.get("truncation", []))
    related = _related_section(
        vault_root,
        item=item,
        target_path=page_result.path,
        graph=graph,
        ref_index=ref_index,
        limit=limits["max_related_pages"],
    )
    if related["truncated"]:
        truncation.append(
            f"related pages capped at {limits['max_related_pages']} "
            f"({related['truncated']} more not shown)"
        )
    provenance = _provenance_section(
        vault_root,
        page_result.frontmatter,
        ref_index=ref_index,
    )
    history = _history_section(
        vault_root,
        page_result.path,
        limit=limits["max_history"],
    )
    if history["truncated"]:
        truncation.append(
            f"history capped at {limits['max_history']} "
            f"({history['truncated']} more not shown)"
        )
    evolution_section = _evolution_section(
        vault_root,
        parsed,
        ref_index=ref_index,
        max_versions=limits["max_evolution_versions"],
    )
    truncation.extend(str(value) for value in evolution_section.get("truncation", []))

    availability = {
        "target": True,
        "related": bool(related["items"]),
        "provenance": provenance["available"],
        "graph": bool(graph["available"]),
        "history": bool(history["items"]),
        "evolution": bool(evolution_section["available"]),
    }
    return {
        "item": item.as_dict(),
        "target": target,
        "related": related,
        "provenance": provenance,
        "graph": graph,
        "history": history,
        "evolution": evolution_section,
        "availability": availability,
        "truncation": _dedupe(truncation),
    }


def _limits(**values: int) -> dict[str, int]:
    out: dict[str, int] = {}
    caps = {
        "max_body_chars": 12000,
        "max_related_pages": 50,
        "max_graph_nodes": 200,
        "max_graph_edges": 400,
        "max_history": 100,
        "max_evolution_versions": 100,
    }
    for name, value in values.items():
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"INVALID_REVIEW_CONTEXT: {name} must be an integer") from exc
        if parsed < 0:
            raise ValueError(f"INVALID_REVIEW_CONTEXT: {name} must be non-negative")
        out[name] = min(parsed, caps[name])
    return out


def _bounded(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    if limit == 0:
        return "", True
    marker = "\n\n[truncated]"
    if limit <= len(marker):
        return marker[:limit], True
    return text[: limit - len(marker)].rstrip() + marker, True


def _graph_section(
    vault_root: Path,
    *,
    path: str,
    ref_index: memory_refs.ReferenceIndex,
    max_nodes: int,
    max_edges: int,
) -> dict[str, Any]:
    if max_nodes == 0:
        return {
            "available": True,
            "reason": None,
            "nodes": [],
            "edges": [],
            "shown_nodes": 0,
            "shown_edges": 0,
            "truncated_nodes": 0,
            "truncated_edges": 0,
            "truncation": [],
        }
    try:
        raw = epistemic_graph.graph_context(
            vault_root,
            path=path,
            depth=1,
            max_nodes=max_nodes,
            max_edges=max_edges,
        )
    except Exception as exc:  # noqa: BLE001 - optional section soft-fails by contract
        return _unavailable_graph(str(exc))
    if not raw.get("available"):
        return _unavailable_graph(str(raw.get("reason") or "graph unavailable"))

    nodes = []
    allowed_keys: set[str] = set()
    for node in raw.get("nodes", []):
        node_path = str(node.get("path") or "")
        if node_path and not access.is_indexable(vault_root, node_path):
            continue
        safe = {
            key: node.get(key)
            for key in (
                "node_key",
                "kind",
                "path",
                "anchor",
                "title",
                "line_start",
                "line_end",
            )
        }
        if node_path:
            safe["ref"] = _reference_for_path(ref_index, node_path)
        nodes.append(safe)
        if safe.get("node_key"):
            allowed_keys.add(str(safe["node_key"]))
    edges = [
        {
            key: edge.get(key)
            for key in (
                "edge_key",
                "src_key",
                "dst_key",
                "relation_type",
                "raw_relation",
                "origin",
                "source_path",
                "source_anchor",
            )
        }
        for edge in raw.get("edges", [])
        if str(edge.get("src_key")) in allowed_keys
        and str(edge.get("dst_key")) in allowed_keys
    ]
    safe_edges = []
    for edge in edges[:max_edges]:
        source_path = str(edge.get("source_path") or "")
        if source_path:
            edge["source_ref"] = _reference_for_path(ref_index, source_path)
        safe_edges.append(edge)
    return {
        "available": True,
        "reason": None,
        "nodes": nodes[:max_nodes],
        "edges": safe_edges,
        "shown_nodes": min(len(nodes), max_nodes),
        "shown_edges": min(len(edges), max_edges),
        "truncated_nodes": max(0, len(nodes) - max_nodes),
        "truncated_edges": max(0, len(edges) - max_edges),
        "truncation": list(raw.get("truncation", [])),
    }


def _unavailable_graph(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "nodes": [],
        "edges": [],
        "shown_nodes": 0,
        "shown_edges": 0,
        "truncated_nodes": 0,
        "truncated_edges": 0,
    }


def _related_section(
    vault_root: Path,
    *,
    item,
    target_path: str,
    graph: dict[str, Any],
    ref_index: memory_refs.ReferenceIndex,
    limit: int,
) -> dict[str, Any]:
    candidates: list[str] = []
    for reason in item.reasons:
        candidates.extend(str(path) for path in reason.get("related_paths", []))
    candidates.extend(
        str(node.get("path"))
        for node in graph.get("nodes", [])
        if node.get("kind") == "file" and node.get("path")
    )
    candidates = [
        path
        for path in _dedupe(candidates)
        if path != target_path and access.is_indexable(vault_root, path)
    ]
    readable_candidates = [path for path in candidates if (vault_root / path).is_file()]
    rows = []
    selected = readable_candidates[:limit] if limit > 0 else []
    for path in selected:
        try:
            result = get_page.get_page(vault_root, path=path)
        except get_page.GetError:
            continue
        parsed = find_module._CACHE.get(vault_root / result.path, vault_root)
        if parsed is None:
            continue
        excerpt = " ".join(result.body.split())[:320]
        rows.append(
            {
                "path": result.path,
                "ref": _reference_for_path(ref_index, result.path),
                "title": parsed.title,
                "type": parsed.page_type,
                "status": parsed.status,
                "excerpt": excerpt,
            }
        )
    return {
        "available": bool(readable_candidates),
        "items": rows,
        "shown": len(rows),
        "total": len(readable_candidates),
        "truncated": len(readable_candidates) - len(rows),
    }


def _provenance_section(
    vault_root: Path,
    frontmatter: dict[str, Any],
    *,
    ref_index: memory_refs.ReferenceIndex,
) -> dict[str, Any]:
    sources = _provenance_rows(
        vault_root,
        _link_values(frontmatter.get("sources")),
        ref_index=ref_index,
    )
    evidence_values: list[str] = []
    for field in _EVIDENCE_FIELDS:
        evidence_values.extend(_link_values(frontmatter.get(field)))
    evidence = _provenance_rows(
        vault_root,
        _dedupe(evidence_values),
        ref_index=ref_index,
    )
    return {"available": bool(sources or evidence), "sources": sources, "evidence": evidence}


def _provenance_rows(
    vault_root: Path,
    values: Iterable[str],
    *,
    ref_index: memory_refs.ReferenceIndex,
) -> list[dict[str, Any]]:
    rows = []
    for value in values:
        path = _normalize_link(value)
        if not path or not access.is_indexable(vault_root, path):
            continue
        exists = (vault_root / path).is_file()
        rows.append(
            {
                "path": path,
                "ref": _reference_for_path(ref_index, path),
                "exists": exists,
            }
        )
    return rows


def _history_section(vault_root: Path, path: str, *, limit: int) -> dict[str, Any]:
    rows = vault.read_log_entries(vault_root, path)
    shown = rows[:limit] if limit > 0 else []
    return {
        "available": True,
        "items": shown,
        "shown": len(shown),
        "total": len(rows),
        "truncated": len(rows) - len(shown),
    }


def _evolution_section(
    vault_root: Path,
    page,
    *,
    ref_index: memory_refs.ReferenceIndex,
    max_versions: int,
) -> dict[str, Any]:
    try:
        result = evolution.evolution_for_page(
            vault_root,
            page=page,
            target_path=page.rel_path,
            max_versions=max_versions,
        )
    except Exception as exc:  # noqa: BLE001 - optional section soft-fails by contract
        return {
            "available": False,
            "reason": str(exc),
            "target_path": page.rel_path,
            "timelines": [],
            "truncation": [],
        }
    for timeline in result.get("timelines", []):
        for version in timeline.get("versions", []):
            version_path = str(version.get("path") or "")
            if version_path:
                version["ref"] = _reference_for_path(ref_index, version_path)
    return {"available": True, "reason": None, **result}


def _reference_for_path(ref_index: memory_refs.ReferenceIndex, path: str) -> str:
    if ref := ref_index.ref_for_path(path):
        return ref
    if path.startswith("Knowledge Base/Sources/"):
        return context_refs.source_ref(path)
    return context_refs.vault_ref(path)


def _link_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return _dedupe(item for child in value for item in _link_values(child))
    if isinstance(value, dict):
        return _dedupe(item for child in value.values() for item in _link_values(child))
    raw = str(value).strip()
    return [raw] if raw else []


def _normalize_link(value: str) -> str:
    raw = str(value or "").strip()
    if raw.startswith("[[") and raw.endswith("]]" ):
        raw = raw[2:-2].split("|", 1)[0].strip()
    if not raw or "://" in raw:
        return raw
    raw = raw.replace("\\", "/").lstrip("/")
    if "." not in raw.rsplit("/", 1)[-1]:
        raw += ".md"
    return raw


def _dedupe(values: Iterable[Any]) -> list[Any]:
    out: list[Any] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_value(child) for child in value]
    if isinstance(value, tuple):
        return [_json_value(child) for child in value]
    if isinstance(value, dict):
        return {str(key): _json_value(child) for key, child in value.items()}
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
