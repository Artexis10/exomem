"""Epistemic graph freshness, audit, and reconcile integration."""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import threading
from pathlib import Path

import pytest

from exomem import (
    audit,
    deferred_index,
    epistemic_graph,
    freshness,
    graph_sync,
    index_sync,
    reconcile,
    semantic_index,
)
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.cli_ops import OpError
from exomem.mutation_lock import VaultMutationCoordinator

A = "Knowledge Base/Notes/Insights/a.md"
B = "Knowledge Base/Notes/Insights/b.md"


#: The wall-clock shape of every contention test in this file.
#:
#: A HOLD keeps a lock, a process, or an in-flight rebuild parked while the test
#: observes an ordering. An OBSERVATION is how long the test will wait for that
#: state to be reached. The gap between them is the entire discriminating power
#: of these tests: a hold that does not outlast its observation lets the
#: ordering pass vacuously, and an observation sized for an idle laptop fails on
#: a loaded shard while the code under test behaves perfectly.
#:
#: `join(timeout=N)` followed by `assert t.is_alive()` is the SAME negative
#: observation in join form, and it is the one shape that consumes its whole
#: window on every healthy run -- it exists to prove a competitor is still
#: parked. Widening one from 0.3s to 60s bought nothing and cost a minute a run.
#:
#: Both constants stay strictly under pytest's per-test `timeout` (pyproject
#: `[tool.pytest.ini_options]`). A valve at or above it never gets to fire: the
#: harness kills the test first and you get a thread dump where a named
#: assertion should have been. tests/test_timing_assertion_hygiene.py pins that.
#:
#: These are not latency claims. Nothing here asserts the product is fast.
_HOLD_SECONDS = 45.0
_OBSERVE_SECONDS = 15.0

def _write(vault: Path, rel: str, body: str) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8", newline="\n")
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


def _seed_live_freshness(vault: Path) -> None:
    freshness.seed(
        vault,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(vault)),
    )
    kb = vault / "Knowledge Base"
    freshness.seed(
        vault,
        "kb",
        ((str(path), freshness.stat_signature(path)) for path in find_module._walk_md(kb)),
    )


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
    from exomem.writer_lease import active_manager

    vault = tmp_path / "vault"
    _seed(vault)

    first = epistemic_graph.EpistemicGraphIndex(vault)
    second = epistemic_graph.EpistemicGraphIndex(vault / ".")

    expected_root = active_manager().config.state_dir
    assert first._mutation_coordinator.state_root == expected_root
    assert first._mutation_coordinator.lock_path == second._mutation_coordinator.lock_path
    assert (
        first._mutation_coordinator.timeout_seconds
        == active_manager()._mutation_timeout_seconds
    )


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
    unusable_state_root.write_text("occupied", encoding="utf-8", newline="\n")
    index._mutation_coordinator = VaultMutationCoordinator(
        unusable_state_root,
        vault,
        timeout_seconds=0.05,
    )

    with pytest.raises(graph_sync.GraphRebuildLockUnavailable) as raised:
        index.rebuild_all()

    assert raised.value.code == "GRAPH_SYNC_REBUILD_LOCK_UNAVAILABLE"
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


@pytest.mark.parametrize(
    ("operation", "rel_path"),
    [
        ("refresh", A),
        ("refresh", "Sources/raw.md"),
        ("delete", A),
    ],
)
def test_spawned_mutator_commits_while_full_rebuild_is_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    rel_path: str,
) -> None:
    context = multiprocessing.get_context("spawn")
    vault = tmp_path / "vault"
    _seed(vault)
    if not rel_path.startswith("Knowledge Base/"):
        _write(vault, rel_path, "# Raw\n\nVault-wide recall material.\n")
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
            if not release_rebuild.wait(_HOLD_SECONDS):
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
        args=(str(vault), operation, rel_path, attempting, completed),
    )

    rebuild_thread.start()
    assert rebuild_entered.wait(_OBSERVE_SECONDS)
    child.start()
    try:
        assert attempting.wait(_OBSERVE_SECONDS)
        # The property is that the rebuild does not *block* the mutator, not
        # that the commit is fast. A blocked mutator cannot finish at any
        # budget: the rebuild holds until `release_rebuild`, which is only set
        # in the `finally` below. So the budget just has to stay clear of the
        # 8.0s at which the held rebuild gives up -- and 0.5s was measuring
        # commit latency instead, which a loaded shared runner exceeds.
        assert completed.wait(_OBSERVE_SECONDS)
        if operation == "refresh":
            if rel_path.startswith("Knowledge Base/"):
                assert rel_path in deferred_index.list_graph_paths(vault)
            else:
                assert deferred_index.graph_full_rebuild_pending(vault) is not None
    finally:
        release_rebuild.set()
        rebuild_thread.join(timeout=_HOLD_SECONDS)
        child.join(timeout=_HOLD_SECONDS)
        if child.is_alive():
            child.terminate()
            child.join(timeout=_HOLD_SECONDS)

    assert not rebuild_thread.is_alive()
    assert rebuild_errors == []
    assert completed.is_set()
    assert child.exitcode == 0


def test_neighbor_reader_during_rebuild_sees_complete_old_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    expected = index.neighbors_for([A])
    assert expected
    writer_entered = threading.Event()
    release_rebuild = threading.Event()
    reader_results: list[list[epistemic_graph.GraphNeighbor]] = []
    reader_errors: list[Exception] = []
    writer_errors: list[Exception] = []
    real_index_path = index._index_path

    def index_path(*args, **kwargs):
        writer_entered.set()
        assert release_rebuild.wait(_HOLD_SECONDS)
        return real_index_path(*args, **kwargs)

    def read_neighbors() -> None:
        try:
            reader_results.append(index.neighbors_for([A]))
        except Exception as exc:  # noqa: BLE001 - asserted in parent thread
            reader_errors.append(exc)

    def rebuild() -> None:
        try:
            index.rebuild_all()
        except Exception as exc:  # noqa: BLE001 - asserted in parent thread
            writer_errors.append(exc)

    monkeypatch.setattr(index, "_index_path", index_path)
    reader = threading.Thread(target=read_neighbors)
    writer = threading.Thread(target=rebuild)
    try:
        writer.start()
        assert writer_entered.wait(_OBSERVE_SECONDS)
        reader.start()
        reader.join(timeout=_HOLD_SECONDS)
        release_rebuild.set()
        writer.join(timeout=_HOLD_SECONDS)
    finally:
        release_rebuild.set()
        if writer.is_alive():
            writer.join(timeout=_HOLD_SECONDS)
        if reader.is_alive():
            reader.join(timeout=_HOLD_SECONDS)

    assert not reader.is_alive()
    assert not writer.is_alive()
    assert reader_errors == []
    assert writer_errors == []
    assert reader_results == [expected]


def test_trusted_reads_after_marker_removal_never_expose_partial_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    real_index_path = index._index_path
    partial_written = threading.Event()
    release_rebuild = threading.Event()
    writer_errors: list[Exception] = []
    indexed = 0

    def pause_after_first_path(*args, **kwargs):
        nonlocal indexed
        result = real_index_path(*args, **kwargs)
        indexed += 1
        if indexed == 1:
            partial_written.set()
            if not release_rebuild.wait(_HOLD_SECONDS):
                raise RuntimeError("test rebuild release signal was not received")
        return result

    def rebuild() -> None:
        try:
            index.rebuild_all()
        except Exception as exc:  # noqa: BLE001 - asserted in parent thread
            writer_errors.append(exc)

    monkeypatch.setattr(index, "_index_path", pause_after_first_path)
    writer = threading.Thread(target=rebuild)
    writer.start()
    try:
        assert partial_written.wait(_OBSERVE_SECONDS)
        assert index.nodes()
        assert index.edges()
        assert index.neighbors_for([A])
        assert epistemic_graph.graph_context(vault, path=A)["available"] is True
    finally:
        release_rebuild.set()
        writer.join(timeout=_HOLD_SECONDS)

    assert not writer.is_alive()
    assert writer_errors == []


def test_full_rebuild_reuses_one_detached_resolver_without_shared_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    find_module._RECALL_RESOLVER_CACHE.clear()
    real_snapshot = find_module.recall_resolver_snapshot
    acquisitions: list[Path] = []

    def acquire(root: Path, **kwargs):
        acquisitions.append(root)
        return real_snapshot(root, **kwargs)

    monkeypatch.setattr(find_module, "recall_resolver_snapshot", acquire)
    monkeypatch.setattr(
        find_module,
        "shared_resolver",
        lambda *_args: pytest.fail("graph maintenance used the shared resolver"),
    )

    report = epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()

    assert report["indexed_files"] == 2
    assert acquisitions == [vault]
    assert vault in find_module._RECALL_RESOLVER_CACHE


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
    real_snapshot = find_module.recall_resolver_snapshot
    acquisitions = 0

    def acquire(root: Path, **kwargs):
        nonlocal acquisitions
        snapshot = real_snapshot(root, **kwargs)
        acquisitions += 1
        if acquisitions == 1:
            staged_target.rename(target)
        return snapshot

    monkeypatch.setattr(find_module, "recall_resolver_snapshot", acquire)

    index = epistemic_graph.EpistemicGraphIndex(vault)
    report = index.rebuild_all()

    assert source.exists()
    assert acquisitions == 3
    assert report["indexed_files"] == 2
    assert any(
        edge["relation_type"] == "links_to"
        and edge["dst_key"] == epistemic_graph._file_key(target_rel)
        for edge in index.edges(source_path=A)
    )


def test_full_rebuild_of_a_continuously_moving_vault_is_marked_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vault that moves under every pass exhausts its bound and stays unavailable.

    Renamed from `..._twice_moving_vault_...` for #576. The contract this test
    exists for is unchanged and still asserted: a projection that never settles
    raises `did not stabilize` and leaves the graph unavailable. What changed is
    the number in the middle. Two attempts were a ceiling; they are now the
    floor, because two passes cannot converge against a corpus still being
    written to and the resulting Class C failure is what stranded the
    availability marker and sent the next write into another full rebuild. So
    this asserts the new bound -- the attempt ceiling, since this vault moves on
    every acquisition and so never reaches the elapsed deadline -- rather than
    the removed one.
    """
    vault = tmp_path / "vault"
    _seed(vault)
    real_snapshot = find_module.recall_resolver_snapshot
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

    monkeypatch.setattr(find_module, "recall_resolver_snapshot", acquire)
    index = epistemic_graph.EpistemicGraphIndex(vault)

    with pytest.raises(RuntimeError, match="did not stabilize"):
        index.rebuild_all()

    assert acquisitions == epistemic_graph.REBUILD_STABILIZATION_MAX_ATTEMPTS
    assert acquisitions > epistemic_graph.REBUILD_STABILIZATION_ATTEMPTS
    assert index.available() is False


def test_full_rebuild_retry_acquisition_failure_marks_graph_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    old_live = index.path.read_bytes()
    real_snapshot = find_module.recall_resolver_snapshot
    acquisitions = 0

    def acquire(root: Path, **kwargs):
        nonlocal acquisitions
        acquisitions += 1
        if acquisitions == 2:
            raise RuntimeError("retry resolver failed")
        snapshot = real_snapshot(root, **kwargs)
        _write(vault, "Knowledge Base/Notes/Insights/moved.md", "# Moved\n")
        return snapshot

    monkeypatch.setattr(find_module, "recall_resolver_snapshot", acquire)

    with pytest.raises(RuntimeError, match="retry resolver failed"):
        index.rebuild_all()

    assert acquisitions == 2
    assert index.available() is False
    assert index.path.read_bytes() == old_live


def test_full_rebuild_retry_freshness_failure_marks_graph_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    old_live = index.path.read_bytes()
    real_freshness = epistemic_graph._disk_vault_freshness
    real_snapshot = find_module.recall_resolver_snapshot
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
    monkeypatch.setattr(find_module, "recall_resolver_snapshot", acquire)

    with pytest.raises(RuntimeError, match="retry freshness failed"):
        index.rebuild_all()

    assert freshness_checks == 3
    assert index.available() is False
    assert index.path.read_bytes() == old_live


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

    assert index.available() is True


def test_full_rebuild_keeps_previous_schema_marker_visible_until_swap(
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
        assert index.available() is True
        indexed += 1
        return real_index_path(*args, **kwargs)

    monkeypatch.setattr(index, "_index_path", index_path)

    index.rebuild_all()

    assert indexed == 2
    assert index.available() is True


def test_full_rebuild_rejects_direct_edit_before_availability_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    source, _target = _seed(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    with index._connect() as conn:
        prior_rows = conn.execute(
            "SELECT node_key, source_hash FROM graph_nodes ORDER BY node_key"
        ).fetchall()
    attempts = 0
    temporary_paths: list[Path] = []

    def race_before_replace(temporary: Path, live: Path) -> None:
        nonlocal attempts
        attempts += 1
        assert live == index.path
        assert temporary != live
        temporary_paths.append(temporary)
        source.write_text(
            f"# Edited during availability publication attempt {attempts}\n",
            encoding="utf-8", newline="\n",)
        freshness.mark_external_pending(vault)

    monkeypatch.setattr(index, "_before_publish_replacement", race_before_replace)

    with pytest.raises(RuntimeError, match="did not stabilize"):
        index.rebuild_all()

    assert attempts == 2
    assert all(not temporary.exists() for temporary in temporary_paths)
    with index._connect() as conn:
        assert conn.execute(
            "SELECT node_key, source_hash FROM graph_nodes ORDER BY node_key"
        ).fetchall() == prior_rows
    assert index.available() is False
    assert index.nodes() == []


def test_full_rebuild_recovers_from_a_stale_live_registry(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    source, _target = _seed(vault)
    _seed_live_freshness(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()

    source.write_text(
        source.read_text(encoding="utf-8").replace("A claim", "Manual A claim"),
        encoding="utf-8", newline="\n",)

    report = index.rebuild_all()

    assert report["indexed_files"] == 2
    assert index.available() is True
    current = next(node for node in index.nodes(path=A) if node["kind"] == "file")
    assert current["source_hash"] == vault_module.content_hash(source.read_bytes().decode("utf-8"))
    conn = sqlite3.connect(epistemic_graph.sidecar_path(vault))
    try:
        checkpoint = conn.execute(
            "SELECT value FROM graph_meta WHERE key = 'recall_projection_checkpoint'"
        ).fetchone()
    finally:
        conn.close()
    assert checkpoint == (
        epistemic_graph._checkpoint_value(freshness.recall_checkpoint(vault, "vault")),
    )


def test_checkpointless_refresh_rebuilds_unseen_direct_edits(tmp_path: Path) -> None:
    """A disk-derived marker has no event suffix that can justify a local patch."""
    vault = tmp_path / "vault"
    source, target = _seed(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()

    source.write_text(
        source.read_text(encoding="utf-8").replace("A claim", "Changed A claim"),
        encoding="utf-8", newline="\n",)
    target.write_text(
        target.read_text(encoding="utf-8").replace("B claim", "Unseen B claim"),
        encoding="utf-8", newline="\n",)

    report = index.refresh_paths([source])

    assert report["indexed_files"] == 2
    assert index.available() is True
    current = next(node for node in index.nodes(path=B) if node["kind"] == "file")
    assert current["source_hash"] == vault_module.content_hash(target.read_bytes().decode("utf-8"))


def test_refresh_admitted_before_failed_rebuild_cannot_restore_availability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    source, _target = _seed(vault)
    _seed_live_freshness(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    real_snapshot = find_module.recall_resolver_snapshot_at_checkpoint
    real_index_path = index._index_path
    rebuild_active = False
    overlap_triggered = False

    def index_path(*args, **kwargs):
        if rebuild_active:
            raise RuntimeError("overlapping rebuild failed")
        return real_index_path(*args, **kwargs)

    def acquire(root: Path, checkpoint):
        nonlocal rebuild_active, overlap_triggered
        snapshot = real_snapshot(root, checkpoint)
        if not overlap_triggered:
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
    monkeypatch.setattr(find_module, "recall_resolver_snapshot_at_checkpoint", acquire)

    source.write_text(
        source.read_text(encoding="utf-8").replace("A claim", "Changed A claim"),
        encoding="utf-8", newline="\n",)
    freshness.on_files_changed(vault, changed=[source])
    find_module.on_resolver_files_changed(vault, [A], [])

    report = index.refresh_paths([source])

    assert overlap_triggered is True
    assert report["indexed_files"] == 1
    assert index.available() is True


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
    assert index.available() is True


def test_full_rebuild_snapshot_isolated_from_shared_cache_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    find_module._RESOLVER_CACHE.clear()
    shared = find_module.shared_resolver(vault)
    real_snapshot = find_module.recall_resolver_snapshot

    def acquire(root: Path, **kwargs):
        snapshot = real_snapshot(root, **kwargs)
        assert snapshot is not shared
        shared._remove_entry(B.removesuffix(".md"))
        return snapshot

    monkeypatch.setattr(find_module, "recall_resolver_snapshot", acquire)
    try:
        index = epistemic_graph.EpistemicGraphIndex(vault)
        index.rebuild_all()

        assert any(
            edge["relation_type"] == "links_to" and edge["dst_key"] == epistemic_graph._file_key(B)
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
    real_snapshot = find_module.recall_resolver_snapshot
    acquisitions: list[Path] = []

    def acquire(root: Path, **kwargs):
        acquisitions.append(root)
        return real_snapshot(root, **kwargs)

    monkeypatch.setattr(find_module, "recall_resolver_snapshot", acquire)
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
        "recall_resolver_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("resolver failed")),
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
    with monkeypatch.context() as scoped:
        scoped.setattr(
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
        encoding="utf-8", newline="\n",)
    page = find_module._parse_page(source, source.stat().st_mtime, vault)
    assert page is not None
    state = semantic_index.build_parent_index_state(vault, source)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    kwargs = {
        "registry": index.registry,
        "source_hash": epistemic_graph.vault_module.content_hash(
            source.read_bytes().decode("utf-8")
        ),
        "parent_state": state,
    }

    fallback = epistemic_graph._edges_for_page(vault, page, state.document, **kwargs)
    explicit = epistemic_graph._edges_for_page(
        vault,
        page,
        state.document,
        resolver=find_module.writer_resolver_snapshot(vault),
        **kwargs,
    )

    assert [edge.as_dict() for edge in explicit] == [edge.as_dict() for edge in fallback]


def test_single_file_edit_refreshes_affected_graph_rows(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    a, b = _seed(vault)
    _seed_live_freshness(vault)
    idx = epistemic_graph.EpistemicGraphIndex(vault)
    idx.rebuild_all()
    b_before = next(n for n in idx.nodes(path=B) if n["kind"] == "file")["source_hash"]

    a.write_text(
        a.read_text(encoding="utf-8").replace("A claim", "A changed claim"),
        encoding="utf-8", newline="\n",)
    freshness.on_files_changed(vault, changed=[a])
    find_module.on_resolver_files_changed(vault, [A], [])
    report = idx.refresh_paths([a])

    assert report["indexed_files"] == 1
    a_after = next(n for n in idx.nodes(path=A) if n["kind"] == "file")
    b_after = next(n for n in idx.nodes(path=B) if n["kind"] == "file")
    assert a_after["source_hash"] == epistemic_graph.vault_module.content_hash(
        a.read_bytes().decode("utf-8")
    )
    assert b_after["source_hash"] == b_before


def test_live_incremental_refresh_does_not_repeat_a_full_disk_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    source, _target = _seed(vault)
    _seed_live_freshness(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()

    source.write_text(
        source.read_text(encoding="utf-8").replace("A claim", "A live changed claim"),
        encoding="utf-8", newline="\n",)
    freshness.on_files_changed(vault, changed=[source])
    find_module.on_resolver_files_changed(vault, [A], [])
    monkeypatch.setattr(
        epistemic_graph,
        "_disk_vault_freshness",
        lambda *_args: pytest.fail("live incremental refresh performed a full disk walk"),
    )
    monkeypatch.setattr(
        epistemic_graph,
        "_resolver_topology_fingerprint",
        lambda *_args: pytest.fail("body-only refresh hashed whole resolver topology"),
    )
    monkeypatch.setattr(
        vault_module.WikilinkResolver,
        "fork",
        lambda *_args: pytest.fail("body-only refresh copied the whole resolver"),
    )
    for method_name in (
        "_checkpoint_membership",
        "_recall_membership",
        "_resolver_source_versions",
        "_resolver_affected_sources",
        "_stored_full_resolver_topology",
    ):
        monkeypatch.setattr(
            index,
            method_name,
            lambda *_args, _name=method_name, **_kwargs: pytest.fail(
                f"body-only refresh called {_name}"
            ),
        )

    report = index.refresh_paths([source])

    assert report["indexed_files"] == 1
    current = next(node for node in index.nodes(path=A) if node["kind"] == "file")
    assert current["source_hash"] == epistemic_graph.vault_module.content_hash(
        source.read_bytes().decode("utf-8")
    )


def test_source_version_recheck_preserves_crlf_bytes(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    source = vault / A
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(
        b"---\r\ntype: insight\r\nstatus: active\r\n---\r\n"
        b"# A\r\n\r\n## Claim\r\n\r\nWindows line endings stay exact.\r\n"
    )
    index = epistemic_graph.EpistemicGraphIndex(vault)
    disk_freshness = epistemic_graph._disk_vault_freshness(vault)
    resolver = find_module.recall_resolver_snapshot(vault, freshness=disk_freshness)
    membership = index._recall_membership()

    assert membership == frozenset({A})
    versions = index._resolver_source_versions(resolver, membership)
    assert versions is not None
    assert index._source_versions_current(versions)
    index.rebuild_all()
    assert index.available()
    file_node = next(node for node in index.nodes(path=A) if node["kind"] == "file")
    assert file_node["source_hash"] == vault_module.content_hash(
        source.read_bytes().decode("utf-8")
    )


def test_incremental_projection_identity_does_not_materialize_recall_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    _seed_live_freshness(vault)
    checkpoint = freshness.recall_checkpoint(vault, "vault")
    monkeypatch.setattr(
        find_module,
        "FreshnessSnapshot",
        lambda *_args, **_kwargs: pytest.fail(
            "identity-only graph check materialized the request recall projection"
        ),
    )

    assert epistemic_graph._incremental_projection_identity(vault) == (
        checkpoint.triple,
        checkpoint.policy_version,
        checkpoint.access_policy_fingerprint,
    )


def test_external_event_observed_during_snapshot_open_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    _seed_live_freshness(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    real_identity = epistemic_graph._incremental_projection_identity
    pending_epoch = 0

    def observe_during_validation(root: Path):
        nonlocal pending_epoch
        pending_epoch = freshness.mark_external_pending(root)
        return real_identity(root)

    monkeypatch.setattr(
        epistemic_graph,
        "_incremental_projection_identity",
        observe_during_validation,
    )

    assert index._open_read_snapshot() is None
    assert pending_epoch > 0
    freshness.clear_external_pending(vault, through=pending_epoch)


def test_created_unreferenced_target_avoids_unnecessary_full_reresolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    _seed_live_freshness(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    created_rel = "Knowledge Base/Notes/Insights/unreferenced.md"
    created = _write(vault, created_rel, "# Unique unreferenced target\n")
    freshness.on_files_changed(vault, changed=[created])
    find_module.on_resolver_files_changed(vault, [created_rel], [])
    monkeypatch.setattr(
        index,
        "_rebuild_all_locked",
        lambda: pytest.fail("a proven-unreferenced create forced a full graph rebuild"),
    )

    report = index.refresh_paths([created], created_paths=[created])

    assert report["indexed_files"] == 1
    assert index.available() is True
    assert next(node for node in index.nodes(path=created_rel) if node["kind"] == "file")


def test_created_target_reresolves_only_sources_whose_links_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    source = _write(
        vault,
        A,
        "---\ntype: insight\nstatus: active\n---\n# A\n\nLinks to [[Future Target]].\n",
    )
    _write(vault, B, "# Existing B\n")
    _seed_live_freshness(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    assert index.relation_participants(["links_to"]).paths == frozenset()
    created_rel = "Knowledge Base/Notes/Insights/future.md"
    created = _write(
        vault,
        created_rel,
        "---\ntype: insight\nstatus: active\ntitle: Future Target\n---\n# Future\n",
    )
    freshness.on_files_changed(vault, changed=[created])
    find_module.on_resolver_files_changed(vault, [created_rel], [])
    monkeypatch.setattr(
        index,
        "_rebuild_all_locked",
        lambda: pytest.fail("a bounded title addition forced a full graph rebuild"),
    )

    report = index.refresh_paths([created], created_paths=[created])

    assert source.exists()
    assert report["indexed_files"] == 2
    assert index.available() is True
    assert index.relation_participants(["links_to"]).paths == frozenset({A, created_rel})


def test_indirect_source_edit_before_publication_cannot_publish_stale_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    source = _write(
        vault,
        A,
        "---\ntype: insight\nstatus: active\n---\n# A\n\nLinks to [[Future Target]].\n",
    )
    _write(vault, B, "# Existing B\n")
    _seed_live_freshness(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    created_rel = "Knowledge Base/Notes/Insights/future.md"
    created = _write(
        vault,
        created_rel,
        "---\ntype: insight\nstatus: active\ntitle: Future Target\n---\n# Future\n",
    )
    freshness.on_files_changed(vault, changed=[created])
    find_module.on_resolver_files_changed(vault, [created_rel], [])
    real_mark = index._mark_incremental_available

    def edit_before_mark(*args, **kwargs):
        source.write_text("# A\n\nDirect edit removed the link.\n", encoding="utf-8", newline="\n")
        return real_mark(*args, **kwargs)

    monkeypatch.setattr(index, "_mark_incremental_available", edit_before_mark)

    report = index.refresh_paths([created], created_paths=[created])

    assert report["indexed_files"] == 3
    assert index.available() is True
    assert index.relation_participants(["links_to"]).paths == frozenset()


def test_topology_scan_input_edit_before_refresh_cannot_publish_stale_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    source = _write(vault, A, "# A\n\nNo link yet.\n")
    _write(vault, B, "# Existing B\n")
    _seed_live_freshness(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    created_rel = "Knowledge Base/Notes/Insights/future.md"
    created = _write(
        vault,
        created_rel,
        "---\ntype: insight\nstatus: active\ntitle: Future Target\n---\n# Future\n",
    )
    freshness.on_files_changed(vault, changed=[created])
    find_module.on_resolver_files_changed(vault, [created_rel], [])
    real_refresh_pass = index._refresh_paths_pass

    def edit_before_refresh(*args, **kwargs):
        source.write_text("# A\n\nNow links to [[Future Target]].\n", encoding="utf-8", newline="\n")
        return real_refresh_pass(*args, **kwargs)

    monkeypatch.setattr(index, "_refresh_paths_pass", edit_before_refresh)

    report = index.refresh_paths([created], created_paths=[created])

    assert report["indexed_files"] == 3
    assert index.available() is True
    assert index.relation_participants(["links_to"]).paths == frozenset({A, created_rel})


def test_topology_membership_change_before_refresh_cannot_publish_stale_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _write(vault, A, "# A\n\nLinks to [[Future Target]].\n")
    _write(vault, B, "# Existing B\n")
    _seed_live_freshness(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    created_rel = "Knowledge Base/Notes/Insights/future.md"
    created = _write(
        vault,
        created_rel,
        "---\ntype: insight\nstatus: active\ntitle: Future Target\n---\n# Future\n",
    )
    freshness.on_files_changed(vault, changed=[created])
    find_module.on_resolver_files_changed(vault, [created_rel], [])
    duplicate_rel = "Knowledge Base/Notes/Insights/future-duplicate.md"
    real_refresh_pass = index._refresh_paths_pass

    def create_ambiguous_target_before_refresh(*args, **kwargs):
        _write(
            vault,
            duplicate_rel,
            "---\ntype: insight\nstatus: active\ntitle: Future Target\n---\n# Duplicate\n",
        )
        return real_refresh_pass(*args, **kwargs)

    monkeypatch.setattr(
        index, "_refresh_paths_pass", create_ambiguous_target_before_refresh
    )

    report = index.refresh_paths([created], created_paths=[created])

    assert report["indexed_files"] == 4
    assert index.available() is True
    assert next(node for node in index.nodes(path=duplicate_rel) if node["kind"] == "file")
    assert index.relation_participants(["links_to"]).paths == frozenset()


def test_topology_resolver_membership_change_outside_kb_forces_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _write(vault, A, "# A\n\nLinks to [[Future Target]].\n")
    _write(vault, B, "# Existing B\n")
    _seed_live_freshness(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    created_rel = "Knowledge Base/Notes/Insights/future.md"
    created = _write(
        vault,
        created_rel,
        "---\ntype: insight\nstatus: active\ntitle: Future Target\n---\n# Future\n",
    )
    freshness.on_files_changed(vault, changed=[created])
    find_module.on_resolver_files_changed(vault, [created_rel], [])
    shadow_rel = "Reference/shadow.md"
    real_refresh_pass = index._refresh_paths_pass

    def create_ambiguous_target_before_refresh(*args, **kwargs):
        _write(
            vault,
            shadow_rel,
            "---\ntitle: Future Target\n---\n# Shadow\n",
        )
        return real_refresh_pass(*args, **kwargs)

    monkeypatch.setattr(
        index, "_refresh_paths_pass", create_ambiguous_target_before_refresh
    )

    report = index.refresh_paths([created], created_paths=[created])

    assert report["indexed_files"] == 3
    assert index.available() is True
    assert index.nodes(path=shadow_rel) == []
    assert index.relation_participants(["links_to"]).paths == frozenset()


def test_topology_resolver_title_mismatch_outside_kb_forces_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _write(vault, A, "# A\n\nLinks to [[Future Target]].\n")
    _write(vault, B, "# Existing B\n")
    shadow = _write(
        vault,
        "Reference/shadow.md",
        "---\ntitle: Shadow Target\n---\n# Shadow\n",
    )
    _seed_live_freshness(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    shadow.write_text(
        "---\ntitle: Future Target\n---\n# Shadow\n",
        encoding="utf-8", newline="\n",)
    created_rel = "Knowledge Base/Notes/Insights/future.md"
    created = _write(
        vault,
        created_rel,
        "---\ntype: insight\nstatus: active\ntitle: Future Target\n---\n# Future\n",
    )
    freshness.on_files_changed(vault, changed=[created])
    find_module.on_resolver_files_changed(vault, [created_rel], [])
    find_module.unload_ram_caches()
    stale_disk_identity = freshness.recall_checkpoint(vault, "vault")
    assert stale_disk_identity is not None
    real_disk_freshness = epistemic_graph._disk_vault_freshness
    calls = 0

    def windows_style_disk_freshness(root: Path):
        nonlocal calls
        calls += 1
        if calls == 1:
            return stale_disk_identity.triple
        return real_disk_freshness(root)

    monkeypatch.setattr(
        epistemic_graph, "_disk_vault_freshness", windows_style_disk_freshness
    )

    report = index.refresh_paths([created], created_paths=[created])

    assert report["indexed_files"] == 3
    assert index.available() is True
    assert index.relation_participants(["links_to"]).paths == frozenset()


def test_observed_external_edit_defers_body_refresh_until_watcher_publication(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _write(vault, A, "# A\n\nLinks to [[Future Target]].\n")
    _write(
        vault,
        B,
        "---\ntitle: Future Target\n---\n# B\n",
    )
    c_rel = "Knowledge Base/Notes/Insights/c.md"
    c = _write(vault, c_rel, "# C\n\nBefore.\n")
    shadow = _write(
        vault,
        "Reference/shadow.md",
        "---\ntitle: Shadow Target\n---\n# Shadow\n",
    )
    _seed_live_freshness(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    assert index.relation_participants(["links_to"]).paths == frozenset({A, B})

    # Watchdog observes the edit synchronously, before its debounced registry
    # publication. A racing canonical writer must fail the graph closed in O(1)
    # rather than either blessing the stale resolver or scanning the vault.
    shadow.write_text(
        "---\ntitle: Future Target\n---\n# Shadow\n",
        encoding="utf-8", newline="\n",)
    pending_epoch = freshness.mark_external_pending(vault)
    c.write_text("# C\n\nAfter!.\n", encoding="utf-8", newline="\n")
    freshness.on_files_changed(vault, changed=[c])
    find_module.on_resolver_files_changed(vault, [c_rel], [])

    report = index.refresh_paths([c])

    assert report["deferred"] == 1
    assert index.available() is False

    # The watcher publishes and patches its full vault-wide event before
    # acknowledging the observed epoch. Its non-KB graph notification then
    # repairs the resolver-dependent KB edges from canonical disk state.
    freshness.on_files_changed(vault, changed=[shadow])
    find_module.on_resolver_files_changed(vault, ["Reference/shadow.md"], [])
    freshness.clear_external_pending(vault, through=pending_epoch)
    repaired = index.refresh_paths([shadow])

    assert repaired["indexed_files"] == 3
    assert index.available() is True
    assert index.relation_participants(["links_to"]).paths == frozenset()


def test_topology_scan_rejects_source_hash_mismatch_hidden_by_file_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _write(vault, A, "# A\n\nLinks to [[Future Target]].\n")
    _write(vault, B, "# Existing B\n")
    unrelated_rel = "Knowledge Base/Notes/Insights/unrelated.md"
    unrelated = _write(vault, unrelated_rel, "# Unrelated\n\nBefore.\n")
    _seed_live_freshness(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    unrelated.write_text("# Unrelated\n\nAfter!.\n", encoding="utf-8", newline="\n")
    created_rel = "Knowledge Base/Notes/Insights/future.md"
    created = _write(
        vault,
        created_rel,
        "---\ntype: insight\nstatus: active\ntitle: Future Target\n---\n# Future\n",
    )
    freshness.on_files_changed(vault, changed=[created])
    find_module.on_resolver_files_changed(vault, [created_rel], [])
    stale_disk_identity = freshness.recall_checkpoint(vault, "vault")
    assert stale_disk_identity is not None
    real_disk_freshness = epistemic_graph._disk_vault_freshness
    calls = 0

    def windows_style_disk_freshness(root: Path):
        nonlocal calls
        calls += 1
        if calls == 1:
            # Model a filesystem whose path/mtime/ctime/size identity did not
            # expose the same-length in-place edit during the topology proof.
            return stale_disk_identity.triple
        return real_disk_freshness(root)

    monkeypatch.setattr(
        epistemic_graph, "_disk_vault_freshness", windows_style_disk_freshness
    )

    report = index.refresh_paths([created], created_paths=[created])

    assert report["indexed_files"] == 4
    assert index.available() is True
    current = next(
        node for node in index.nodes(path=unrelated_rel) if node["kind"] == "file"
    )
    assert current["source_hash"] == epistemic_graph.vault_module.content_hash(
        unrelated.read_bytes().decode("utf-8")
    )


def test_direct_index_sync_delete_publishes_before_graph_fanout(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _source, target = _seed(vault)
    _seed_live_freshness(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()

    target.unlink()
    index_sync.delete_after_remove(vault, [B])

    assert index.available() is True
    assert index.nodes(path=B) == []


def test_live_refresh_replays_every_published_delta_before_availability(
    tmp_path: Path,
) -> None:
    """A delayed B fan-out cannot let an A refresh stamp B's later checkpoint."""
    vault = tmp_path / "vault"
    a, b = _seed(vault)
    _seed_live_freshness(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()

    a.write_text(
        a.read_text(encoding="utf-8").replace("A claim", "A refreshed claim"),
        encoding="utf-8", newline="\n",)
    b.write_text(
        b.read_text(encoding="utf-8").replace("B claim", "B changed during delayed fanout"),
        encoding="utf-8", newline="\n",)
    freshness.on_files_changed(vault, changed=[a])
    freshness.on_files_changed(vault, changed=[b])
    # The B resolver/index callback is delayed or lost.  The A callback is all
    # this refresh receives, but the graph marker must not claim B is current
    # unless it replays the retained A+B projected delta itself.
    find_module.on_resolver_files_changed(vault, [A], [])

    report = index.refresh_paths([a])

    assert report["indexed_files"] == 2
    assert index.available() is True
    b_node = next(node for node in index.nodes(path=B) if node["kind"] == "file")
    assert b_node["source_hash"] == epistemic_graph.vault_module.content_hash(
        b.read_bytes().decode("utf-8")
    )
    assert index.relation_participants(["links_to"]).paths == frozenset({A, B})


def test_title_only_target_change_reresolves_unchanged_sources(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    source = _write(
        vault,
        A,
        "---\ntype: insight\nstatus: active\n---\n# A\n\n## Claim\n\nLinks to [[Old B]].\n",
    )
    target = _write(
        vault,
        B,
        "---\ntype: insight\nstatus: active\ntitle: Old B\n---\n# B\n\n## Claim\n\nTarget.\n",
    )
    _seed_live_freshness(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    assert index.relation_participants(["links_to"]).paths == frozenset({A, B})

    target.write_text(
        target.read_text(encoding="utf-8").replace("title: Old B", "title: New B"),
        encoding="utf-8", newline="\n",)
    freshness.on_files_changed(vault, changed=[target])
    find_module.on_resolver_files_changed(vault, [B], [])

    report = index.refresh_paths([target])

    assert source.exists()
    assert report["indexed_files"] == 2
    assert index.available() is True
    assert index.relation_participants(["links_to"]).paths == frozenset()


def test_projected_resolver_coalesces_renamed_target_before_delayed_callback(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    source = _write(
        vault,
        A,
        "---\ntype: insight\nstatus: active\n---\n# A\n\n## Claim\n\nA claim links to [[Old B]].\n",
    )
    target = _write(
        vault,
        B,
        "---\ntype: insight\nstatus: active\n---\n# Old B\n\n## Claim\n\nTarget.\n",
    )
    _seed_live_freshness(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()

    # Warm a resolver from the exact projected checkpoint. The watcher then
    # publishes A and B separately, but delivers only A's resolver callback.
    # A path-local patch must still see B's coalesced target title.
    find_module.recall_resolver_snapshot(vault)
    source.write_text(
        "---\ntype: insight\nstatus: active\n---\n# A\n\n## Claim\n\nA claim links to [[New B]].\n",
        encoding="utf-8", newline="\n",)
    target.write_text(
        "---\ntype: insight\nstatus: active\n---\n# New B\n\n## Claim\n\nTarget.\n",
        encoding="utf-8", newline="\n",)
    freshness.on_files_changed(vault, changed=[source])
    freshness.on_files_changed(vault, changed=[target])
    find_module.on_resolver_files_changed(vault, [A], [])

    resolver = find_module.recall_resolver_snapshot(vault)
    resolved, warning = vault_module.normalize_wikilink(
        "New B", vault, resolver=resolver, strict=False
    )
    assert warning is None
    assert resolved == B.removesuffix(".md")
    old, old_warning = vault_module.normalize_wikilink(
        "Old B", vault, resolver=resolver, strict=False
    )
    assert old == "Old B"
    assert old_warning is not None

    index.refresh_paths([source])
    assert index.relation_participants(["links_to"]).paths == frozenset({A, B})

    # The delayed B callback is now a no-op. Its result remains equivalent to
    # a direct full rebuild over the same human-owned files.
    find_module.on_resolver_files_changed(vault, [B], [])
    index.refresh_paths([target])
    incremental_edges = index.edges()
    index.rebuild_all()
    assert index.edges() == incremental_edges


def test_published_edit_withholds_stale_graph_until_incremental_refresh(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    source, _target = _seed(vault)
    _seed_live_freshness(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    assert index.relation_participants(["links_to"]).paths == frozenset({A, B})

    source.write_text("# A\n\nThe link was removed directly.\n", encoding="utf-8", newline="\n")
    freshness.on_files_changed(vault, changed=[source])
    find_module.on_resolver_files_changed(vault, [A], [])

    assert index.available() is False
    assert index.edges() == []
    assert index.relation_participants(["links_to"]).status == "warming"

    report = index.refresh_paths([source])

    assert report["indexed_files"] == 1
    assert index.available() is True
    assert index.relation_participants(["links_to"]).paths == frozenset()


def test_incremental_refresh_retries_when_path_changes_during_indexing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    source, _target = _seed(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    real_edges_for_page = epistemic_graph._edges_for_page
    real_snapshot = find_module.recall_resolver_snapshot
    acquisitions: list[Path] = []
    raced = False

    def edges_for_page(*args, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            source.write_text("# Raced refresh source\n", encoding="utf-8", newline="\n")
        return real_edges_for_page(*args, **kwargs)

    def acquire(root: Path, **kwargs):
        acquisitions.append(root)
        return real_snapshot(root, **kwargs)

    monkeypatch.setattr(epistemic_graph, "_edges_for_page", edges_for_page)
    monkeypatch.setattr(find_module, "recall_resolver_snapshot", acquire)

    report = index.refresh_paths([source])

    assert raced is True
    assert acquisitions == [vault, vault]
    assert report["indexed_files"] == 2
    current = next(node for node in index.nodes(path=A) if node["kind"] == "file")
    assert current["source_hash"] == epistemic_graph.vault_module.content_hash(
        source.read_bytes().decode("utf-8")
    )


def test_incremental_graph_update_matches_full_rebuild(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    a, _b = _seed(vault)
    idx = epistemic_graph.EpistemicGraphIndex(vault)
    idx.rebuild_all()

    a.write_text(
        a.read_text(encoding="utf-8") + "\n## Decision\n\nKeep it derived.\n",
        encoding="utf-8", newline="\n",)
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
    a.write_text(changed, encoding="utf-8", newline="\n")

    report = audit.audit(vault, categories=["graph_drift"])
    assert report.findings
    assert report.findings[0].category == "graph_drift"

    reconciled = reconcile.reconcile(vault)

    assert a.read_text(encoding="utf-8") == changed
    assert reconciled.graph_status == "refreshed"
    assert all(f["category"] != "graph_drift" for f in reconciled.remaining_drift)


def test_reconcile_rebuilds_a_deleted_graph_sidecar(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    epistemic_graph.sidecar_path(vault).unlink()

    reconciled = reconcile.reconcile(vault)

    assert reconciled.graph_status == "refreshed"
    assert epistemic_graph.EpistemicGraphIndex(vault).available()


def test_disabled_graph_indexing_makes_drift_check_noop(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_INDEX", "1")

    report = audit.audit(vault, categories=["graph_drift"])

    assert report.findings == []


def test_disabled_canonical_upsert_bars_an_existing_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    source, _target = _seed(vault)
    _seed_live_freshness(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    old_signature = freshness.stat_signature(source)

    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "A claim links to [[Knowledge Base/Notes/Insights/b]].",
            "A claim no longer links to the other note.",
        ),
        encoding="utf-8", newline="\n",)
    real_stat_signature = freshness.stat_signature
    monkeypatch.setattr(
        freshness,
        "stat_signature",
        lambda path: old_signature if Path(path) == source else real_stat_signature(path),
    )
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_INDEX", "1")

    index_sync.upsert_after_write(vault, [source])

    assert index.reads_suspended() is True
    monkeypatch.delenv("EXOMEM_DISABLE_GRAPH_INDEX")
    assert index.available() is False
    index.rebuild_all()
    assert not any(
        edge["relation_type"] == "links_to" for edge in index.edges(source_path=A)
    )
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_INDEX", "1")
    epistemic_graph.delete_after_remove(vault, [A])
    assert index.reads_suspended() is True


def test_failed_disabled_canonical_barrier_marks_recovery_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    source, _target = _seed(vault)
    _seed_live_freshness(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    old_signature = freshness.stat_signature(source)
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "A claim links to [[Knowledge Base/Notes/Insights/b]].",
            "A claim no longer links to the other note.",
        ),
        encoding="utf-8", newline="\n",)
    real_stat_signature = freshness.stat_signature
    monkeypatch.setattr(
        freshness,
        "stat_signature",
        lambda path: old_signature if Path(path) == source else real_stat_signature(path),
    )
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_INDEX", "1")
    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "suspend_reads",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("barrier failed")),
    )

    index_sync.upsert_after_write(vault, [source])

    assert freshness.external_pending(vault) is True
    assert index.available() is False

    # A failed SQLite barrier cannot be the only crash boundary.  After the
    # writer exits, a direct graph consumer has neither its process-local
    # pending epoch nor a watcher startup rebuild to protect it.  Even when a
    # coarse filesystem signature collides, that cold reader must reject the
    # stale source bytes preserved in the sidecar.
    monkeypatch.delenv("EXOMEM_DISABLE_GRAPH_INDEX")
    freshness.clear()
    find_module.unload_ram_caches()

    assert freshness.external_pending(vault) is False
    assert index.available() is False
    assert index.edges(source_path=A) == []


def test_cold_source_proof_rechecks_bytes_changed_during_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    source, _target = _seed(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    freshness.clear()
    find_module.unload_ram_caches()
    real_fingerprint = epistemic_graph._resolver_topology_fingerprint
    mutated = False

    def mutate_after_first_byte_pass(resolver: vault_module.WikilinkResolver) -> str:
        nonlocal mutated
        if not mutated:
            mutated = True
            source.write_text(
                source.read_text(encoding="utf-8").replace(
                    "A claim links to [[Knowledge Base/Notes/Insights/b]].",
                    "A claim no longer links to the other note.",
                ),
                encoding="utf-8", newline="\n",)
        return real_fingerprint(resolver)

    monkeypatch.setattr(
        epistemic_graph,
        "_resolver_topology_fingerprint",
        mutate_after_first_byte_pass,
    )

    assert index.edges(source_path=A) == []
    assert mutated is True


@pytest.mark.skipif(os.name == "nt", reason="symlink swap requires Unix test privileges")
def test_cold_source_proof_never_follows_post_admission_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    source, _target = _seed(vault)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    freshness.clear()
    find_module.unload_ram_caches()
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n\nSensitive external bytes.\n", encoding="utf-8", newline="\n")
    real_indexed_membership = index._indexed_recall_membership
    swapped = False

    def swap_after_admission() -> frozenset[str] | None:
        nonlocal swapped
        result = real_indexed_membership()
        if not swapped:
            swapped = True
            source.unlink()
            source.symlink_to(outside)
        return result

    followed_symlink = False
    real_read_bytes = Path.read_bytes

    def observe_unsafe_read(path: Path) -> bytes:
        nonlocal followed_symlink
        if path == source and path.is_symlink():
            followed_symlink = True
        return real_read_bytes(path)

    monkeypatch.setattr(index, "_indexed_recall_membership", swap_after_admission)
    monkeypatch.setattr(Path, "read_bytes", observe_unsafe_read)

    assert index.edges(source_path=A) == []
    assert swapped is True
    assert followed_symlink is False


def test_relation_edges_follow_incremental_edit_move_and_delete(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    source, _target = _seed(vault)
    source.write_text(
        source.read_text(encoding="utf-8") + "\n- supports: [[Knowledge Base/Notes/Insights/b]]\n",
        encoding="utf-8", newline="\n",)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    assert any(edge["relation_type"] == "supports" for edge in index.edges(source_path=A))

    source.write_text(
        source.read_text(encoding="utf-8").replace("supports:", "contradicts:"),
        encoding="utf-8", newline="\n",)
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
        edge["relation_type"] == "contradicts" for edge in index.edges(source_path=moved_rel)
    )

    moved.unlink()
    index.delete_paths([moved_rel])
    assert index.nodes(path=moved_rel) == []
    assert index.edges(source_path=moved_rel) == []


def test_target_refresh_preserves_inbound_relation_as_placeholder(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    source, target = _seed(vault)
    source.write_text(
        source.read_text(encoding="utf-8") + "\n- supports: [[Knowledge Base/Notes/Insights/b]]\n",
        encoding="utf-8", newline="\n",)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    before = next(
        edge for edge in index.edges(source_path=A) if edge["relation_type"] == "supports"
    )
    target.write_text(target.read_text(encoding="utf-8") + "\nUpdated.\n", encoding="utf-8", newline="\n")
    index.refresh_paths([target])
    assert before in index.edges(source_path=A)


def test_incremental_write_after_registry_change_forces_full_reresolution(tmp_path: Path) -> None:
    import yaml

    vault = tmp_path / "vault"
    source, target = _seed(vault)
    registry_path = vault / "Knowledge Base" / "_Schema" / "relation-registry.yaml"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    proposal = {
        "schema_version": 1,
        "extensions": {
            "science.replicates": {
                "parent": "supports",
                "description": "Reports independent reproduction",
                "aliases": ["mirrors"],
            }
        },
    }
    registry_path.write_text(yaml.safe_dump(proposal), encoding="utf-8", newline="\n")
    source.write_text(
        source.read_text(encoding="utf-8") + "\n- mirrors: [[Knowledge Base/Notes/Insights/b]]\n",
        encoding="utf-8", newline="\n",)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    proposal["extensions"]["science.replicates"]["aliases"] = ["reproduces"]
    registry_path.write_text(yaml.safe_dump(proposal), encoding="utf-8", newline="\n")
    target.write_text(target.read_text(encoding="utf-8") + "\nChanged.\n", encoding="utf-8", newline="\n")

    report = epistemic_graph.EpistemicGraphIndex(vault).refresh_paths([target])

    assert report["indexed_files"] == 2
    changed = next(
        edge
        for edge in epistemic_graph.EpistemicGraphIndex(vault).edges(source_path=A)
        if edge["raw_relation"] == "mirrors"
    )
    assert changed["registry_status"] == "unregistered"
