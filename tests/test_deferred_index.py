from __future__ import annotations

from pathlib import Path

from exomem import deferred_index


def test_deferred_paths_are_durable_deduplicated_and_clearable(vault: Path) -> None:
    rel = "Knowledge Base/Notes/deferred.md"
    assert deferred_index.add(vault, [rel, rel]) == 1
    assert deferred_index.add(vault, [rel]) == 0
    assert deferred_index.status(vault)["count"] == 1

    # A fresh read from SQLite represents process restart recovery.
    assert deferred_index.list_paths(vault) == [rel]
    assert deferred_index.clear(vault, [rel]) == 1
    assert deferred_index.status(vault)["count"] == 0


def test_deferred_status_does_not_create_sidecar(vault: Path) -> None:
    path = deferred_index.store_path(vault)
    assert not path.exists()
    assert deferred_index.status(vault)["count"] == 0
    assert not path.exists()
