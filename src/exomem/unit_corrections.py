"""Bounded exact relation discovery over an already-current graph view."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
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
    outgoing_sources: Mapping[str, Sequence[tuple[str, Sequence[Mapping[str, Any]]]]] | None = None,
    accept_outgoing: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """Discover references once; the caller admits exact units before the cap.

    No cold graph proof or resolver build belongs to optional enrichment. If
    publication moves during hydration, discard every tentative reference.
    """
    def result(status: str, units=(), outgoing=()) -> dict[str, Any]:
        return {"status": status, "units": list(units),
                **({"outgoing": list(outgoing)} if accept_outgoing is not None else {})}

    references = tuple(dict.fromkeys(
        ref for ref in unit_refs
        if "#" in ref and memory_refs.parse_memory_ref(ref.partition("#")[0]) is not None
    ))
    if not references:
        return result("available")
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
            return result("unavailable")
        resolver = find.recall_resolver_snapshot_at_checkpoint(root, checkpoint)
        if resolver is None:
            return result("unavailable")
        conn = epistemic_graph.EpistemicGraphIndex(root)._open_read_snapshot(live_only=True)
        if conn is None:
            return result("unavailable")
        # The shared SQL row cap bounds volume; the progress handler bounds work.
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        check_deadline()
        view = epistemic_graph.GraphView(root, conn, keep=keep, resolver=resolver)
        seeds: dict[str, str] = {}
        outgoing_seeds: dict[str, tuple[str, str, frozenset[str]]] = {}
        partial = False
        for reference in references:
            check_deadline()
            candidates = view.indexed_units(reference)
            if len(candidates) != 1:
                partial = True
                continue
            seeds[candidates[0]["node_key"]] = reference
            versions = (outgoing_sources or {}).get(reference)
            if versions and accept_outgoing is not None:
                parent = view.parent(candidates[0]["path"])
                resolved = parent.structure.resolve_unit(reference) if parent is not None else None
                unit = (parent.structure.units[resolved.index]
                        if resolved is not None and resolved.status == "found" and resolved.index is not None
                        else None)
                relations = next((relations for source_hash, relations in versions
                                  if unit is not None and source_hash and unit.source_hash == source_hash), None)
                if relations is None:
                    partial = True
                    continue
                outgoing_seeds[candidates[0]["node_key"]] = (
                    reference, unit.source_hash, frozenset(str(row.get("raw") or "") for row in relations),
                )
        admitted: dict[str, dict[str, Any]] = {}
        per_seed: dict[str, int] = {}
        outgoing: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        outgoing_counts: dict[str, int] = {}

        def counterpart(node_key: str) -> dict[str, Any] | None:
            node = view.node(node_key)
            reference = ((node or {}).get("metadata") or {}).get("unit_ref")
            if not reference:
                return None
            candidates = view.indexed_units(reference)
            if len(candidates) != 1 or candidates[0]["node_key"] != node["node_key"]:
                return None
            parent = view.parent(node["path"])
            resolved = parent.structure.resolve_unit(reference) if parent is not None else None
            if resolved is None or resolved.status != "found" or resolved.index is None:
                return None
            return {"unit_ref": reference, "path": node["path"],
                    "fingerprint": parent.structure.units[resolved.index].fingerprint}

        def inspect_outgoing(edge: dict[str, Any]) -> None:
            nonlocal partial
            check_deadline()
            source = outgoing_seeds.get(edge.get("src_key"))
            if (source is None or accept_outgoing is None
                    or not relation_registry.traversable_resolution_status(edge.get("registry_status"))):
                return
            source_ref, source_hash, authored = source
            metadata = edge.get("metadata") or {}
            if edge.get("origin") != "semantic_relation" or metadata.get("raw") not in authored:
                return
            candidate = counterpart(str(edge.get("dst_key") or ""))
            if candidate is None:
                # The graph resolves names against visible candidates, so its
                # closed ambiguous outcome is incomplete for every reader.
                # Other unresolved targets stay silent for restricted readers:
                # withholding a page must look like its absence.
                if metadata.get("fragment_resolution") not in {None, "unit"} and (
                    keep is None or metadata.get("target_resolution") != "unresolved"
                    or metadata.get("fragment_resolution") == "ambiguous"
                ):
                    partial = True
                return
            reference = candidate["unit_ref"]
            if reference == source_ref:
                return
            key = (source_ref, source_hash, edge["relation_type"], reference)
            if key in outgoing:
                return
            candidate.update(source_ref=source_ref, source_hash=source_hash,
                             relation=edge["relation_type"])
            if not accept_outgoing(candidate):
                return
            check_deadline()
            if outgoing_counts.get(source_ref, 0) >= MAX_COUNTERPARTS:
                partial = True
                return
            outgoing[key] = candidate
            outgoing_counts[source_ref] = outgoing_counts.get(source_ref, 0) + 1

        def inspect(edge: dict[str, Any]) -> bool:
            inspect_outgoing(edge)
            # Outgoing pointers spend no incoming correction slot. Both share this
            # existing row scan and deadline; optional pointers never widen either.
            return incoming(edge)

        def incoming(edge: dict[str, Any]) -> bool:
            nonlocal partial
            check_deadline()
            target = seeds.get(edge.get("dst_key"))
            family = (edge.get("metadata") or {}).get("relation_family")
            if (target is None or family not in CORRECTIVE_FAMILIES
                    or not relation_registry.traversable_resolution_status(edge.get("registry_status"))):
                return False
            candidate = counterpart(str(edge.get("src_key") or ""))
            if candidate is None:
                return False
            reference = candidate["unit_ref"]
            if reference == target or reference in admitted:
                return False
            candidate.update(corrects=target, relation=edge["relation_type"], family=family)
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
            set(seeds), limit=MAX_COUNTERPARTS * len(references), accept=inspect,
        )
        check_deadline()
        if freshness.external_pending(root) or not freshness.recall_checkpoint_is_current(root, "vault", checkpoint):
            return result("unavailable")
        return result("partial" if partial or overflow or view.unavailable else "available",
                      admitted.values(), outgoing.values())
    except TimeoutError:
        return result("budget_exhausted")
    except sqlite3.Error:
        return result("budget_exhausted" if time.monotonic() >= deadline else "unavailable")
    finally:
        if conn is not None:
            try:
                conn.set_progress_handler(None, 0)
            except sqlite3.Error:
                # Publication may already have cycled this tracked reader.
                pass
            conn.close()
