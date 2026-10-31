"""Every write class still reaches the watcher through its event filter.

The observer schedule passes watchdog the event classes the handler consumes.
On Linux that filter also narrows the inotify mask, so page reads never reach
the process. A filter, or a watchdog release, that maps one write class to no
delivered event loses that write without an error. These cases drive the real
observer with real filesystem operations, one per write class.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from exomem import file_watcher

_WAIT_SECONDS = 10.0


@pytest.fixture
def observed(vault: Path, monkeypatch: pytest.MonkeyPatch):
    """A started watcher whose file records and directory notes are captured."""
    monkeypatch.setattr(file_watcher.freshness, "event_indexes_enabled", lambda: False)
    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True, exist_ok=True)
    page = notes / "filter-probe.md"
    page.write_text("# Probe\n\nbefore\n", encoding="utf-8")
    seen: list[tuple[str, Path, bool]] = []
    arrived = threading.Condition()

    def record(path: Path, *, deleted: bool, notify_views: bool = True) -> None:  # noqa: ARG001
        with arrived:
            seen.append(("file", Path(path), deleted))
            arrived.notify_all()

    def note_directories(*paths: str | bytes) -> None:
        with arrived:
            seen.extend(("dir", Path(os.fsdecode(path)), False) for path in paths)
            arrived.notify_all()

    watcher = file_watcher.FileWatcher(vault, debounce_seconds=0.01)
    monkeypatch.setattr(watcher, "_record", record)
    monkeypatch.setattr(watcher, "_note_directories", note_directories)
    assert watcher.start() is True

    def wait_for(kind: str, path: Path, deleted: bool, *, times: int = 1) -> bool:
        with arrived:
            return arrived.wait_for(
                lambda: seen.count((kind, path, deleted)) >= times, _WAIT_SECONDS
            )

    try:
        yield page, wait_for
    finally:
        watcher.stop()


def test_an_atomic_replace_reaches_the_watcher(observed) -> None:
    page, wait_for = observed
    staged = page.with_name(".filter-probe.md.tmp")
    staged.write_text("# Probe\n\nreplaced\n", encoding="utf-8")
    os.replace(staged, page)

    assert wait_for("file", page, False), "a rename into place was not recorded"


def test_an_in_place_write_reaches_the_watcher(observed) -> None:
    page, wait_for = observed
    with page.open("r+", encoding="utf-8") as handle:
        handle.seek(0, os.SEEK_END)
        handle.write("appended\n")

    assert wait_for("file", page, False), "an in-place write was not recorded"


def test_a_delete_reaches_the_watcher(observed) -> None:
    page, wait_for = observed
    page.unlink()

    assert wait_for("file", page, True), "a delete was not recorded"


def test_a_new_directory_and_its_page_reach_the_watcher(observed) -> None:
    page, wait_for = observed
    folder = page.parent / "new-folder"
    folder.mkdir()
    created = folder / "created.md"
    created.write_text("# Created\n", encoding="utf-8")

    assert wait_for("dir", folder, False), "a new directory was not noted"
    assert wait_for("file", created, False), "a page in a new directory was not recorded"


def test_a_deleted_directory_reaches_the_watcher(observed) -> None:
    page, wait_for = observed
    folder = page.parent / "deleted-folder"
    folder.mkdir()
    assert wait_for("dir", folder, False), "a new directory was not noted"
    folder.rmdir()

    # The creation noted the directory once; the delete must note it again.
    assert wait_for("dir", folder, False, times=2), "a deleted directory was not noted"


def test_a_moved_directory_moves_its_pages_for_the_watcher(observed) -> None:
    page, wait_for = observed
    source = page.parent / "move-source"
    source.mkdir()
    child = source / "child.md"
    child.write_text("# Child\n", encoding="utf-8")
    assert wait_for("file", child, False)
    destination = page.parent / "move-destination"
    os.rename(source, destination)

    assert wait_for("dir", destination, False), "a moved directory was not noted"
    assert wait_for("file", child, True), "a moved directory left its old page recorded"
    assert wait_for("file", destination / "child.md", False), (
        "a moved directory did not record its page at the new path"
    )
