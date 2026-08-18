"""The fan-out event that has to explain itself."""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import file_watcher


def test_the_event_names_the_state_a_diagnosis_would_have_had_to_infer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Epoch, state, generation, queue depth and scope -- in one line.

    Root-causing an unclassified incompleteness previously meant reading the
    graph's sidecars after the fact to reconstruct exactly these.
    """
    from exomem import deferred_index, graph_sync

    monkeypatch.setattr(
        graph_sync, "classify_epoch", lambda _root: type("E", (), {"kind": "coherent"})()
    )
    monkeypatch.setattr(
        graph_sync, "status", lambda _root: {"state": "recovery_required", "generation": 2}
    )
    monkeypatch.setattr(deferred_index, "graph_status", lambda _root: {"count": 13})
    monkeypatch.setattr(deferred_index, "graph_full_rebuild_pending", lambda _root: 2)

    fields = file_watcher._graph_incompleteness_fields(tmp_path)

    assert fields == "epoch=coherent state=recovery_required generation=2 queued=13 scope=full"


def test_a_known_path_list_reports_a_path_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`scope` is the field that separates the two repair regimes.

    A whole-vault marker means the changed scope could not be determined, which
    is the expensive regime; a path list is the proportional one.
    """
    from exomem import deferred_index, graph_sync

    monkeypatch.setattr(
        graph_sync, "classify_epoch", lambda _root: type("E", (), {"kind": "recoverable"})()
    )
    monkeypatch.setattr(
        graph_sync, "status", lambda _root: {"state": "unavailable", "generation": 7}
    )
    monkeypatch.setattr(deferred_index, "graph_status", lambda _root: {"count": 4})
    monkeypatch.setattr(deferred_index, "graph_full_rebuild_pending", lambda _root: None)

    assert "scope=paths" in file_watcher._graph_incompleteness_fields(tmp_path)
    assert "queued=4" in file_watcher._graph_incompleteness_fields(tmp_path)


def test_one_unreadable_field_never_costs_the_others_or_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Diagnostics must never be the reason a drain fails.

    The branch this runs in has already decided to re-arm periodic recovery; an
    exception here would replace a re-armed recovery with a lost one.
    """
    from exomem import deferred_index, graph_sync

    def explode(_root: Path) -> object:
        raise RuntimeError("sidecar unreadable")

    monkeypatch.setattr(graph_sync, "classify_epoch", explode)
    monkeypatch.setattr(
        graph_sync, "status", lambda _root: {"state": "current", "generation": 1}
    )
    monkeypatch.setattr(deferred_index, "graph_status", explode)
    monkeypatch.setattr(deferred_index, "graph_full_rebuild_pending", lambda _root: None)

    fields = file_watcher._graph_incompleteness_fields(tmp_path)

    assert "epoch=?" in fields
    assert "queued=?" in fields
    assert "state=current" in fields
    assert "generation=1" in fields
    assert "scope=paths" in fields


def test_a_failed_status_read_degrades_both_fields_it_feeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`state` and `generation` come from one read, so one failure marks both.

    They are taken together deliberately -- describing the situation should not
    cost two walks of the sidecars being described -- which makes it worth
    pinning that the shared read degrades honestly rather than reporting a
    generation it never obtained.
    """
    from exomem import deferred_index, graph_sync

    def explode(_root: Path) -> object:
        raise RuntimeError("sidecar unreadable")

    monkeypatch.setattr(graph_sync, "status", explode)
    monkeypatch.setattr(
        graph_sync, "classify_epoch", lambda _root: type("E", (), {"kind": "coherent"})()
    )
    monkeypatch.setattr(deferred_index, "graph_status", lambda _root: {"count": 2})
    monkeypatch.setattr(deferred_index, "graph_full_rebuild_pending", lambda _root: None)

    fields = file_watcher._graph_incompleteness_fields(tmp_path)

    assert fields == "epoch=coherent state=? generation=? queued=2 scope=paths"
