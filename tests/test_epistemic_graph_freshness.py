"""Epistemic graph freshness, audit, and reconcile integration."""

from __future__ import annotations

import multiprocessing
import os
import threading
from pathlib import Path

import pytest

from exomem import audit, epistemic_graph, reconcile, semantic_index
from exomem import find as find_module
from exomem.cli_ops import OpError
from exomem.mutation_lock import VaultMutationCoordinator

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


def _spawn_graph_mutation(
    vault_root: str,
    operation: str,
    rel_path: str,
    attempting,
    completed,
) -> None:
    os.environ["EXOMEM_DISABLE_EMBEDDINGS"] = "1"
    attempting.set()
    index = epistemic_graph.EpistemicGraphIndex(Path(vault_root))
    if operation == "refresh":
        index.refresh_paths([Path(vault_root) / rel_path])
    else:
        index.delete_paths([rel_path])
    completed.set()


def test_graph_mutation_lock_is_shared_and_rooted_inside_vault_kb(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _seed(vault)

    first = epistemic_graph.EpistemicGraphIndex(vault)
    second = epistemic_graph.EpistemicGraphIndex(vault / ".")

    expected_root = (vault / "Knowledge Base" / ".graph-coordination").resolve()
    assert first._mutation_coordinator.state_root == expected_root
    assert first._mutation_coordinator.lock_path == second._mutation_coordinator.lock_path
    assert first._mutation_coordinator.timeout_seconds == 30.0


def test_graph_mutation_lock_unavailable_preserves_current_graph(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    before_nodes = index.nodes()
    before_edges = index.edges()
    unusable_state_root = tmp_path / "not-a-directory"
    unusable_state_root.write_text("occupied", encoding="utf-8")
    index._mutation_coordinator = VaultMutationCoordinator(
        unusable_state_root,
        vault,
        timeout_seconds=0.05,
    )

    with pytest.raises(OpError) as raised:
        index.rebuild_all()

    assert raised.value.code == "MUTATION_LOCK_UNAVAILABLE"
    assert index.available() is True
    assert index.nodes() == before_nodes
    assert index.edges() == before_edges


def test_graph_dispatch_wrappers_propagate_structured_lock_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / A
    lock_error = OpError("MUTATION_BUSY", "graph mutation is busy")
    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "refresh_paths",
        lambda *_args: (_ for _ in ()).throw(lock_error),
    )

    with pytest.raises(OpError, match="MUTATION_BUSY"):
        epistemic_graph.upsert_after_write(tmp_path, [target])

    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "delete_paths",
        lambda *_args: (_ for _ in ()).throw(lock_error),
    )
    with pytest.raises(OpError, match="MUTATION_BUSY"):
        epistemic_graph.delete_after_remove(tmp_path, [A])


@pytest.mark.parametrize("operation", ["refresh", "delete"])
def test_spawned_mutator_waits_for_full_rebuild_mutation_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    context = multiprocessing.get_context("spawn")
    vault = tmp_path / "vault"
    _seed(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    real_index_path = index._index_path
    rebuild_entered = threading.Event()
    release_rebuild = threading.Event()
    rebuild_errors: list[Exception] = []
    blocked_once = False

    def blocking_index_path(*args, **kwargs):
        nonlocal blocked_once
        if not blocked_once:
            blocked_once = True
            rebuild_entered.set()
            if not release_rebuild.wait(8.0):
                raise RuntimeError("test rebuild release signal was not received")
        return real_index_path(*args, **kwargs)

    def rebuild() -> None:
        try:
            index.rebuild_all()
        except Exception as exc:  # noqa: BLE001 - asserted in parent thread
            rebuild_errors.append(exc)

    monkeypatch.setattr(index, "_index_path", blocking_index_path)
    rebuild_thread = threading.Thread(target=rebuild)
    attempting = context.Event()
    completed = context.Event()
    child = context.Process(
        target=_spawn_graph_mutation,
        args=(str(vault), operation, A, attempting, completed),
    )

    rebuild_thread.start()
    assert rebuild_entered.wait(3.0)
    child.start()
    try:
        assert attempting.wait(5.0)
        assert not completed.wait(0.5)
    finally:
        release_rebuild.set()
        rebuild_thread.join(timeout=8.0)
        child.join(timeout=8.0)
        if child.is_alive():
            child.terminate()
            child.join(timeout=3.0)

    assert not rebuild_thread.is_alive()
    assert rebuild_errors == []
    assert completed.is_set()
    assert child.exitcode == 0


def test_full_rebuild_reuses_one_detached_resolver_without_shared_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    find_module._RESOLVER_CACHE.clear()
    real_snapshot = find_module.writer_resolver_snapshot
    acquisitions: list[Path] = []

    def acquire(root: Path, **kwargs):
        acquisitions.append(root)
        return real_snapshot(root, **kwargs)

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


def test_full_rebuild_retries_when_target_is_renamed_after_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    source = _write(
        vault,
        A,
        "# Source\n\nLinks to [[Knowledge Base/Notes/Insights/late-target]].\n",
    )
    target_rel = "Knowledge Base/Notes/Insights/late-target.md"
    target = vault / target_rel
    staged_target = _write(
        vault,
        "Knowledge Base/Notes/Insights/staged-target.md",
        "# Late target\n",
    )
    real_snapshot = find_module.writer_resolver_snapshot
    acquisitions = 0

    def acquire(root: Path, **kwargs):
        nonlocal acquisitions
        snapshot = real_snapshot(root, **kwargs)
        acquisitions += 1
        if acquisitions == 1:
            staged_target.rename(target)
        return snapshot

    monkeypatch.setattr(find_module, "writer_resolver_snapshot", acquire)

    index = epistemic_graph.EpistemicGraphIndex(vault)
    report = index.rebuild_all()

    assert source.exists()
    assert acquisitions == 2
    assert report["indexed_files"] == 2
    assert any(
        edge["relation_type"] == "links_to"
        and edge["dst_key"] == epistemic_graph._file_key(target_rel)
        for edge in index.edges(source_path=A)
    )


def test_full_rebuild_twice_moving_vault_is_marked_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    real_snapshot = find_module.writer_resolver_snapshot
    acquisitions = 0

    def acquire(root: Path, **kwargs):
        nonlocal acquisitions
        snapshot = real_snapshot(root, **kwargs)
        acquisitions += 1
        _write(
            vault,
            f"Knowledge Base/Notes/Insights/churn-{acquisitions}.md",
            f"# Churn {acquisitions}\n",
        )
        return snapshot

    monkeypatch.setattr(find_module, "writer_resolver_snapshot", acquire)
    index = epistemic_graph.EpistemicGraphIndex(vault)

    with pytest.raises(RuntimeError, match="did not stabilize"):
        index.rebuild_all()

    assert acquisitions == 2
    assert index.available() is False


def test_full_rebuild_retry_acquisition_failure_marks_graph_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    real_snapshot = find_module.writer_resolver_snapshot
    acquisitions = 0

    def acquire(root: Path, **kwargs):
        nonlocal acquisitions
        acquisitions += 1
        if acquisitions == 2:
            raise RuntimeError("retry resolver failed")
        snapshot = real_snapshot(root, **kwargs)
        _write(vault, "Knowledge Base/Notes/Insights/moved.md", "# Moved\n")
        return snapshot

    monkeypatch.setattr(find_module, "writer_resolver_snapshot", acquire)

    with pytest.raises(RuntimeError, match="retry resolver failed"):
        index.rebuild_all()

    assert acquisitions == 2
    assert index.available() is False


def test_full_rebuild_retry_freshness_failure_marks_graph_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    real_freshness = epistemic_graph._disk_vault_freshness
    real_snapshot = find_module.writer_resolver_snapshot
    freshness_checks = 0

    def freshness(root: Path):
        nonlocal freshness_checks
        freshness_checks += 1
        if freshness_checks == 3:
            raise RuntimeError("retry freshness failed")
        return real_freshness(root)

    def acquire(root: Path, **kwargs):
        snapshot = real_snapshot(root, **kwargs)
        _write(vault, "Knowledge Base/Notes/Insights/moved.md", "# Moved\n")
        return snapshot

    monkeypatch.setattr(epistemic_graph, "_disk_vault_freshness", freshness)
    monkeypatch.setattr(find_module, "writer_resolver_snapshot", acquire)

    with pytest.raises(RuntimeError, match="retry freshness failed"):
        index.rebuild_all()

    assert freshness_checks == 3
    assert index.available() is False


def test_full_rebuild_pass_failure_marks_partial_graph_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()

    def fail_index(*_args, **_kwargs):
        raise RuntimeError("index pass failed")

    monkeypatch.setattr(index, "_index_path", fail_index)

    with pytest.raises(RuntimeError, match="index pass failed"):
        index.rebuild_all()

    assert index.available() is False


def test_full_rebuild_keeps_schema_marker_absent_until_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    real_index_path = index._index_path
    indexed = 0

    def index_path(*args, **kwargs):
        nonlocal indexed
        assert index.available() is False
        indexed += 1
        return real_index_path(*args, **kwargs)

    monkeypatch.setattr(index, "_index_path", index_path)

    index.rebuild_all()

    assert indexed == 2
    assert index.available() is True


def test_refresh_admitted_before_failed_rebuild_cannot_restore_availability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    source, _target = _seed(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    real_snapshot = find_module.writer_resolver_snapshot
    real_index_path = index._index_path
    rebuild_active = False
    overlap_triggered = False

    def index_path(*args, **kwargs):
        if rebuild_active:
            raise RuntimeError("overlapping rebuild failed")
        return real_index_path(*args, **kwargs)

    def acquire(root: Path, **kwargs):
        nonlocal rebuild_active, overlap_triggered
        snapshot = real_snapshot(root, **kwargs)
        if "freshness_key" not in kwargs and not overlap_triggered:
            overlap_triggered = True
            rebuild_active = True
            try:
                with pytest.raises(RuntimeError, match="overlapping rebuild failed"):
                    index.rebuild_all()
            finally:
                rebuild_active = False
            assert index.available() is False
        return snapshot

    monkeypatch.setattr(index, "_index_path", index_path)
    monkeypatch.setattr(find_module, "writer_resolver_snapshot", acquire)

    report = index.refresh_paths([source])

    assert overlap_triggered is True
    assert report["indexed_files"] == 1
    assert index.available() is False


def test_refresh_missing_sidecar_routes_to_full_rebuild(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    source, _target = _seed(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)

    report = index.refresh_paths([source])

    assert report["indexed_files"] == 2
    assert {node["path"] for node in index.nodes()} == {A, B}
    assert index.available() is True


def test_full_rebuild_first_post_pass_freshness_failure_marks_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    real_freshness = epistemic_graph._disk_vault_freshness
    freshness_checks = 0

    def freshness(root: Path):
        nonlocal freshness_checks
        freshness_checks += 1
        if freshness_checks == 2:
            raise RuntimeError("post-pass freshness failed")
        return real_freshness(root)

    monkeypatch.setattr(epistemic_graph, "_disk_vault_freshness", freshness)

    with pytest.raises(RuntimeError, match="post-pass freshness failed"):
        index.rebuild_all()

    assert freshness_checks == 2
    assert index.available() is False


def test_full_rebuild_snapshot_isolated_from_shared_cache_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    find_module._RESOLVER_CACHE.clear()
    shared = find_module.shared_resolver(vault)
    real_snapshot = find_module.writer_resolver_snapshot

    def acquire(root: Path, **kwargs):
        snapshot = real_snapshot(root, **kwargs)
        assert snapshot is not shared
        shared._remove_entry(B.removesuffix(".md"))
        return snapshot

    monkeypatch.setattr(find_module, "writer_resolver_snapshot", acquire)
    try:
        index = epistemic_graph.EpistemicGraphIndex(vault)
        index.rebuild_all()

        assert any(
            edge["relation_type"] == "links_to"
            and edge["dst_key"] == epistemic_graph._file_key(B)
            for edge in index.edges(source_path=A)
        )
    finally:
        find_module._RESOLVER_CACHE.clear()


def test_refresh_batch_reuses_one_snapshot_and_separate_calls_reacquire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    a, b = _seed(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    real_snapshot = find_module.writer_resolver_snapshot
    acquisitions: list[Path] = []

    def acquire(root: Path, **kwargs):
        acquisitions.append(root)
        return real_snapshot(root, **kwargs)

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
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("resolver failed")
        ),
    )

    with pytest.raises(RuntimeError, match="resolver failed"):
        index.rebuild_all()

    assert index.available() is True
    assert index.nodes() == before_nodes
    assert index.edges() == before_edges


def test_rebuild_initial_freshness_failure_preserves_committed_graph_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    before_nodes = index.nodes()
    before_edges = index.edges()
    monkeypatch.setattr(
        epistemic_graph,
        "_disk_vault_freshness",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("freshness failed")),
    )

    with pytest.raises(RuntimeError, match="freshness failed"):
        index.rebuild_all()

    assert index.available() is True
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
