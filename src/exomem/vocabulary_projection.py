"""Bounded current-write graph/provenance evidence for vocabulary review."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import (
    entity_types,
    epistemic_graph,
    memory_refs,
    vocabulary_provenance,
    vocabulary_review,
)
from .vocabulary_signals import consider_generic_pair
from .vocabulary_workflow import Evidence, _hash, make_item

EDGE_PAGE = 4
SOURCE_PAGE = 8


def _generation(connection) -> str:
    values = dict(
        connection.execute(
            "SELECT key, value FROM graph_meta WHERE key IN ('instance', 'generation')"
        ).fetchall()
    )
    if set(values) != {"instance", "generation"} or not all(values.values()):
        raise ValueError("VOCABULARY_EVIDENCE_UNAVAILABLE: projection identity unavailable")
    return _hash(values)


def _live_snapshot(vault_root: Path, path: str, target_hash: str, generation: str) -> bool:
    connection = epistemic_graph.EpistemicGraphIndex(vault_root)._open_read_snapshot()
    if connection is None:
        return False
    try:
        anchor = connection.execute(
            "SELECT source_hash FROM graph_nodes WHERE node_key=?",
            (epistemic_graph._file_key(path),),
        ).fetchone()
        return anchor is not None and anchor[0] == target_hash and _generation(connection) == generation
    finally:
        connection.close()


def _cursor(kind: str, path: str, after: Any, generation: str) -> str:
    return json.dumps(
        {"kind": kind, "path": path, "after": after, "generation": generation}, sort_keys=True
    )


def _read_cursor(continuation: str, *, kind: str, generation: str) -> dict[str, Any]:
    try:
        cursor = json.loads(continuation)
        if (
            not isinstance(cursor, dict)
            or set(cursor) != {"kind", "path", "after", "generation"}
            or cursor["kind"] != kind
            or not isinstance(cursor["path"], str)
        ):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("VOCABULARY_CONTINUATION_INVALID: refresh the projection") from exc
    if cursor["generation"] != generation:
        raise ValueError("VOCABULARY_CONTINUATION_STALE: refresh the projection")
    return cursor


def _source_rows(connection, path: str, after: str = ""):
    rows = connection.execute(
        "SELECT DISTINCT t.path, t.source_hash, t.exomem_id, t.page_type "
        "FROM graph_edges e JOIN graph_nodes t ON t.node_key=e.dst_key "
        "WHERE e.src_key=? AND e.relation_type='derived_from' "
        "AND e.source_anchor='sources' AND t.kind='file' "
        "AND t.page_type IN ('source', 'evidence') AND t.path > ? ORDER BY t.path LIMIT ?",
        (epistemic_graph._file_key(path), after, SOURCE_PAGE + 1),
    ).fetchall()
    for _, _, source_id, _ in rows[:SOURCE_PAGE]:
        if (
            memory_refs.normalize_id(source_id)
            and len(
                connection.execute(
                    "SELECT node_key FROM graph_nodes WHERE exomem_id=? AND kind='file' LIMIT 2",
                    (source_id,),
                ).fetchall()
            )
            != 1
        ):
            raise ValueError("VOCABULARY_EVIDENCE_UNAVAILABLE: ambiguous source identity")
    return rows


def _source_pages(vault_root: Path, rows):
    pages, hints = [], {}
    for source_path, source_hash, source_id, source_type in rows[:SOURCE_PAGE]:
        if source_type not in {"source", "evidence"}:
            continue
        source_ref = (
            memory_refs.memory_ref(source_id)
            if memory_refs.normalize_id(source_id)
            else source_path
        )
        page = vocabulary_review._read(vault_root, {"paths": {source_ref: source_path}}, source_ref)
        if page["content_hash"] != source_hash:
            raise ValueError("VOCABULARY_EVIDENCE_UNAVAILABLE: source projection changed")
        page["ref"] = source_ref
        pages.append(page)
        hints[source_ref] = source_path
    return pages, hints


def more_evidence(vault_root: Path, *, continuation: str) -> dict[str, Any]:
    cursor = vocabulary_provenance._read_cursor(
        continuation, kind="vocabulary-provenance-context/v1"
    )
    connection = epistemic_graph.EpistemicGraphIndex(vault_root)._open_read_snapshot()
    if connection is None:
        raise ValueError("VOCABULARY_EVIDENCE_UNAVAILABLE: graph projection unavailable")
    try:
        generation = _generation(connection)
    finally:
        connection.close()
    if generation != cursor["graph_generation"]:
        raise ValueError("VOCABULARY_CONTINUATION_STALE: refresh source evidence")
    checkpoint = vocabulary_provenance.context(
        vault_root,
        continuation=continuation,
        snapshot_validator=lambda current_path, current_hash, current_generation: _live_snapshot(
            vault_root, current_path, current_hash, current_generation
        ),
    )
    hints = {entry["ref"]: entry["path"] for entry in checkpoint["sources"]}
    for entry in checkpoint["sources"]:
        page = vocabulary_review._read(vault_root, {"paths": hints}, entry["ref"])
        if page["content_hash"] != entry["version"]:
            raise ValueError("VOCABULARY_EVIDENCE_UNAVAILABLE: source projection changed")
    return {
        "evidence": [
            Evidence(origin["ref"], origin["version"], origin["independence_key"])
            for origin in checkpoint["sources"]
        ],
        "paths": hints,
        "continuation": checkpoint["continuation"],
    }


def _note_pairs(connection, path, known_types, after):
    """Page one left endpoint against four right endpoints, never a self-join.

    The graph source-path index restricts both scans to this note's links.
    At most seven neighbour rows are materialized, even for a large note.
    """
    placeholders = ",".join("?" for _ in known_types)

    def neighbours(boundary, limit, *, inclusive=False):
        comparison = ">=" if inclusive else ">"
        return connection.execute(
            "SELECT DISTINCT t.path, t.source_hash, t.exomem_id, t.page_type "
            "FROM graph_edges e JOIN graph_nodes t ON t.node_key=e.dst_key "
            "WHERE e.source_path=? AND e.relation_type IN ('links_to', 'relates_to') AND t.kind='file' "
            "AND t.lifecycle_status='active' "
            f"AND t.page_type IN ({placeholders}) AND t.path {comparison} ? "
            "ORDER BY t.path LIMIT ?",
            (epistemic_graph._with_md(path), *known_types, boundary, limit),
        ).fetchall()

    lefts = neighbours(after[0], 2, inclusive=True)
    if not lefts:
        return [], None, 0
    left = lefts[0]
    rights = neighbours(max(left[0], after[1]), EDGE_PAGE + 1)
    pairs = [(*left, *right) for right in rights[:EDGE_PAGE]]
    next_after = (
        [left[0], rights[EDGE_PAGE - 1][0]]
        if len(rights) > EDGE_PAGE
        else [lefts[1][0], ""]
        if len(lefts) > 1
        else None
    )
    return pairs, next_after, len(lefts) + len(rights)


def _anchor(connection, path):
    return connection.execute(
        "SELECT path, source_hash, exomem_id, page_type FROM graph_nodes WHERE node_key=?",
        (epistemic_graph._file_key(path),),
    ).fetchone()


def _candidate_rows(connection, path, known_types, anchor, after):
    note_pair = anchor is not None and anchor[3] not in known_types
    if note_pair:
        rows, next_after, neighbour_count = _note_pairs(connection, path, known_types, after)
    else:
        neighbour_count = 0
        rows = connection.execute(
            "SELECT s.path, s.source_hash, s.exomem_id, s.page_type, "
            "t.path, t.source_hash, t.exomem_id, t.page_type "
            "FROM graph_edges e JOIN graph_nodes s ON s.node_key=e.src_key "
            "JOIN graph_nodes t ON t.node_key=e.dst_key "
            "WHERE e.source_path=? AND e.relation_type='relates_to' "
            "AND s.kind='file' AND t.kind='file' "
            "AND s.lifecycle_status='active' AND t.lifecycle_status='active' "
            "AND (s.path, t.path) > (?, ?) "
            "GROUP BY s.node_key, t.node_key ORDER BY s.path, t.path LIMIT ?",
            (epistemic_graph._with_md(path), *after, EDGE_PAGE + 1),
        ).fetchall()
        next_after = (
            [rows[EDGE_PAGE - 1][0], rows[EDGE_PAGE - 1][4]] if len(rows) > EDGE_PAGE else None
        )
    return note_pair, rows, next_after, neighbour_count


def _anchor_is_current(vault_root: Path, path: str, target_hash: str) -> bool:
    try:
        page = vocabulary_review._read(vault_root, {"paths": {path: path}}, path)
    except (OSError, ValueError):
        return False
    return page["content_hash"] == target_hash


def _registry_currency(registry) -> tuple[int, str]:
    return registry.core_version, registry.extension_hash


def proven_empty_for_write(vault_root: Path, *, path: str) -> bool:
    """Whether one live graph snapshot proves this write cannot surface work."""
    connection = None
    proof = None
    try:
        connection = epistemic_graph.EpistemicGraphIndex(vault_root)._open_read_snapshot()
        if connection is None:
            return False
        generation = _generation(connection)
        anchor = _anchor(connection, path)
        if anchor is None:
            return False
        registry = entity_types.load_entity_types(vault_root)
        known_types = sorted(registry.active_ids)
        registry_currency = _registry_currency(registry)
        _note_pair, rows, _next_after, _neighbour_count = _candidate_rows(
            connection, path, known_types, anchor, ["", ""]
        )
        if rows:
            return False
        proof = (anchor[1], generation, registry_currency)
    except Exception:  # noqa: BLE001 - a speculative fast path must fail closed
        return False
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - a speculative fast path must fail closed
                proof = None
    if proof is None:
        return False
    target_hash, generation, registry_currency = proof
    try:
        return (
            _anchor_is_current(vault_root, path, target_hash)
            and _registry_currency(entity_types.load_entity_types(vault_root))
            == registry_currency
            and _live_snapshot(vault_root, path, target_hash, generation)
        )
    except Exception:  # noqa: BLE001 - a speculative fast path must fail closed
        return False


def for_write(vault_root: Path, *, path: str, continuation: str | None = None) -> dict[str, Any]:
    """Read one pair/edge page and one provenance page for a written page.

    No full index build or reference-sidecar fallback is allowed here. The
    snapshot owner returns unavailable while publication is incomplete.
    """
    index = epistemic_graph.EpistemicGraphIndex(vault_root)
    connection = index._open_read_snapshot()
    if connection is None:
        return {
            "status": "unavailable",
            "reason": "graph_projection_unavailable",
            "items": [],
            "signals": [],
        }
    try:
        generation = _generation(connection)
        registry = entity_types.load_entity_types(vault_root)
        known_types = sorted(registry.active_ids)
        registry_currency = _registry_currency(registry)
        anchor = _anchor(connection, path)
        note_pair = anchor is not None and anchor[3] not in known_types
        anchor_ref = anchor[0] if anchor is not None else None
        if note_pair and memory_refs.normalize_id(anchor[2]):
            matches = connection.execute(
                "SELECT path, source_hash FROM graph_nodes WHERE exomem_id=? AND kind='file' LIMIT 2",
                (anchor[2],),
            ).fetchall()
            if matches == [(anchor[0], anchor[1])]:
                anchor_ref = memory_refs.memory_ref(anchor[2])
        cursor_kind = "pairs/v1" if note_pair else "edges/v1"
        after = ["", ""]
        source_continuation = None
        source_after = ""
        if continuation is not None:
            try:
                kind = json.loads(continuation).get("kind")
            except (TypeError, ValueError, AttributeError) as exc:
                raise ValueError("VOCABULARY_CONTINUATION_INVALID: refresh written edge evidence") from exc
            if kind == "vocabulary-provenance/v1":
                source_continuation = continuation
                source_cursor = vocabulary_provenance._read_cursor(
                    continuation, kind="vocabulary-provenance/v1"
                )
                if (
                    source_cursor["path"] != path
                    or source_cursor["graph_generation"] != generation
                    or anchor is None
                    or source_cursor["target_hash"] != anchor[1]
                ):
                    raise ValueError("VOCABULARY_CONTINUATION_STALE: refresh written source evidence")
                source_after = source_cursor["after"]
            else:
                cursor = _read_cursor(continuation, kind=cursor_kind, generation=generation)
                after = cursor["after"]
                if (
                    cursor["path"] != path
                    or not isinstance(after, list)
                    or len(after) != 2
                    or not all(isinstance(part, str) for part in after)
                ):
                    raise ValueError("VOCABULARY_CONTINUATION_INVALID: refresh written edge evidence")
        note_pair, rows, next_after, neighbour_count = _candidate_rows(
            connection, path, known_types, anchor, after
        )
        source_rows = _source_rows(connection, path, source_after)
        # A UUID-shaped value is not a resolved identity when another file has
        # the same value. Probe the identity index, never the reference scanner.
        for row in rows[:EDGE_PAGE]:
            for entity_id in (row[2], row[6]):
                if (
                    memory_refs.normalize_id(entity_id)
                    and len(
                        connection.execute(
                            "SELECT node_key FROM graph_nodes WHERE exomem_id=? AND kind='file' LIMIT 2",
                            (entity_id,),
                        ).fetchall()
                    )
                    != 1
                ):
                    return {
                        "status": "unavailable",
                        "reason": "ambiguous_entity_identity",
                        "items": [],
                        "signals": [],
                    }
    finally:
        connection.close()
    if anchor is None:
        return {
            "status": "unavailable",
            "reason": "target_projection_unavailable",
            "items": [],
            "signals": [],
        }
    if not rows and (
        not _anchor_is_current(vault_root, path, anchor[1])
        or _registry_currency(entity_types.load_entity_types(vault_root)) != registry_currency
        or not _live_snapshot(vault_root, path, anchor[1], generation)
    ):
        return {
            "status": "unavailable",
            "reason": "target_projection_changed",
            "items": [],
            "signals": [],
        }
    pages, hints = _source_pages(vault_root, source_rows)
    provenance = vocabulary_provenance.advance(
        vault_root,
        path=path,
        target_hash=anchor[1],
        graph_generation=generation,
        source_page=pages,
        has_more=len(source_rows) > SOURCE_PAGE,
        continuation=source_continuation,
        snapshot_validator=lambda current_path, current_hash, current_generation: _live_snapshot(
            vault_root, current_path, current_hash, current_generation
        ),
    )
    if provenance["status"] == "warming":
        return {
            "status": "warming",
            "reason": "source_discovery_pending",
            "items": [],
            "signals": [],
            "continuation": provenance["continuation"],
            "query_budget": {
                "edge_rows": len(rows),
                "source_rows": len(source_rows),
                "neighbour_rows": neighbour_count,
            },
        }
    origins = [
        {**origin, "context": ""} for origin in provenance["signal_origins"]
    ]
    anchor_evidence = []
    if note_pair:
        page = vocabulary_review._read(vault_root, {"paths": {anchor_ref: anchor[0]}}, anchor_ref)
        if page["content_hash"] != anchor[1]:
            raise ValueError("VOCABULARY_EVIDENCE_UNAVAILABLE: written context changed")
        anchor_evidence = [Evidence(anchor_ref, anchor[1])]
        hints[anchor_ref] = anchor[0]
    items, signals = [], []
    for (
        source_path,
        source_hash,
        source_id,
        source_type,
        target_path,
        target_hash,
        target_id,
        target_type,
    ) in rows[:EDGE_PAGE]:
        if source_type not in known_types or target_type not in known_types:
            continue
        if not memory_refs.normalize_id(source_id) or not memory_refs.normalize_id(target_id):
            continue
        source_ref, target_ref = (
            memory_refs.memory_ref(source_id),
            memory_refs.memory_ref(target_id),
        )
        targets = {source_ref: source_path, target_ref: target_path}
        for ref, expected in ((source_ref, source_hash), (target_ref, target_hash)):
            page = vocabulary_review._read(vault_root, {"paths": targets}, ref)
            if page["content_hash"] != expected:
                return {
                    "status": "unavailable",
                    "reason": "target_projection_changed",
                    "items": [],
                    "signals": [],
                }
        signal = consider_generic_pair(
            source_ref=source_ref,
            target_ref=target_ref,
            resolved=True,
            relation="relates_to",
            projection_status="current",
            origins=origins,
        )
        signals.append(signal)
        if signal["eligibility"] != "eligible":
            continue
        items.append(
            make_item(
                family="relation-type/v1",
                signal="generic-pair",
                targets={source_ref: source_hash, target_ref: target_hash},
                evidence=anchor_evidence
                + [
                    Evidence(origin["ref"], origin["version"], origin["independence_key"])
                    for origin in provenance["representatives"]
                ],
                registry_hashes=vocabulary_review.registry_hashes(vault_root),
                projection_status="current",
                paths={
                    **{origin["ref"]: origin["path"] for origin in provenance["representatives"]},
                    **({anchor_ref: anchor[0]} if note_pair else {}),
                    **targets,
                },
                continuation=provenance["context_continuation"],
                projection_currency={"provenance": provenance["currency"]},
            )
        )
    return {
        "status": "current",
        "items": items,
        "signals": signals,
        "continuation": _cursor(cursor_kind, path, next_after, generation) if next_after else None,
        "query_budget": {
            "edge_rows": len(rows),
            "source_rows": len(source_rows),
            "neighbour_rows": neighbour_count,
        },
    }
