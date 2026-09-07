"""Foreground-priority checkpoint integration seams."""

from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
import threading

import pytest

from exomem import due_state, epistemic_graph, find_corpus, foreground_activity, graph_sync, recall_policy


def test_admission_and_parse_checkpoints_are_inert_without_background_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    page = vault / "Knowledge Base" / "Notes" / "page.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Page\n", encoding="utf-8")
    calls: list[Path] = []
    monkeypatch.setattr(foreground_activity, "checkpoint", lambda root: calls.append(Path(root)))

    recall_policy.is_recall_candidate(vault, page)
    find_corpus.parse_page(page, 0.0, vault)

    assert calls == [vault, vault]


def test_registered_graph_builder_enters_background_scope(tmp_path: Path, monkeypatch) -> None:
    entered: list[Path] = []

    @contextmanager
    def scope(root: Path, **_kwargs):
        entered.append(Path(root))
        yield

    monkeypatch.setattr(foreground_activity, "background_scope", scope)
    coordinator = graph_sync.GraphRebuildCoordinator(tmp_path)
    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=1, scope="full", mutation_id="a" * 24, paths=(), created_paths=()
    )
    waiter = coordinator.start_or_join(
        checkpoint, lambda required: graph_sync.GraphBuildOutcome.covering(required)
    )
    assert waiter.wait(1).covers(checkpoint)
    assert entered == [tmp_path.resolve()]


def test_missing_graph_and_due_state_workers_enter_background_scope(
    tmp_path: Path, monkeypatch
) -> None:
    entered: list[Path] = []
    graph_done = threading.Event()
    due_done = threading.Event()

    @contextmanager
    def scope(root: Path, **_kwargs):
        entered.append(Path(root))
        yield

    class Index:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def rebuild_all(self) -> None:
            graph_done.set()

    monkeypatch.setattr(foreground_activity, "background_scope", scope)
    monkeypatch.setattr(epistemic_graph, "EpistemicGraphIndex", Index)
    monkeypatch.setattr(epistemic_graph, "graph_scheduling_enabled", lambda: True)
    monkeypatch.setattr(epistemic_graph, "publication_refusal_active", lambda _root: False)
    monkeypatch.setattr(due_state, "reconcile", lambda _root, **_kwargs: due_done.set())
    monkeypatch.delenv("EXOMEM_SYNC_DUE_STATE_WARM", raising=False)
    assert epistemic_graph.schedule_background_rebuild(tmp_path)
    assert due_state._schedule_reconcile(tmp_path, today=due_state.dt.date.today()) is None
    assert graph_done.wait(1) and due_done.wait(1)
    assert entered.count(tmp_path) == 2
