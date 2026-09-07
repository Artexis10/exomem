"""Foreground-priority checkpoint integration seams."""

from __future__ import annotations

import inspect
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import commands as commands_module
from exomem import (
    due_state,
    epistemic_graph,
    find_corpus,
    foreground_activity,
    graph_sync,
    recall_policy,
    writer_lease,
)
from exomem.governance import egress, projection_runtime, projection_timing


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


def test_dispatcher_foreground_scope_covers_postfilter_and_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    events: list[str] = []

    def leaf(vault_root: Path) -> dict[str, bool]:
        assert vault_root == vault
        assert foreground_activity.foreground_active(vault)
        events.append("leaf")
        return {"leaf": True}

    command = SimpleNamespace(name="find", leaf=leaf, read_only=False, path_roles=())

    class Manager:
        def invoke(self, current, injected, kwargs, **_metadata):
            return current.leaf(*injected, **kwargs)

    @contextmanager
    def completion(_request_class, **_kwargs):
        assert foreground_activity.foreground_active(vault)
        events.append("enter")
        try:
            yield
        finally:
            assert foreground_activity.foreground_active(vault)
            events.append("exit")

    def postfilter(_name, result, root):
        assert root == vault
        assert foreground_activity.foreground_active(vault)
        events.append("postfilter")
        return {"filtered": result["leaf"]}

    monkeypatch.setattr(writer_lease, "get_manager", lambda: Manager())
    monkeypatch.setattr(commands_module, "invocation_is_read_only", lambda *_args: False)
    monkeypatch.setattr(egress, "is_vault_root", lambda value: isinstance(value, Path))
    monkeypatch.setattr(egress, "postfilter", postfilter)
    monkeypatch.setattr(
        projection_runtime, "has_preactivated_projection_runtime", lambda _root: False
    )
    monkeypatch.setattr(
        projection_runtime, "requires_fixed_projected_completion", lambda _root: True
    )
    monkeypatch.setattr(
        projection_timing, "request_class_for_command", lambda *_args: "foreground-test"
    )
    monkeypatch.setattr(projection_timing, "fixed_public_completion", completion)

    assert writer_lease.invoke_command(command, vault) == {"filtered": True}
    assert events == ["enter", "leaf", "postfilter", "exit"]
    assert not foreground_activity.foreground_active(vault)
    assert inspect.signature(writer_lease.invoke_command) == inspect.signature(
        writer_lease.invoke_command.__wrapped__.__wrapped__
    )


def test_dispatcher_foreground_scope_cleans_up_after_base_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    events: list[str] = []

    def leaf(vault_root: Path) -> None:
        assert vault_root == vault
        assert foreground_activity.foreground_active(vault)
        events.append("leaf")
        raise KeyboardInterrupt

    command = SimpleNamespace(name="find", leaf=leaf, read_only=False, path_roles=())

    class Manager:
        def invoke(self, current, injected, kwargs, **_metadata):
            return current.leaf(*injected, **kwargs)

    @contextmanager
    def completion(_request_class, **_kwargs):
        assert foreground_activity.foreground_active(vault)
        events.append("enter")
        try:
            yield
        finally:
            assert foreground_activity.foreground_active(vault)
            events.append("exit")

    def postfilter_error(_name, error, root, **_kwargs):
        assert isinstance(error, KeyboardInterrupt)
        assert root == vault
        assert foreground_activity.foreground_active(vault)
        events.append("postfilter_error")
        return error

    monkeypatch.setattr(writer_lease, "get_manager", lambda: Manager())
    monkeypatch.setattr(commands_module, "invocation_is_read_only", lambda *_args: False)
    monkeypatch.setattr(egress, "is_vault_root", lambda value: isinstance(value, Path))
    monkeypatch.setattr(egress, "postfilter_error", postfilter_error)
    monkeypatch.setattr(
        projection_runtime, "has_preactivated_projection_runtime", lambda _root: False
    )
    monkeypatch.setattr(
        projection_runtime, "requires_fixed_projected_completion", lambda _root: True
    )
    monkeypatch.setattr(
        projection_timing, "request_class_for_command", lambda *_args: "foreground-test"
    )
    monkeypatch.setattr(projection_timing, "fixed_public_completion", completion)

    with pytest.raises(KeyboardInterrupt):
        writer_lease.invoke_command(command, vault)
    assert events == ["enter", "leaf", "postfilter_error", "exit"]
    assert not foreground_activity.foreground_active(vault)
