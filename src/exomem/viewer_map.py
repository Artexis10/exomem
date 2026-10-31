"""A bounded, request-local page reduction of one admitted graph snapshot."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from heapq import merge
from pathlib import Path
from typing import Any, Literal

from typing_extensions import TypedDict

from . import epistemic_graph as graph
from . import find, freshness, memory_refs, request_budget, traversal_profiles
from .governance import egress
from .retrieval_models import ToolFailureEnvelope

# These ceilings bound work and delivery under the viewer bridge's five-second timeout.
# A false early stop costs the reader a visibly partial map, rather than a failed call.
_SECONDS = 3.5
_DELIVERY_SECONDS = 0.5
_MAX_PAGES = 8000
_MAX_EDGES = 32000
_BATCH = 64
# These per-author protocol bounds preserve the bridge deadline; false misses cost partial edges.
_SOURCE_BYTES = 64 * 1024
_SUMMARY_BYTES = 256 * 1024
_PREPARATION_BYTES = 4 * 1024 * 1024


class MapCounts(TypedDict):
    eligible_nodes: int | None
    eligible_edges: int | None
    observed_nodes: int
    observed_edges: int
    drawn_nodes: int
    drawn_edges: int


class MapTruncation(TypedDict):
    stage: str
    reason: str
    limit: int | float | None


class ViewerMap(TypedDict):
    # OpenSpec's bounded viewer map fixes these three coverage states.
    state: Literal["complete", "partial", "unavailable"]
    available: bool
    reason: str | None
    snapshot: dict[str, str] | None
    observed_at: str
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    vocabulary: list[dict[str, Any]]
    counts: MapCounts
    ranking_complete: bool
    truncation: list[MapTruncation]


def _limit(name: str, default: float) -> float:
    """An operator can shorten a bound, but cannot remove its hard ceiling."""
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return min(default, value) if math.isfinite(value) and value >= 1 else default


def op_viewer_map(
    vault_root: Path,
    max_nodes: int = 200,
    max_edges: int = 400,
    purpose: str | None = None,
) -> ViewerMap | ToolFailureEnvelope:
    """Return a bounded global map of admitted current graph pages. Read-only.

    Rank pages by admitted incoming cross-page edges, then graph node key.
    Include isolated pages and collapse semantic-unit endpoints to their pages.
    Relation vocabulary retains source instance and definition identities.

    Complete scans report exact eligible totals even when drawing is capped.
    Partial scans report observed counts, unknown exact totals and provisional
    ranking. Missing, stale, unproven or incompatible snapshots are unavailable.
    No interactive request builds or proves a cold graph snapshot.

    Args:
        max_nodes: Drawing cap, from 1 to 200.
        max_edges: Drawing cap, from 0 to 400.
        purpose: Declared purpose under the existing disclosure policy.

    Returns:
        Coverage state, snapshot identity, observation time, admitted page nodes
        and edges, drawn vocabulary, counts, ranking completeness and structured
        truncation. Graph keys identify the current snapshot; memory_ref carries
        a persistent identity when known. Path keys do not promise rename stability.
    """
    started = time.monotonic()
    seconds = _limit("EXOMEM_VIEWER_MAP_BUDGET_SECONDS", _SECONDS)
    budget = request_budget.RequestBudget(seconds=seconds, entry=started)
    outer = request_budget.current()
    if outer is not None:
        budget.deadline = min(budget.deadline, outer.deadline)
    page_limit = int(_limit("EXOMEM_VIEWER_MAP_MAX_INSPECTED_PAGES", _MAX_PAGES))
    edge_limit = int(_limit("EXOMEM_VIEWER_MAP_MAX_INSPECTED_EDGES", _MAX_EDGES))
    max_nodes = min(max(1, int(max_nodes)), traversal_profiles.MAX_NODES)
    max_edges = min(max(0, int(max_edges)), traversal_profiles.MAX_EDGES)
    result: ViewerMap = {
        "state": "unavailable", "available": False, "reason": None, "snapshot": None,
        "observed_at": datetime.now(UTC).isoformat(), "nodes": [], "edges": [],
        "vocabulary": [], "counts": {
            "eligible_nodes": None, "eligible_edges": None, "observed_nodes": 0,
            "observed_edges": 0, "drawn_nodes": 0, "drawn_edges": 0,
        }, "ranking_complete": False, "truncation": [],
    }
    refusals: list[str] = []
    outcomes: list[str] = []
    index = graph.EpistemicGraphIndex(vault_root, prove_cold_snapshots=False)
    conn = index._open_read_snapshot(
        prove=False, allow_cold_projection=False, outcome_out=outcomes, refusal_out=refusals,
    )
    if conn is None:
        result["reason"] = next(iter(outcomes or refusals), "graph_snapshot_unavailable")
        return result

    def stop(stage: str, reason: str, limit: int | float | None) -> None:
        item: MapTruncation = {"stage": stage, "reason": reason, "limit": limit}
        if item not in result["truncation"]:
            result["truncation"].append(item)

    def expired(stage: str) -> bool:
        if budget.can_afford(_DELIVERY_SECONDS):
            return False
        stop(stage, "deadline", seconds)
        if outer is not None:
            outer.note_truncated("viewer_map")
        return True

    def preparation_check() -> None:
        if expired("inspection_edges"):
            raise TimeoutError("viewer preparation deadline")

    vocabulary: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        # Interrupt expensive SQLite reads too; Python boundaries alone cannot bound SQL work.
        metadata = dict(conn.execute(
            "SELECT key, value FROM graph_meta WHERE key IN "
            "('schema_version', 'generation', 'instance', 'recall_projection_identity')"
        ))
        result["snapshot"] = metadata
        result["available"] = True
        result["reason"] = None
        conn.set_progress_handler(lambda: int(not budget.can_afford(_DELIVERY_SECONDS)), 1000)
        keep = egress.release_walk_filter(vault_root, purpose=purpose)
        # Borrow exact resident topology: a false miss costs partial edges, never a cold corpus walk.
        checkpoint = freshness.live_recall_checkpoint(vault_root, "vault")
        projection = ((checkpoint.triple, checkpoint.policy_version, checkpoint.access_policy_fingerprint)
                      if checkpoint is not None else None)
        resolver = (find.recall_resolver_snapshot_at_checkpoint(vault_root, checkpoint)
                    if checkpoint is not None and projection is not None
                    and metadata.get("recall_projection_identity") == graph._availability_freshness_value(projection)
                    else None)
        if checkpoint != freshness.live_recall_checkpoint(vault_root, "vault"):
            resolver = None
        edge_coverage = keep is None or resolver is not None
        if not edge_coverage:
            stop("inspection_edges", "resolver_unavailable", None)
        view = graph.GraphView(
            vault_root, conn, keep=keep, resolver=resolver, reads_placeholders=False,
            check=preparation_check, checkpoint=checkpoint, source_limit=_SOURCE_BYTES,
            summary_limit=_SUMMARY_BYTES, preparation_limit=_PREPARATION_BYTES,
        )
        profiles: dict[str, traversal_profiles.TraversalProfile] = {}
        degrees: dict[str, int] = {}
        paths: dict[str, str] = {}
        inspected_pages = 0
        after = ""
        while not expired("inspection_pages"):
            rows = conn.execute(
                "SELECT node_key, path FROM graph_nodes WHERE kind = 'file' "
                "AND node_key > ? ORDER BY node_key LIMIT ?", (after, _BATCH),
            ).fetchall()
            if not rows:
                break
            for key, path in rows:
                after = str(key)
                if expired("inspection_pages"):
                    break
                if not view.allowed(str(path)):
                    continue
                # Only an extra admitted page can exhaust semantic coverage.
                if inspected_pages == page_limit:
                    stop("inspection_pages", "work_limit", page_limit)
                    break
                inspected_pages += 1
                degrees[str(key)] = 0
                paths[str(path)] = str(key)
                result["counts"]["observed_nodes"] = len(degrees)
            if any(t["stage"] == "inspection_pages" for t in result["truncation"]):
                break

        # Retain degrees, not a materialized graph. A second bounded read draws only selected edges.
        observed_edges = 0
        edge_after = ("", 0)
        changed: dict[str, list[dict[str, Any]]] = {}
        authors = sorted(paths)
        for offset in range(0, len(authors), _BATCH):
            if expired("inspection_edges"):
                break
            view.preflight(authors[offset:offset + _BATCH])
        if keep is not None and edge_coverage:
            for author in authors:
                if expired("inspection_edges"):
                    break
                # Ambiguous links can have dependencies without any stored edge row.
                replacement = view.authored_edges(author)
                if replacement is not None:
                    changed[author] = replacement
        observed_after = ("", 0)
        observed_replacements: dict[str, int] = {}

        def admitted_edge(row: dict[str, Any]) -> dict[str, Any] | None:
            author = str(row.get("source_path") or "")
            if author not in paths or author in view.interrupted or not view.allowed(author):
                return None
            source = view.node_path(str(row.get("src_key") or ""))
            target = view.node_path(str(row.get("dst_key") or ""))
            if source not in paths or target not in paths or source == target:
                return None
            served = view.edge(row)
            if served is None:
                return None
            # GraphView interprets candidates before the graph protocol's scope-status exclusion.
            if served.get("registry_status") in {"unregistered", "scope_violation"}:
                return None
            if any(view.node(str(served[field])) is None for field in ("src_key", "dst_key")):
                return None
            parent = view.parent(author)
            if parent is None:
                return None
            registry = parent.relations
            definition = registry.definition(str(served.get("relation_type") or ""))
            if definition is None:
                return None
            instance = "core" if definition.core else str(parent.instance_id)
            profile_key = str(parent.instance_id)
            if profile_key not in profiles:
                profiles[profile_key] = traversal_profiles.load_profiles(
                    vault_root, registry=registry,
                ).resolve(None)
            if not traversal_profiles.relation_allowed(profiles[profile_key], definition):
                return None
            return {
                **served, "src_key": paths[source], "dst_key": paths[target],
                "relation_identity": {"instance": instance, "definition_key": definition.key},
            }

        def raw_edges() -> Iterator[tuple[str, int, dict[str, Any]]]:
            nonlocal edge_after
            if not edge_coverage:
                return
            admitted_authors = json.dumps([author for author in authors if author not in view.interrupted])
            while not expired("inspection_edges"):
                rows = conn.execute(
                    f"SELECT source_path, rowid, {graph.edge_columns('')} FROM graph_edges "
                    "WHERE source_path IN (SELECT value FROM json_each(?)) "
                    "AND (source_path, rowid) > (?, ?) ORDER BY source_path, rowid LIMIT ?",
                    (admitted_authors, *edge_after, _BATCH),
                ).fetchall()
                if not rows:
                    return
                for author, position, *columns in rows:
                    edge_after = str(author), int(position)
                    if expired("inspection_edges"):
                        return
                    if author not in paths or not view.allowed(str(author)):
                        continue
                    if author not in changed:
                        yield str(author), int(position), graph._edge_row_to_dict(columns)

        def replacement_edges() -> Iterator[tuple[str, int, dict[str, Any]]]:
            for author in sorted(changed):
                for position, edge in enumerate(changed[author]):
                    if expired("inspection_edges"):
                        return
                    yield author, position, edge

        for author, position, raw in merge(
            raw_edges(), replacement_edges(), key=lambda row: row[:2],
        ):
            if expired("inspection_edges"):
                break
            edge = admitted_edge(raw)
            if edge is not None and observed_edges == edge_limit:
                stop("inspection_edges", "work_limit", edge_limit)
                break
            observed_after = author, position
            if author in changed:
                observed_replacements[author] = position + 1
            if edge is not None:
                observed_edges += 1
                result["counts"]["observed_edges"] = observed_edges
                degrees[str(edge["dst_key"])] += 1

        selected = sorted(degrees, key=lambda key: (-degrees[key], key))[:max_nodes]
        for key in selected:
            if expired("drawing"):
                break
            row = conn.execute(
                "SELECT path, title, source_hash, exomem_id, page_type, origin_date, updated_date "
                "FROM graph_nodes "
                "WHERE node_key = ? AND kind = 'file'", (key,),
            ).fetchone()
            if row is None:
                continue
            path, title, source_hash, identity, page_type, origin_date, updated_date = row
            result["nodes"].append({
                "node_key": key, "kind": "file", "path": path,
                "title": str(graph._served(path, title) or "")[:240],
                "source_hash": source_hash,
                "incoming_edges": degrees[key] if edge_coverage else None,
                "memory_ref": memory_refs.memory_ref(identity) if identity else None,
                "page_type": page_type, "origin_date": origin_date, "updated_date": updated_date,
            })
        chosen_paths = [str(node["path"]) for node in result["nodes"]]
        chosen_keys = {str(node["node_key"]) for node in result["nodes"]}
        draw_candidates = 0

        def draw(raw: dict[str, Any]) -> None:
            nonlocal draw_candidates
            edge = admitted_edge(raw)
            if edge is None or edge["src_key"] not in chosen_keys or edge["dst_key"] not in chosen_keys:
                return
            draw_candidates += 1
            if len(result["edges"]) == max_edges:
                return
            identity = edge["relation_identity"]
            registry = view.registry_for(str(edge["source_path"]))
            definition = registry.definition(str(identity["definition_key"]))
            assert definition is not None
            result["edges"].append({
                "edge_key": edge["edge_key"], "src_key": edge["src_key"],
                "dst_key": edge["dst_key"], "relation_type": edge["relation_type"],
                "source_path": edge["source_path"], "relation_identity": identity,
            })
            vocabulary[(str(identity["instance"]), definition.key)] = {
                **identity, "description": definition.description[:1000],
                "family": definition.family, "direction": definition.direction,
                "presentation_role": definition.presentation_role,
            }

        # The draw query excludes endpoints outside the selected page set, including unit parents.
        def drawing_rows() -> Iterator[tuple[str, int, dict[str, Any]]]:
            encoded_paths = json.dumps(chosen_paths)
            encoded_authors = json.dumps([author for author in authors if author not in view.interrupted])
            cursor = ("", 0)
            while not expired("drawing"):
                rows = conn.execute(
                    f"SELECT e.source_path, e.rowid, {graph.EDGE_COLUMNS} FROM graph_edges e "
                    "JOIN graph_nodes s ON s.node_key = e.src_key "
                    "JOIN graph_nodes d ON d.node_key = e.dst_key "
                    "WHERE s.path IN (SELECT value FROM json_each(?)) "
                    "AND d.path IN (SELECT value FROM json_each(?)) "
                    "AND e.source_path IN (SELECT value FROM json_each(?)) "
                    "AND (e.source_path, e.rowid) > (?, ?) "
                    "AND (e.source_path, e.rowid) <= (?, ?) "
                    "ORDER BY e.source_path, e.rowid LIMIT ?",
                    (encoded_paths, encoded_paths, encoded_authors, *cursor, *observed_after, _BATCH),
                ).fetchall()
                if not rows:
                    break
                for author, position, *columns in rows:
                    cursor = str(author), int(position)
                    if author not in changed:
                        yield str(author), int(position), graph._edge_row_to_dict(columns)

        def drawing_replacements() -> Iterator[tuple[str, int, dict[str, Any]]]:
            for author in sorted(changed):
                for position, edge in enumerate(changed[author][:observed_replacements.get(author, 0)]):
                    yield author, position, edge

        if chosen_paths and not expired("drawing"):
            # Both streams keep author order before the cap, including authors with no stored row.
            for _author, _position, raw in merge(
                drawing_rows(), drawing_replacements(), key=lambda row: row[:2],
            ):
                if expired("drawing"):
                    break
                draw(raw)
                if draw_candidates > max_edges:
                    break
        if view.unavailable:
            stop("inspection", "selected_interpretation_unavailable", None)
        for reason in sorted(set(view.interrupted.values())):
            limit = {"source_work_limit": _SOURCE_BYTES, "summary_work_limit": _SUMMARY_BYTES,
                     "preparation_work_limit": _PREPARATION_BYTES}.get(reason)
            stop("inspection_edges", reason, limit)
        if resolver is not None and checkpoint != freshness.live_recall_checkpoint(vault_root, "vault"):
            # Publishers can advance shared topology during a read; withhold mixed edges, retain partial pages.
            stop("inspection_edges", "resolver_checkpoint_changed", None)
            result["edges"] = []
            for node in result["nodes"]:
                node["incoming_edges"] = None
        complete = not result["truncation"]
        if len(degrees) > max_nodes:
            stop("output_nodes", "drawing_limit", max_nodes)
        if observed_edges > len(result["edges"]):
            stop("output_edges", "drawing_limit" if draw_candidates > max_edges
                 else "endpoints_not_drawn", max_edges if draw_candidates > max_edges else None)
        result["state"] = "complete" if complete else "partial"
        result["ranking_complete"] = complete
        result["counts"].update({
            "eligible_nodes": len(degrees) if complete else None,
            "eligible_edges": observed_edges if complete else None,
            "observed_nodes": len(degrees), "observed_edges": observed_edges,
        })
    except TimeoutError:
        stop("inspection", "deadline", seconds)
        result["state"] = "partial"
        result["ranking_complete"] = False
    except sqlite3.OperationalError as error:
        if error.sqlite_errorcode != sqlite3.SQLITE_INTERRUPT:
            raise
        stop("inspection", "deadline", seconds)
        result["state"] = "partial"
        result["ranking_complete"] = False
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()
    if not result["ranking_complete"]:
        result["counts"]["eligible_nodes"] = None
        result["counts"]["eligible_edges"] = None
        for node in result["nodes"]:
            node["incoming_edges"] = None
    # Interrupted SQL reaches the same disclosure guard and truthful drawn counts.
    guarded = egress.guard_graph_context(vault_root, result, purpose=purpose)
    result.update(guarded)
    drawn_meanings = {(e["relation_identity"]["instance"], e["relation_identity"]["definition_key"])
                     for e in result["edges"]}
    result["vocabulary"] = [vocabulary[key] for key in sorted(drawn_meanings)]
    result["counts"]["drawn_nodes"] = len(result["nodes"])
    result["counts"]["drawn_edges"] = len(result["edges"])
    return result
