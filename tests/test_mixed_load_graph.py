"""Read-only convergence proof used by the mixed-load benchmark."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from exomem import deferred_index, freshness, graph_sync
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.epistemic_graph import EpistemicGraphIndex

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mixed_load_graph.py"
PAGE_A = "Knowledge Base/Notes/Insights/tracker.md"
PAGE_B = "Knowledge Base/Notes/Insights/background.md"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("mixed_load_graph", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _page(title: str, body: str) -> str:
    return f"---\ntype: insight\nstatus: active\n---\n# {title}\n\n{body}\n"


def _seed_live_freshness(root: Path) -> None:
    freshness.seed(
        root,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(root)),
    )
    kb = root / "Knowledge Base"
    freshness.seed(
        root,
        "kb",
        ((str(path), freshness.stat_signature(path)) for path in find_module._walk_md(kb)),
    )


@pytest.fixture
def ready_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    root = tmp_path / "vault"
    (root / "Knowledge Base/Notes/Insights").mkdir(parents=True)
    (root / PAGE_A).write_text(
        _page(
            "Tracker",
            "## Relations\n\n- supports [[Knowledge Base/Notes/Insights/background]]",
        ),
        encoding="utf-8",
    )
    (root / PAGE_B).write_text(_page("Background", "Reference state."), encoding="utf-8")
    _seed_live_freshness(root)

    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=1,
        mutation_id="0123456789abcdef01234567",
        paths=(),
        created_paths=(),
        scope="full",
    )
    graph_sync._write_floor(root, graph_sync.GraphSyncGenerationFloor.create(1))
    graph_sync._write_checkpoint(root, checkpoint)
    EpistemicGraphIndex(root).rebuild_all()
    assert graph_sync.classify_epoch(root).kind == "coherent"
    return root


def test_inspect_graph_proves_a_current_fixture_and_expected_link(ready_graph: Path) -> None:
    graph = _load_module()

    proof = graph.inspect_graph(
        ready_graph,
        expected_link=(PAGE_A, PAGE_B),
        expected_relation="supports",
    )

    assert proof["ready"] is True
    assert proof["reason"] is None
    assert proof["epoch"]["kind"] == "coherent"
    assert proof["epoch"]["floor_generation"] == 1
    assert proof["epoch"]["checkpoint_generation"] == 1
    assert proof["epoch"]["acknowledgement_generation"] == 1
    assert len(proof["epoch"]["checkpoint_sha256"]) == 64
    assert proof["epoch"]["checkpoint_sha256"] == proof["epoch"]["acknowledgement_sha256"]
    assert proof["membership"]["source_count"] == 2
    assert proof["membership"]["graph_count"] == 2
    assert len(proof["membership"]["source_digest"]) == 64
    assert proof["membership"]["source_digest"] == proof["membership"]["graph_digest"]
    assert proof["queues"] == {"graph": 0, "full": 0, "semantic": 0}


def test_inspect_graph_rejects_an_old_edge_that_is_still_present(ready_graph: Path) -> None:
    graph = _load_module()
    proof = graph.inspect_graph(
        ready_graph, expected_link=(PAGE_A, PAGE_B), expected_relation="supports",
        absent_link=(PAGE_A, PAGE_B),
    )
    assert proof["ready"] is False
    assert proof["reason"] == "old_link_still_present"


def test_inspect_graph_proves_old_edge_absence_in_same_snapshot(ready_graph: Path) -> None:
    graph = _load_module()
    proof = graph.inspect_graph(
        ready_graph, expected_link=(PAGE_A, PAGE_B), expected_relation="supports",
        absent_link=(PAGE_B, PAGE_A),
    )
    assert proof["ready"] is True
    assert proof["absent_link"]["present"] is False
    assert proof["expected_link"] == {
        "source": PAGE_A,
        "target": PAGE_B,
        "relation": "supports",
        "present": True,
    }
    assert proof["proof_elapsed_ms"] >= 0


def test_inspect_graph_rejects_a_stale_file_hash(ready_graph: Path) -> None:
    graph = _load_module()
    (ready_graph / PAGE_A).write_text(_page("Tracker", "Changed after graph build."), encoding="utf-8")

    proof = graph.inspect_graph(ready_graph)

    assert proof["ready"] is False
    assert proof["reason"] == "file_hash_stale"


def test_inspect_graph_rejects_a_missing_graph_source(ready_graph: Path) -> None:
    graph = _load_module()
    (ready_graph / PAGE_A).unlink()

    proof = graph.inspect_graph(ready_graph)

    assert proof["ready"] is False
    assert proof["reason"] == "file_missing"


def test_inspect_graph_rejects_legacy_epoch_without_an_acknowledgement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    root = tmp_path / "vault"
    (root / "Knowledge Base/Notes").mkdir(parents=True)
    (root / "Knowledge Base/Notes/example.md").write_text(_page("Example", "Body."), encoding="utf-8")
    _seed_live_freshness(root)
    EpistemicGraphIndex(root).rebuild_all()
    assert graph_sync.classify_epoch(root).kind == "legacy"
    graph = _load_module()

    proof = graph.inspect_graph(root)

    assert proof["ready"] is False
    assert proof["reason"] == "epoch_not_coherent"


def test_inspect_graph_rejects_pending_graph_receipts(ready_graph: Path) -> None:
    graph = _load_module()
    deferred_index.add_graph_receipts(ready_graph, [PAGE_A])

    proof = graph.inspect_graph(ready_graph)

    assert proof["ready"] is False
    assert proof["reason"] == "pending_graph_work"
    assert proof["queues"]["graph"] == 1


def test_inspect_graph_rejects_a_pending_full_rebuild_marker(ready_graph: Path) -> None:
    graph = _load_module()
    deferred_index.mark_graph_full_rebuild(ready_graph, generation=2)

    proof = graph.inspect_graph(ready_graph)

    assert proof["ready"] is False
    assert proof["reason"] == "pending_graph_full_rebuild"
    assert proof["full_rebuild_generation"] == 2


def test_inspect_graph_rejects_an_empty_graph_membership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    root = tmp_path / "vault"
    (root / "Knowledge Base").mkdir(parents=True)
    _seed_live_freshness(root)
    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=1,
        mutation_id="0123456789abcdef01234567",
        paths=(),
        created_paths=(),
        scope="full",
    )
    graph_sync._write_floor(root, graph_sync.GraphSyncGenerationFloor.create(1))
    graph_sync._write_checkpoint(root, checkpoint)
    EpistemicGraphIndex(root).rebuild_all()
    graph = _load_module()

    proof = graph.inspect_graph(root)

    assert proof["ready"] is False
    assert proof["reason"] == "empty_graph_membership"


def test_inspect_graph_rejects_changing_membership(
    ready_graph: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = _load_module()
    actual = graph._source_snapshot
    calls = 0

    def moving_membership(root: Path) -> tuple[dict[str, str], tuple[Any, ...]]:
        nonlocal calls
        calls += 1
        result, guards = actual(root)
        if calls == 2:
            return {**result, "Knowledge Base/Notes/Insights/raced.md": "0" * 64}, guards
        return result, guards

    monkeypatch.setattr(graph, "_source_snapshot", moving_membership)

    proof = graph.inspect_graph(ready_graph)

    assert proof["ready"] is False
    assert proof["reason"] == "membership_changed"


def test_inspect_graph_rejects_a_wrong_expected_link(ready_graph: Path) -> None:
    graph = _load_module()

    proof = graph.inspect_graph(ready_graph, expected_link=(PAGE_B, PAGE_A))

    assert proof["ready"] is False
    assert proof["reason"] == "expected_link_missing"


def test_inspect_graph_rejects_a_neutral_edge_when_supports_is_required(
    ready_graph: Path,
) -> None:
    graph = _load_module()
    (ready_graph / PAGE_A).write_text(
        _page("Tracker", "The tracker links to [[background]]."), encoding="utf-8"
    )
    _seed_live_freshness(ready_graph)
    EpistemicGraphIndex(ready_graph).rebuild_all()

    any_relation_proof = graph.inspect_graph(ready_graph, expected_link=(PAGE_A, PAGE_B))
    proof = graph.inspect_graph(
        ready_graph,
        expected_link=(PAGE_A, PAGE_B),
        expected_relation="supports",
    )

    assert any_relation_proof["ready"] is True
    assert any_relation_proof["expected_link"] == {
        "source": PAGE_A,
        "target": PAGE_B,
        "relation": None,
        "present": True,
    }
    assert proof["ready"] is False
    assert proof["reason"] == "expected_link_missing"
    assert proof["expected_link"] == {
        "source": PAGE_A,
        "target": PAGE_B,
        "relation": "supports",
        "present": False,
    }


def test_inspect_graph_opens_sqlite_read_only(ready_graph: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    graph = _load_module()
    calls: list[tuple[str, bool]] = []
    connect = sqlite3.connect

    def record_connect(database: str, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        calls.append((database, bool(kwargs.get("uri"))))
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(graph.sqlite3, "connect", record_connect)

    proof = graph.inspect_graph(ready_graph)

    assert proof["ready"] is True
    assert any("mode=ro" in database and uri for database, uri in calls)


def test_inspect_graph_rechecks_source_guards_after_the_scan(
    ready_graph: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = _load_module()
    calls = 0
    recheck = vault_module.PathGuard.recheck

    def record_recheck(self: Any, root: Path) -> None:
        nonlocal calls
        calls += 1
        recheck(self, root)

    monkeypatch.setattr(vault_module.PathGuard, "recheck", record_recheck)

    proof = graph.inspect_graph(ready_graph)

    assert proof["ready"] is True
    assert calls >= 8
