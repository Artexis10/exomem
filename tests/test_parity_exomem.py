"""Public graph evidence must identify the typed edge, not nearby text."""
import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    sys.modules.pop("parity_exomem", None)
    return importlib.import_module("parity_exomem")


def payload(adapter):
    return {"available": True, "truncation": [], "nodes": [
        {"node_key": "a", "kind": "file", "path": adapter.PREFIX + "active-tracker.md"},
        {"node_key": "b", "kind": "file", "path": adapter.PREFIX + "background.md"},
    ], "edges": [{"src_key": "a", "dst_key": "b", "relation_type": "supports",
                  "source_path": adapter.PREFIX + "active-tracker.md"}]}


def test_public_graph_requires_exact_typed_directed_edge(adapter):
    result = payload(adapter)
    assert adapter.verify_graph(result, "background.md", "archived-runbook.md")
    result["edges"][0]["relation_type"] = "links_to"
    assert not adapter.verify_graph(result, "background.md", "archived-runbook.md")


def test_truncated_graph_cannot_prove_old_edge_absence(adapter):
    result = payload(adapter)
    result["truncation"] = ["max_edges"]
    assert not adapter.verify_graph(result, "background.md", "archived-runbook.md")


def test_graph_with_old_edge_fails(adapter):
    result = payload(adapter)
    result["nodes"].append({"node_key": "old", "kind": "file", "path": adapter.PREFIX + "archived-runbook.md"})
    result["edges"].append({**result["edges"][0], "dst_key": "old"})
    assert not adapter.verify_graph(result, "background.md", "archived-runbook.md")


def test_search_rejects_marker_prefix_lookalike(adapter):
    result = {"hits": [{"path": adapter.TRACKER, "snippet": "paritymarker00001suffix"}]}
    assert not adapter.verify_search(result, "paritymarker00001")


def test_read_proof_binds_the_requested_file(adapter):
    assert adapter.verify_read({"path": adapter.TRACKER, "body": "accepted\n"}, "accepted")
    assert not adapter.verify_read({"path": "wrong.md", "body": "accepted"}, "accepted")
