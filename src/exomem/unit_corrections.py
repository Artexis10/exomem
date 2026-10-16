"""Bounded incoming reference discovery over an already-current graph view."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from . import epistemic_graph, find, freshness, memory_refs, relation_registry, request_budget
from .governance import egress

# nosemgrep: ep-word-set -- closed relation-schema families whose authored facts inform a claim.
CORRECTIVE_FAMILIES = frozenset(
    {"contradiction", "refinement", "support", "supersession", "resolution", "answer"}
)
MAX_COUNTERPARTS = 4


def reader_view(root: Path, purpose: str | None = None) -> Callable[[str], bool] | None:
    return egress.reader_view(root, purpose=purpose)


def discover(
    root: Path,
    unit_refs: Sequence[str],
    *,
    accept: Callable[[dict[str, Any]], bool],
    keep: Callable[[str], bool] | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Discover references once; the caller admits exact units before the cap.

    No cold graph proof or resolver build belongs to optional enrichment. If
    publication moves during hydration, discard every tentative reference.
    """
    references = tuple(dict.fromkeys(
        ref for ref in unit_refs
        if "#" in ref and memory_refs.parse_memory_ref(ref.partition("#")[0]) is not None
    ))
    if not references:
        return {"status": "available", "units": []}
    budget = request_budget.current()
    if deadline is None:
        deadline = budget.deadline if budget else time.monotonic() + request_budget.ACTIVATION_DOOR_BUDGET_SECONDS
    elif budget is not None:
        deadline = min(deadline, budget.deadline)

    def check_deadline() -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError

    conn = None
    try:
        check_deadline()
        checkpoint = freshness.live_recall_checkpoint(root, "vault")
        if checkpoint is None:
            return {"status": "unavailable", "units": []}
        resolver = find.recall_resolver_snapshot_at_checkpoint(root, checkpoint)
        if resolver is None:
            return {"status": "unavailable", "units": []}
        conn = epistemic_graph.EpistemicGraphIndex(root)._open_read_snapshot(live_only=True)
        if conn is None:
            return {"status": "unavailable", "units": []}
        # The shared SQL row cap bounds volume; the progress handler bounds work.
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        check_deadline()
        view = epistemic_graph.GraphView(root, conn, keep=keep, resolver=resolver)
        seeds: dict[str, str] = {}
        partial = False
        for reference in references:
            check_deadline()
            candidates = view.indexed_units(reference)
            if len(candidates) != 1:
                partial = True
                continue
            seeds[candidates[0]["node_key"]] = reference
        admitted: dict[str, dict[str, Any]] = {}
        per_seed: dict[str, int] = {}

        def incoming(edge: dict[str, Any]) -> bool:
            nonlocal partial
            check_deadline()
            target = seeds.get(edge.get("dst_key"))
            family = (edge.get("metadata") or {}).get("relation_family")
            if (target is None or family not in CORRECTIVE_FAMILIES
                    or not relation_registry.traversable_resolution_status(edge.get("registry_status"))):
                return False
            node = view.node(str(edge.get("src_key") or ""))
            metadata = (node or {}).get("metadata") or {}
            reference = metadata.get("unit_ref")
            if not reference or reference == target or reference in admitted:
                return False
            candidates = view.indexed_units(reference)
            if len(candidates) != 1 or candidates[0]["node_key"] != node["node_key"]:
                return False
            parent = view.parent(node["path"])
            resolved = parent.structure.resolve_unit(reference) if parent is not None else None
            if resolved is None or resolved.status != "found" or resolved.index is None:
                return False
            unit = parent.structure.units[resolved.index]
            candidate = {
                "unit_ref": reference, "path": node["path"],
                "fingerprint": unit.fingerprint,
                "corrects": target, "relation": edge["relation_type"], "family": family,
            }
            if not accept(candidate):
                return False
            check_deadline()
            if per_seed.get(target, 0) >= MAX_COUNTERPARTS:
                partial = True
                return False
            admitted[reference] = candidate
            per_seed[target] = per_seed.get(target, 0) + 1
            return True

        _edges, overflow = view.neighbor_edges(
            set(seeds), limit=MAX_COUNTERPARTS * len(references), accept=incoming,
        )
        check_deadline()
        if freshness.external_pending(root) or not freshness.recall_checkpoint_is_current(root, "vault", checkpoint):
            return {"status": "unavailable", "units": []}
        return {"status": "partial" if partial or overflow or view.unavailable else "available",
                "units": list(admitted.values())}
    except TimeoutError:
        return {"status": "budget_exhausted", "units": []}
    except sqlite3.Error:
        return {"status": "budget_exhausted" if time.monotonic() >= deadline else "unavailable", "units": []}
    finally:
        if conn is not None:
            try:
                conn.set_progress_handler(None, 0)
            except sqlite3.Error:
                # Publication may already have cycled this tracked reader.
                pass
            conn.close()
