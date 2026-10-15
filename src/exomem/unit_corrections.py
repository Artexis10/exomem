"""Bounded, admitted incoming context for an exact served semantic unit."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import epistemic_graph, request_budget
from .governance import egress

# These are closed relation-schema families whose authored facts inform a claim.
CORRECTIVE_FAMILIES = frozenset(
    {"contradiction", "refinement", "support", "supersession", "resolution", "answer"}
)


def reader_view(root: Path) -> Callable[[str], bool] | None:
    return egress.restricted_release_filter(root)


def counterparts(
    root: Path,
    unit_ref: str,
    *,
    keep: Callable[[str], bool] | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Resolve incoming units outside the initial recall window, within graph caps."""
    if (deadline is not None and time.monotonic() >= deadline) or not request_budget.can_afford(
        request_budget.ACTIVATION_STAGE_RESERVE_SECONDS
    ):
        return {"status": "budget_exhausted", "units": []}
    context = epistemic_graph.graph_context(
        root,
        unit_ref=unit_ref,
        depth=1,
        max_nodes=12,
        max_edges=12,
        keep=keep,
        include_lag=False,
    )
    if not context.get("available") or context.get("unit_status") != "found":
        return {"status": "unavailable", "units": []}
    seeds = {
        str(seed.get("node_key")) if isinstance(seed, dict) else str(seed)
        for seed in context.get("seeds", [])
    }
    nodes = {node["node_key"]: node for node in context.get("nodes", [])}
    units = []
    seen = set()
    for edge in context.get("edges", []):
        family = (edge.get("metadata") or {}).get("relation_family")
        if edge.get("dst_key") not in seeds or family not in CORRECTIVE_FAMILIES:
            continue
        node = nodes.get(edge.get("src_key"))
        metadata = (node or {}).get("metadata") or {}
        reference = metadata.get("unit_ref")
        if not reference or reference == unit_ref or reference in seen:
            continue
        seen.add(reference)
        units.append(
            {
                "unit_ref": reference,
                "path": node["path"],
                "relation": edge["relation_type"],
                "family": family,
                "text": str(node.get("text") or "")[:800],
                "source_hash": node.get("source_hash"),
                "context": str(metadata.get("context") or "")[:256],
                **({"verdict": metadata["verdict"]} if metadata.get("verdict") else {}),
                **({"check_by": metadata["check_by"]} if metadata.get("check_by") else {}),
            }
        )
    clipped = len(units) > 4 or any(
        len(str(nodes.get(edge.get("src_key"), {}).get("text") or "")) > 800
        for edge in context.get("edges", [])
        if edge.get("dst_key") in seeds
    )
    return {
        "status": "partial"
        if clipped or context.get("truncation") or context.get("warnings")
        else "available",
        "units": units[:4],
    }
