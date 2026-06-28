"""file_watcher — debounce/dispatch LOGIC tested directly (no real watchdog observer).

We stub embeddings.upsert_after_write / delete_after_remove and feed change events,
asserting the watcher coalesces them into one batched dispatch with the right paths.
The soft-fail path (watchdog import fails → start() is a no-op) is tested by patching
the lazy import.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from kb_mcp import embeddings, file_watcher


def _stub_embeddings(monkeypatch: pytest.MonkeyPatch):
    ups: list[list[Path]] = []
    dels: list[list[str]] = []
    monkeypatch.setattr(embeddings, "upsert_after_write", lambda root, paths: ups.append(list(paths)))
    monkeypatch.setattr(embeddings, "delete_after_remove", lambda root, rels: dels.append(list(rels)))
    return ups, dels


def test_flush_batches_upserts_and_dedupes(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    ups, dels = _stub_embeddings(monkeypatch)
    w = file_watcher.FileWatcher(vault)
    a = vault / "Knowledge Base" / "Notes" / "a.md"
    b = vault / "Knowledge Base" / "Notes" / "b.md"
    w._record(a, deleted=False)
    w._record(a, deleted=False)  # duplicate save coalesces
    w._record(b, deleted=False)
    w._flush()
    assert len(ups) == 1, "one batched upsert call for the whole window"
    assert sorted(ups[0]) == sorted([a, b])
    assert dels == []
    # Pending cleared after flush — a second flush dispatches nothing.
    w._flush()
    assert len(ups) == 1


def test_non_markdown_is_ignored(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    ups, dels = _stub_embeddings(monkeypatch)
    w = file_watcher.FileWatcher(vault)
    w._record(vault / "Knowledge Base" / "Evidence" / "scan.png", deleted=False)
    w._flush()
    assert ups == [] and dels == []


def test_delete_routes_to_delete_after_remove(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    ups, dels = _stub_embeddings(monkeypatch)
    w = file_watcher.FileWatcher(vault)
    gone = vault / "Knowledge Base" / "Notes" / "gone.md"
    w._record(gone, deleted=True)
    w._flush()
    assert ups == []
    assert dels == [["Knowledge Base/Notes/gone.md"]]


def test_modify_then_delete_only_deletes(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    ups, dels = _stub_embeddings(monkeypatch)
    w = file_watcher.FileWatcher(vault)
    p = vault / "Knowledge Base" / "Notes" / "x.md"
    w._record(p, deleted=False)
    w._record(p, deleted=True)  # deleted within the same window wins
    w._flush()
    assert ups == []
    assert dels == [["Knowledge Base/Notes/x.md"]]


def test_delete_then_recreate_only_upserts(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    ups, dels = _stub_embeddings(monkeypatch)
    w = file_watcher.FileWatcher(vault)
    p = vault / "Knowledge Base" / "Notes" / "y.md"
    w._record(p, deleted=True)
    w._record(p, deleted=False)  # recreated → modify
    w._flush()
    assert dels == []
    assert ups == [[p]]


def test_dispatch_thread_coalesces_within_debounce(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    ups, _dels = _stub_embeddings(monkeypatch)
    w = file_watcher.FileWatcher(vault, debounce_seconds=0.05)
    t = threading.Thread(target=w._run_dispatch, daemon=True)
    t.start()
    try:
        a = vault / "Knowledge Base" / "Notes" / "a.md"
        b = vault / "Knowledge Base" / "Notes" / "b.md"
        w._record(a, deleted=False)
        w._record(b, deleted=False)
        deadline = time.monotonic() + 2.0
        while not ups and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ups, "dispatch thread should flush after the debounce window"
        assert sorted(ups[0]) == sorted([a, b]), "rapid saves coalesce into one batch"
    finally:
        w._stop.set()
        w._wake.set()
        t.join(timeout=2)


def test_start_soft_fails_when_watchdog_missing(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom():
        raise ImportError("No module named 'watchdog'")

    monkeypatch.setattr(file_watcher, "_import_watchdog", _boom)
    w = file_watcher.FileWatcher(vault)
    assert w.start() is False  # no-op, server keeps running
    assert w._thread is None and w._observer is None


def test_start_no_op_when_kb_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # watchdog "available" but no Knowledge Base/ dir → don't watch.
    monkeypatch.setattr(file_watcher, "_import_watchdog", lambda: (object, object))
    w = file_watcher.FileWatcher(tmp_path)
    assert w.start() is False
