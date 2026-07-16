"""Epistemic graph freshness, audit, and reconcile integration."""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import audit, epistemic_graph, reconcile, semantic_index
from exomem import find as find_module

A = "Knowledge Base/Notes/Insights/a.md"
B = "Knowledge Base/Notes/Insights/b.md"


def _write(vault: Path, rel: str, body: str) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _seed(vault: Path) -> tuple[Path, Path]:
    a = _write(
        vault,
        A,
        """\
---
type: insight
status: active
---
# A

## Claim

A claim links to [[Knowledge Base/Notes/Insights/b]].
""",
    )
    b = _write(
        vault,
        B,
        """\
---
type: insight
status: active
---
# B

## Claim

B claim.
""",
    )
    return a, b


def test_full_rebuild_reuses_one_detached_resolver_without_shared_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    find_module._RESOLVER_CACHE.clear()
    real_snapshot = find_module.writer_resolver_snapshot
    acquisitions: list[Path] = []

    def acquire(root: Path):
        acquisitions.append(root)
        return real_snapshot(root)

    monkeypatch.setattr(find_module, "writer_resolver_snapshot", acquire)
    monkeypatch.setattr(
        find_module,
        "shared_resolver",
        lambda *_args: pytest.fail("graph maintenance used the shared resolver"),
    )

    report = epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()

    assert report["indexed_files"] == 2
    assert acquisitions == [vault]
    assert vault not in find_module._RESOLVER_CACHE


def test_refresh_batch_reuses_one_snapshot_and_separate_calls_reacquire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    a, b = _seed(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    real_snapshot = find_module.writer_resolver_snapshot
    acquisitions: list[Path] = []

    def acquire(root: Path):
        acquisitions.append(root)
        return real_snapshot(root)

    monkeypatch.setattr(find_module, "writer_resolver_snapshot", acquire)
    monkeypatch.setattr(
        find_module,
        "shared_resolver",
        lambda *_args: pytest.fail("graph maintenance used the shared resolver"),
    )

    index.refresh_paths([a, b])
    assert acquisitions == [vault]

    index.refresh_paths([a])
    assert acquisitions == [vault, vault]


def test_rebuild_resolver_failure_preserves_committed_graph_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    before_nodes = index.nodes()
    before_edges = index.edges()
    monkeypatch.setattr(
        find_module,
        "writer_resolver_snapshot",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("resolver failed")),
    )

    with pytest.raises(RuntimeError, match="resolver failed"):
        index.rebuild_all()

    assert index.nodes() == before_nodes
    assert index.edges() == before_edges


def test_explicit_detached_resolver_matches_direct_fallback_for_ambiguous_links(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    source, _target = _seed(vault)
    _write(vault, "Knowledge Base/Notes/one/collision.md", "# First collision\n")
    _write(vault, "Knowledge Base/Notes/two/collision.md", "# Second collision\n")
    source.write_text(
        source.read_text(encoding="utf-8") + "\n- supports: [[collision]]\n",
        encoding="utf-8",
    )
    page = find_module._parse_page(source, source.stat().st_mtime, vault)
    assert page is not None
    state = semantic_index.build_parent_index_state(vault, source)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    kwargs = {
        "registry": index.registry,
        "source_hash": epistemic_graph.vault_module.content_hash(
            source.read_text(encoding="utf-8")
        ),
        "parent_state": state,
    }

    fallback = epistemic_graph._edges_for_page(
        vault, page, state.document, **kwargs
    )
    explicit = epistemic_graph._edges_for_page(
        vault,
        page,
        state.document,
        resolver=find_module.writer_resolver_snapshot(vault),
        **kwargs,
    )

    assert [edge.as_dict() for edge in explicit] == [
        edge.as_dict() for edge in fallback
    ]


def test_single_file_edit_refreshes_affected_graph_rows(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    a, b = _seed(vault)
    idx = epistemic_graph.EpistemicGraphIndex(vault)
    idx.rebuild_all()
    b_before = next(n for n in idx.nodes(path=B) if n["kind"] == "file")["source_hash"]

    a.write_text(
        a.read_text(encoding="utf-8").replace("A claim", "A changed claim"),
        encoding="utf-8",
    )
    report = idx.refresh_paths([a])

    assert report["indexed_files"] == 1
    a_after = next(n for n in idx.nodes(path=A) if n["kind"] == "file")
    b_after = next(n for n in idx.nodes(path=B) if n["kind"] == "file")
    assert a_after["source_hash"] == epistemic_graph.vault_module.content_hash(
        a.read_text(encoding="utf-8")
    )
    assert b_after["source_hash"] == b_before


def test_incremental_graph_update_matches_full_rebuild(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    a, _b = _seed(vault)
    idx = epistemic_graph.EpistemicGraphIndex(vault)
    idx.rebuild_all()

    a.write_text(
        a.read_text(encoding="utf-8") + "\n## Decision\n\nKeep it derived.\n",
        encoding="utf-8",
    )
    idx.refresh_paths([a])
    incremental = epistemic_graph.graph_context(vault, path=A, depth=1)

    epistemic_graph.sidecar_path(vault).unlink()
    idx = epistemic_graph.EpistemicGraphIndex(vault)
    idx.rebuild_all()
    rebuilt = epistemic_graph.graph_context(vault, path=A, depth=1)

    assert incremental == rebuilt


def test_graph_drift_is_audited_and_reconciled_without_markdown_mutation(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    a, _b = _seed(vault)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    changed = a.read_text(encoding="utf-8").replace("A claim", "Externally edited claim")
    a.write_text(changed, encoding="utf-8")

    report = audit.audit(vault, categories=["graph_drift"])
    assert report.findings
    assert report.findings[0].category == "graph_drift"

    reconciled = reconcile.reconcile(vault)

    assert a.read_text(encoding="utf-8") == changed
    assert reconciled.graph_status == "refreshed"
    assert all(f["category"] != "graph_drift" for f in reconciled.remaining_drift)


def test_disabled_graph_indexing_makes_drift_check_noop(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_INDEX", "1")

    report = audit.audit(vault, categories=["graph_drift"])

    assert report.findings == []


def test_relation_edges_follow_incremental_edit_move_and_delete(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    source, _target = _seed(vault)
    source.write_text(
        source.read_text(encoding="utf-8")
        + "\n- supports: [[Knowledge Base/Notes/Insights/b]]\n",
        encoding="utf-8",
    )
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    assert any(edge["relation_type"] == "supports" for edge in index.edges(source_path=A))

    source.write_text(
        source.read_text(encoding="utf-8").replace("supports:", "contradicts:"),
        encoding="utf-8",
    )
    index.refresh_paths([source])
    assert any(edge["relation_type"] == "contradicts" for edge in index.edges(source_path=A))
    assert not any(edge["relation_type"] == "supports" for edge in index.edges(source_path=A))

    moved_rel = "Knowledge Base/Notes/Insights/moved-a.md"
    moved = vault / moved_rel
    source.rename(moved)
    index.delete_paths([A])
    index.refresh_paths([moved])
    assert index.nodes(path=A) == []
    assert any(
        edge["relation_type"] == "contradicts"
        for edge in index.edges(source_path=moved_rel)
    )

    moved.unlink()
    index.delete_paths([moved_rel])
    assert index.nodes(path=moved_rel) == []
    assert index.edges(source_path=moved_rel) == []


def test_target_refresh_preserves_inbound_relation_as_placeholder(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    source, target = _seed(vault)
    source.write_text(
        source.read_text(encoding="utf-8")
        + "\n- supports: [[Knowledge Base/Notes/Insights/b]]\n",
        encoding="utf-8",
    )
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    before = next(
        edge
        for edge in index.edges(source_path=A)
        if edge["relation_type"] == "supports"
    )
    target.write_text(target.read_text(encoding="utf-8") + "\nUpdated.\n", encoding="utf-8")
    index.refresh_paths([target])
    assert before in index.edges(source_path=A)


def test_incremental_write_after_registry_change_forces_full_reresolution(tmp_path: Path) -> None:
    import yaml

    vault = tmp_path / "vault"
    source, target = _seed(vault)
    registry_path = vault / "Knowledge Base" / "_Schema" / "relation-registry.yaml"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    proposal = {"schema_version": 1, "extensions": {"science.replicates": {
        "parent": "supports", "description": "Reports independent reproduction",
        "aliases": ["mirrors"],
    }}}
    registry_path.write_text(yaml.safe_dump(proposal), encoding="utf-8")
    source.write_text(
        source.read_text(encoding="utf-8")
        + "\n- mirrors: [[Knowledge Base/Notes/Insights/b]]\n",
        encoding="utf-8",
    )
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    proposal["extensions"]["science.replicates"]["aliases"] = ["reproduces"]
    registry_path.write_text(yaml.safe_dump(proposal), encoding="utf-8")
    target.write_text(target.read_text(encoding="utf-8") + "\nChanged.\n", encoding="utf-8")

    report = epistemic_graph.EpistemicGraphIndex(vault).refresh_paths([target])

    assert report["indexed_files"] == 2
    changed = next(
        edge for edge in epistemic_graph.EpistemicGraphIndex(vault).edges(source_path=A)
        if edge["raw_relation"] == "mirrors"
    )
    assert changed["registry_status"] == "unregistered"
