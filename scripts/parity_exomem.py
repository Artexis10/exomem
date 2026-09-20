"""Exomem's public operations and read-only proofs for the common workload."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import durable_closure_common as common
import mixed_load_graph
import parity_fixture

PREFIX = "Knowledge Base/Reference/"
TRACKER = PREFIX + parity_fixture.TRACKER


def prepare(state: Path, vault: Path, pages: int, *, source: Path) -> tuple[dict, dict, Path]:
    env = common.exomem_environment(state, vault)
    env.update(PYTHONPATH=str(source / "src"), EXOMEM_DISABLE_CLIP="1")
    shutil.copytree(source / "src/exomem/_scaffold/_Schema", vault / "Knowledge Base/_Schema")
    corpus = vault / PREFIX
    return env, parity_fixture.materialize(corpus, pages), corpus


def edit_arguments(old: str, new: str) -> tuple[str, dict]:
    return "edit_memory", {"path": TRACKER, "why": "advance disposable parity fixture",
        "operation": {"kind": "replace_string", "old_string": old, "new_string": new, "replace_all": False}}


def append_arguments(content: str) -> tuple[str, dict]:
    return "edit_memory", {"path": TRACKER, "why": "append independent parity token",
        "operation": {"kind": "edit_section", "heading": "Concurrent", "new_string": content,
                      "section_position": "append"}}


def read_arguments() -> tuple[str, dict]:
    return "read_memory", {"path": TRACKER}


def search_arguments(marker: str) -> tuple[str, dict]:
    return "ask_memory", {"query": marker, "mode": "keyword", "detail": "full", "graph": False,
                          "rerank": False, "limit": 5}


def graph_arguments(target: str = parity_fixture.OLD_TARGET) -> tuple[str, dict]:
    return "connect_memory", {"operation": "graph-context", "path": PREFIX + target, "depth": 1, "relation_types": ["supports"],
                             "max_nodes": 40, "max_edges": 80, "traversal_profile": "all"}


def verify_read(payload: dict, expected: str) -> bool:
    return (common.result_classification(payload) == "ok" and payload.get("path") == TRACKER
            and parity_fixture.read_body_equals(payload, expected))


def verify_search(payload: dict, marker: str) -> bool:
    for key in ("hits", "results", "items", "result"):
        rows = payload.get(key)
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict) or row.get("path") != TRACKER:
                    continue
                if any(isinstance(row.get(field), str) and re.search(r"(?<!\w)" + re.escape(marker) + r"(?!\w)", row[field])
                       for field in ("snippet", "excerpt", "content", "body", "text")):
                    return True
    return False


def verify_graph(payload: dict, target: str, absent_target: str | None = None) -> bool:
    payload = payload.get("graph", payload)
    if not isinstance(payload, dict):
        return False
    if payload.get("available") is not True or payload.get("truncation"):
        return False
    nodes = {node.get("node_key"): node for node in payload.get("nodes", []) if isinstance(node, dict)}
    targets = set()
    for edge in payload.get("edges", []):
        if not isinstance(edge, dict) or edge.get("relation_type") != "supports" or edge.get("source_path") != TRACKER:
            continue
        source, destination = nodes.get(edge.get("src_key"), {}), nodes.get(edge.get("dst_key"), {})
        if source.get("path") == TRACKER and destination.get("kind") == "file":
            targets.add(destination.get("path"))
    return PREFIX + target in targets and (absent_target is None or PREFIX + absent_target not in targets)


def inspect_index(state: Path, vault: Path, fixture: dict) -> dict:
    return common.inspect_indexed_fixture(product="exomem", state=state, vault=vault, fixture=fixture)


def inspect_relation(vault: Path, target: str, absent_target: str | None = None) -> dict:
    return mixed_load_graph.inspect_graph(
        vault, expected_link=(TRACKER, PREFIX + target), expected_relation="supports",
        absent_link=(TRACKER, PREFIX + absent_target) if absent_target else None,
    )
