"""file_watcher — debounce/dispatch LOGIC tested directly (no real watchdog observer).

We stub embeddings.upsert_after_write / delete_after_remove and feed change events,
asserting the watcher coalesces them into one batched dispatch with the right paths.
The soft-fail path (watchdog import fails → start() is a no-op) is tested by patching
the lazy import.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import pytest

from exomem import embeddings, file_watcher, media_processing
from exomem import find as find_module


def _stub_embeddings(monkeypatch: pytest.MonkeyPatch):
    ups: list[list[Path]] = []
    dels: list[list[str]] = []

    def upsert_status(root, paths):
        ups.append(list(paths))
        return embeddings.EmbeddingSyncStatus(
            "completed", "embedding_upsert_completed", len(paths)
        )

    def delete_status(root, rels):
        dels.append(list(rels))
        return embeddings.EmbeddingSyncStatus(
            "completed", "embedding_delete_completed", len(rels)
        )

    monkeypatch.setattr(embeddings, "upsert_after_write_status", upsert_status)
    monkeypatch.setattr(embeddings, "delete_after_remove_status", delete_status)
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


# ---- Automatic governed-media dispatch (OpenSpec: automatic-media-processing) ----


def _spy_media_and_text_dispatch(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Capture the separate media leaf and every Markdown freshness/index seam."""
    calls: dict[str, list] = {
        "media": [],
        "freshness": [],
        "inbound": [],
        "resolver": [],
        "upsert": [],
        "delete": [],
    }

    def reconcile_media(root: Path, path: Path, *, explicit: bool = True) -> None:
        calls["media"].append((root, path, explicit))

    monkeypatch.setattr(media_processing, "reconcile_media", reconcile_media)
    monkeypatch.setattr(
        file_watcher.freshness,
        "on_files_changed",
        lambda root, changed, deleted: calls["freshness"].append(
            (root, list(changed), list(deleted))
        ),
    )
    monkeypatch.setattr(
        file_watcher.index_sync,
        "upsert_after_write",
        lambda root, paths, **kwargs: calls["upsert"].append(
            (root, list(paths), dict(kwargs))
        ),
    )
    monkeypatch.setattr(
        file_watcher.index_sync,
        "delete_after_remove",
        lambda root, rels: calls["delete"].append((root, list(rels))),
    )
    monkeypatch.setattr(
        "exomem.vault.on_inbound_files_changed",
        lambda root, up, deleted: calls["inbound"].append(
            (root, list(up), list(deleted))
        ),
    )
    monkeypatch.setattr(
        "exomem.find.on_resolver_files_changed",
        lambda root, up, deleted: calls["resolver"].append(
            (root, list(up), list(deleted))
        ),
    )
    return calls


def test_supported_audio_event_dispatches_media_only(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy_media_and_text_dispatch(monkeypatch)
    recording = vault / "Knowledge Base" / "Evidence" / "Audio" / "field-note.M4A"
    recording.parent.mkdir(parents=True, exist_ok=True)
    recording.write_bytes(b"direct watcher audio")
    watcher = file_watcher.FileWatcher(vault)

    watcher._record(recording, deleted=False)
    watcher._flush()

    assert calls["media"] == [(vault, recording, False)]
    assert calls["inbound"] == []
    assert calls["resolver"] == []


def test_supported_audio_event_reconciles_under_writer_authority(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    from exomem import writer_lease

    depth = 0

    class Manager:
        @contextmanager
        def mutation_guard(self, root: Path):
            nonlocal depth
            assert root == vault
            depth += 1
            try:
                yield
            finally:
                depth -= 1

    monkeypatch.setattr(writer_lease, "get_manager", lambda: Manager())
    monkeypatch.setattr(
        media_processing,
        "reconcile_media",
        lambda *_a, **_kw: depth == 1
        or pytest.fail("watcher media reconciliation escaped mutation guard"),
    )
    recording = vault / "Knowledge Base" / "Evidence" / "Audio" / "guarded.m4a"
    recording.parent.mkdir(parents=True, exist_ok=True)
    recording.write_bytes(b"audio")

    watcher = file_watcher.FileWatcher(vault)
    watcher._record(recording, deleted=False)
    watcher._flush()

    assert depth == 0


def test_supported_audio_never_enters_markdown_freshness_or_embedding(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy_media_and_text_dispatch(monkeypatch)
    recording = vault / "Knowledge Base" / "Evidence" / "Audio" / "binary-only.wav"
    recording.parent.mkdir(parents=True, exist_ok=True)
    recording.write_bytes(b"binary audio must not be treated as markdown")
    watcher = file_watcher.FileWatcher(vault)

    watcher._record(recording, deleted=False)
    watcher._flush()

    assert calls["media"] == [(vault, recording, False)]
    assert calls["freshness"] == []
    assert calls["inbound"] == []
    assert calls["resolver"] == []
    assert calls["upsert"] == []
    assert calls["delete"] == []


def test_supported_audio_events_are_debounced_and_deduplicated(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy_media_and_text_dispatch(monkeypatch)
    recording = vault / "Knowledge Base" / "Evidence" / "Audio" / "burst.m4a"
    recording.parent.mkdir(parents=True, exist_ok=True)
    recording.write_bytes(b"one recording, several filesystem events")
    watcher = file_watcher.FileWatcher(vault, debounce_seconds=0.02)
    dispatch = threading.Thread(target=watcher._run_dispatch, daemon=True)
    dispatch.start()
    try:
        watcher._record(recording, deleted=False)
        watcher._record(recording, deleted=False)
        watcher._record(recording, deleted=False)
        deadline = time.monotonic() + 2.0
        while not calls["media"] and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        watcher._stop.set()
        watcher._wake.set()
        dispatch.join(timeout=2)

    assert calls["media"] == [(vault, recording, False)]
    assert calls["freshness"] == []
    assert calls["inbound"] == []
    assert calls["resolver"] == []
    assert calls["upsert"] == []


def test_unsupported_attachment_dispatches_neither_media_nor_text(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy_media_and_text_dispatch(monkeypatch)
    attachment = vault / "Knowledge Base" / "Evidence" / "payload.bin"
    attachment.parent.mkdir(parents=True, exist_ok=True)
    attachment.write_bytes(b"unsupported attachment")
    watcher = file_watcher.FileWatcher(vault)

    watcher._record(attachment, deleted=False)
    watcher._flush()

    assert calls == {
        "media": [],
        "freshness": [],
        "inbound": [],
        "resolver": [],
        "upsert": [],
        "delete": [],
    }


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


def test_file_watcher_reads_policy_without_restart(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    w = file_watcher.FileWatcher(vault)

    assert w._debounce_seconds() == pytest.approx(2.0)
    assert w._reconcile_interval_seconds() == pytest.approx(900.0)

    monkeypatch.setenv("EXOMEM_MODE", "normal")

    assert w._debounce_seconds() == pytest.approx(0.5)
    assert w._reconcile_interval_seconds() == pytest.approx(300.0)


def test_live_import_burst_defers_semantic_indexing(
    vault, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[tuple[list[Path], dict]] = []
    monkeypatch.setattr(
        file_watcher.index_sync,
        "upsert_after_write",
        lambda root, paths, **kw: calls.append((list(paths), dict(kw))),
    )
    monkeypatch.setattr(
        file_watcher.mode,
        "watcher_policy",
        lambda: file_watcher.mode.WatcherPolicy(
            debounce_seconds=0.05,
            reconcile_interval_seconds=999.0,
            max_embed_files_per_batch=1,
            max_reconcile_embed_files=500,
            defer_expensive_indexes=False,
        ),
    )
    w = file_watcher.FileWatcher(vault)
    a = vault / "Knowledge Base" / "Notes" / "burst-a.md"
    b = vault / "Knowledge Base" / "Notes" / "burst-b.md"
    with caplog.at_level(logging.WARNING, logger="exomem.file_watcher"):
        w._record(a, deleted=False)
        w._record(b, deleted=False)
        w._flush()

    assert len(calls) == 1
    assert sorted(calls[0][0]) == sorted([a, b])
    assert calls[0][1] == {"defer_semantic": True}
    assert "live import/sync burst" in caplog.text



def test_dispatch_thread_uses_quiet_policy_for_burst_coalescing(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[Path]] = []
    monkeypatch.setattr(
        file_watcher.index_sync,
        "upsert_after_write",
        lambda root, paths, **_kw: calls.append(list(paths)),
    )
    monkeypatch.setattr(
        file_watcher.mode,
        "watcher_policy",
        lambda: file_watcher.mode.WatcherPolicy(
            debounce_seconds=0.05,
            reconcile_interval_seconds=999.0,
            max_embed_files_per_batch=0,
            max_reconcile_embed_files=0,
            defer_expensive_indexes=True,
        ),
    )
    w = file_watcher.FileWatcher(vault)
    t = threading.Thread(target=w._run_dispatch, daemon=True)
    t.start()
    try:
        a = vault / "Knowledge Base" / "Notes" / "quiet-a.md"
        b = vault / "Knowledge Base" / "Notes" / "quiet-b.md"
        w._record(a, deleted=False)
        time.sleep(0.01)
        w._record(b, deleted=False)
        deadline = time.monotonic() + 2.0
        while not calls and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(calls) == 1
        assert sorted(calls[0]) == sorted([a, b])
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


def test_file_watcher_dispatch_thread_restarts_after_stop(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hosted quiesce/resume reuses one watcher object; restart must be real."""

    class Handler:
        pass

    class Observer:
        def schedule(self, *_args, **_kwargs) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def join(self, timeout: float | None = None) -> None:
            pass

    monkeypatch.setattr(file_watcher, "_import_watchdog", lambda: (Observer, Handler))
    monkeypatch.setattr(file_watcher.freshness, "event_indexes_enabled", lambda: False)
    watcher = file_watcher.FileWatcher(vault, debounce_seconds=0.01)

    assert watcher.start() is True
    watcher.stop()
    assert watcher.start() is True
    try:
        assert watcher._thread is not None
        time.sleep(0.02)
        assert watcher._thread.is_alive(), "resumed dispatch thread exited on the stale stop event"
    finally:
        watcher.stop()


# ---- Self-write suppression (OpenSpec: improve-find-latency-token-cost) ----


def test_self_write_upsert_suppressed(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    ups, dels = _stub_embeddings(monkeypatch)
    file_watcher.clear_self_write_registry()
    w = file_watcher.FileWatcher(vault)
    p = vault / "Knowledge Base" / "Notes" / "self-write.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# self write\n", encoding="utf-8")
    file_watcher.register_self_write(vault, [p])
    w._record(p, deleted=False)
    w._flush()
    assert ups == [] and dels == []


def test_external_edit_after_self_write_dispatches(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    ups, _dels = _stub_embeddings(monkeypatch)
    file_watcher.clear_self_write_registry()
    w = file_watcher.FileWatcher(vault)
    p = vault / "Knowledge Base" / "Notes" / "self-then-external.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# self write\n", encoding="utf-8")
    file_watcher.register_self_write(vault, [p])
    # A later EXTERNAL edit changes the file signature — must dispatch.
    p.write_text("# self write\n\nexternally edited, longer now\n", encoding="utf-8")
    w._record(p, deleted=False)
    w._flush()
    assert ups and p in ups[0]


def test_upsert_suppression_expires(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    ups, _dels = _stub_embeddings(monkeypatch)
    file_watcher.clear_self_write_registry()
    monkeypatch.setattr(file_watcher, "UPSERT_SUPPRESS_TTL_SECONDS", -1.0)
    w = file_watcher.FileWatcher(vault)
    p = vault / "Knowledge Base" / "Notes" / "expired-suppression.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# expired\n", encoding="utf-8")
    file_watcher.register_self_write(vault, [p])
    w._record(p, deleted=False)
    w._flush()
    assert ups and p in ups[0]


def test_self_delete_suppressed(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    ups, dels = _stub_embeddings(monkeypatch)
    file_watcher.clear_self_write_registry()
    w = file_watcher.FileWatcher(vault)
    rel = "Knowledge Base/Notes/self-deleted.md"
    file_watcher.register_self_delete(vault, [rel])
    w._record(vault / rel, deleted=True)
    w._flush()
    assert ups == [] and dels == []


def test_delete_suppression_expires(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    _ups, dels = _stub_embeddings(monkeypatch)
    file_watcher.clear_self_write_registry()
    monkeypatch.setattr(file_watcher, "DELETE_SUPPRESS_TTL_SECONDS", -1.0)
    w = file_watcher.FileWatcher(vault)
    rel = "Knowledge Base/Notes/expired-delete.md"
    file_watcher.register_self_delete(vault, [rel])
    w._record(vault / rel, deleted=True)
    w._flush()
    assert dels == [[rel]]


def test_unregistered_external_events_still_dispatch(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    ups, dels = _stub_embeddings(monkeypatch)
    file_watcher.clear_self_write_registry()
    w = file_watcher.FileWatcher(vault)
    p = vault / "Knowledge Base" / "Notes" / "external-edit.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# external\n", encoding="utf-8")
    w._record(p, deleted=False)
    gone = vault / "Knowledge Base" / "Notes" / "external-gone.md"
    w._record(gone, deleted=True)
    w._flush()
    assert ups and p in ups[0]
    assert dels and "Knowledge Base/Notes/external-gone.md" in dels[0]


def test_batch_atomic_write_registers_suppression(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem.vault import PlannedWrite, batch_atomic_write

    ups, _dels = _stub_embeddings(monkeypatch)
    file_watcher.clear_self_write_registry()
    w = file_watcher.FileWatcher(vault)
    p = vault / "Knowledge Base" / "Notes" / "batch-written.md"
    batch_atomic_write([PlannedWrite(path=p, content="# batch\n")], vault_root=vault)
    ups.clear()  # the writer's own (stubbed) upsert — not the echo under test
    w._record(p, deleted=False)
    w._flush()
    assert ups == []


# ---- Reconcile drift dispatch through the event fan-out (PR1) ----------------
#
# The 300s reconcile only re-derives the freshness map from a fresh walk. When a
# watchdog event is missed, that drift used to force every triple-keyed derived
# index (resolver, bm25, keyword) to rebuild lazily on the NEXT query — a
# multi-second first-query-after-drift stall — and never re-embedded the missed
# files (a recall gap). PR1 makes reconcile return the drift delta and the
# watcher dispatch it through the SAME fan-out a live batch uses, off the query
# path. We drive drift by writing/utime WITHOUT _record (a missed event), then
# call _reconcile_once directly (the pattern in test_freshness_registry.py:215).


def _spy_reconcile_fanout(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Capture the reconcile dispatch seams: inbound + resolver publishes, the
    KB-filtered embed heal (index_sync), and the bm25 pre-warm."""
    calls: dict[str, list] = {
        "inbound": [], "resolver": [], "upsert": [], "delete": [], "warm": [],
    }
    monkeypatch.setattr(
        "exomem.vault.on_inbound_files_changed",
        lambda root, up, dl: calls["inbound"].append((list(up), list(dl))),
    )
    monkeypatch.setattr(
        "exomem.find.on_resolver_files_changed",
        lambda root, up, dl: calls["resolver"].append((list(up), list(dl))),
    )
    monkeypatch.setattr(
        file_watcher.index_sync, "upsert_after_write",
        lambda root, paths, **_kw: calls["upsert"].append(list(paths)),
    )
    monkeypatch.setattr(
        file_watcher.index_sync, "delete_after_remove",
        lambda root, rels: calls["delete"].append(list(rels)),
    )
    monkeypatch.setattr("exomem.bm25.warm", lambda root, scope: calls["warm"].append(scope))
    return calls


def _vault_rel(vault: Path, path: Path) -> str:
    return path.resolve().relative_to(vault.resolve()).as_posix()


def test_reconcile_dispatches_drift_delta_to_fanout(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    file_watcher.clear_self_write_registry()
    calls = _spy_reconcile_fanout(monkeypatch)
    w = file_watcher.FileWatcher(vault)
    w._reconcile_once(seed=True)

    target = next(find_module._walk_md(vault / "Knowledge Base"))
    future = time.time() + 10_000
    os.utime(target, (future, future))  # a missed watchdog event

    w._reconcile_once(seed=False)

    rel = _vault_rel(vault, target)
    # The exact delta reaches inbound + resolver once, no phantom deletes.
    assert calls["inbound"] == [([rel], [])]
    assert calls["resolver"] == [([rel], [])]
    # The KB file is handed to the embed/lexical heal exactly once (deduped
    # across the kb + vault scopes).
    assert len(calls["upsert"]) == 1
    assert [p.resolve() for p in calls["upsert"][0]] == [target.resolve()]
    assert calls["delete"] == []
    # bm25 corpus pre-warmed for both scopes off the query path.
    assert calls["warm"] == ["kb", "vault"]


def test_quiet_reconcile_defers_embedding_and_skips_bm25_warm(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    file_watcher.clear_self_write_registry()
    calls = _spy_reconcile_fanout(monkeypatch)
    w = file_watcher.FileWatcher(vault)
    w._reconcile_once(seed=True)

    target = next(find_module._walk_md(vault / "Knowledge Base"))
    future = time.time() + 10_000
    os.utime(target, (future, future))

    w._reconcile_once(seed=False)

    assert len(calls["upsert"]) == 1
    assert [p.resolve() for p in calls["upsert"][0]] == [target.resolve()]
    assert calls["warm"] == []


def test_reconcile_seed_dispatches_nothing(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    """Boot seed is NOT drift — it must not re-embed the whole vault."""
    file_watcher.clear_self_write_registry()
    calls = _spy_reconcile_fanout(monkeypatch)
    w = file_watcher.FileWatcher(vault)

    w._reconcile_once(seed=True)

    assert calls == {"inbound": [], "resolver": [], "upsert": [], "delete": [], "warm": []}


def test_reconcile_no_drift_dispatches_nothing(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    file_watcher.clear_self_write_registry()
    calls = _spy_reconcile_fanout(monkeypatch)
    w = file_watcher.FileWatcher(vault)
    w._reconcile_once(seed=True)

    w._reconcile_once(seed=False)  # nothing changed on disk since the seed

    assert calls == {"inbound": [], "resolver": [], "upsert": [], "delete": [], "warm": []}


def test_periodic_reconcile_discovers_missed_media_without_text_reembed(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    discovery_calls: list[tuple[Path, int]] = []

    def reconcile_all_media(root: Path, *, limit: int) -> None:
        discovery_calls.append((root, limit))

    monkeypatch.setattr(
        media_processing,
        "reconcile_all_media",
        reconcile_all_media,
        raising=False,
    )
    calls = _spy_reconcile_fanout(monkeypatch)
    watcher = file_watcher.FileWatcher(vault)
    watcher._reconcile_once(seed=True)
    discovery_calls.clear()

    recording = vault / "Knowledge Base" / "Evidence" / "Audio" / "missed.m4a"
    recording.parent.mkdir(parents=True, exist_ok=True)
    recording.write_bytes(b"event missed while watcher was disconnected")
    watcher._reconcile_once(seed=False)

    assert len(discovery_calls) == 1
    root, limit = discovery_calls[0]
    assert root == vault
    assert isinstance(limit, int) and limit > 0
    assert calls["inbound"] == []
    assert calls["resolver"] == []
    assert calls["upsert"] == []
    assert calls["delete"] == []


def test_periodic_media_reconcile_runs_under_writer_authority(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    from exomem import writer_lease

    depth = 0

    class Manager:
        @contextmanager
        def mutation_guard(self, root: Path):
            nonlocal depth
            assert root == vault
            depth += 1
            try:
                yield
            finally:
                depth -= 1

    monkeypatch.setattr(writer_lease, "get_manager", lambda: Manager())
    monkeypatch.setattr(
        media_processing,
        "reconcile_all_media",
        lambda *_a, **_kw: depth == 1
        or pytest.fail("periodic media reconciliation escaped mutation guard"),
    )
    calls = _spy_reconcile_fanout(monkeypatch)
    watcher = file_watcher.FileWatcher(vault)
    watcher._reconcile_once(seed=True)
    watcher._reconcile_once(seed=False)

    assert depth == 0
    assert calls["upsert"] == []


def test_reconcile_delete_routes_to_delete_after_remove(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_watcher.clear_self_write_registry()
    calls = _spy_reconcile_fanout(monkeypatch)
    w = file_watcher.FileWatcher(vault)
    w._reconcile_once(seed=True)

    target = next(find_module._walk_md(vault / "Knowledge Base"))
    rel = _vault_rel(vault, target)
    target.unlink()  # a missed delete event

    w._reconcile_once(seed=False)

    assert calls["delete"] == [[rel]]
    assert calls["upsert"] == []
    assert calls["resolver"] == [([], [rel])]
    assert calls["inbound"] == [([], [rel])]


def test_reconcile_reembeds_missed_kb_file(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    """The recall-gap fix: a missed KB edit reaches embeddings.upsert_after_write
    through the real index_sync seam (stubbed embedder, as in _stub_embeddings)."""
    file_watcher.clear_self_write_registry()
    ups, dels = _stub_embeddings(monkeypatch)
    monkeypatch.setattr("exomem.bm25.warm", lambda root, scope: None)
    w = file_watcher.FileWatcher(vault)
    w._reconcile_once(seed=True)

    target = next(find_module._walk_md(vault / "Knowledge Base"))
    future = time.time() + 10_000
    os.utime(target, (future, future))

    w._reconcile_once(seed=False)

    embedded = [p for batch in ups for p in batch]
    assert any(p.resolve() == target.resolve() for p in embedded)
    assert dels == []


def test_reconcile_restamps_resolver_triple_no_rebuild(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 11.3s fix: after reconcile dispatch, the cached resolver's freshness
    triple is restamped, so the next _get_query_resolver HITS the same instance
    instead of a full-vault rebuild."""
    file_watcher.clear_self_write_registry()
    _stub_embeddings(monkeypatch)  # keep torch out; lexstore/resolver run for real
    monkeypatch.setattr("exomem.bm25.warm", lambda root, scope: None)
    w = file_watcher.FileWatcher(vault)
    w._reconcile_once(seed=True)

    # Prime the process-shared resolver at the current freshness triple.
    r1 = find_module._get_query_resolver(vault)

    target = next(find_module._walk_md(vault / "Knowledge Base"))
    future = time.time() + 10_000
    os.utime(target, (future, future))

    w._reconcile_once(seed=False)

    r2 = find_module._get_query_resolver(vault)
    assert r2 is r1, "reconcile must restamp the resolver triple, not force a rebuild"


def test_reconcile_dispatch_suppresses_registered_self_write(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path that was a registered self-write (already fanned out by the writer)
    must not be re-dispatched by reconcile drift."""
    file_watcher.clear_self_write_registry()
    w = file_watcher.FileWatcher(vault)
    p = vault / "Knowledge Base" / "Notes" / "reconcile-self-write.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# self\n", encoding="utf-8")
    file_watcher.register_self_write(vault, [p])  # matching signature registered

    # Spy AFTER register_self_write so the writer's own publish isn't counted.
    ups, dels = _stub_embeddings(monkeypatch)
    inbound: list = []
    resolver: list = []
    monkeypatch.setattr(
        "exomem.vault.on_inbound_files_changed",
        lambda root, up, dl: inbound.append((list(up), list(dl))),
    )
    monkeypatch.setattr(
        "exomem.find.on_resolver_files_changed",
        lambda root, up, dl: resolver.append((list(up), list(dl))),
    )

    w._dispatch_reconcile_delta([str(p)], [])

    assert ups == [] and dels == []
    assert inbound == [] and resolver == []


def test_reconcile_delta_conflict_existing_file_routes_as_changed_only(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kb/vault scope walks are two separate, non-atomic snapshots — a file
    deleted+recreated between them can appear in BOTH the changed and deleted
    delta lists in the same cycle. Split-brain must resolve by trusting the
    filesystem now: a path that exists is dispatched as changed ONLY, never
    also as a delete (a delete-after-upsert would strip a live file's index
    rows until the next drift cycle re-surfaces it)."""
    file_watcher.clear_self_write_registry()
    ups, dels = _stub_embeddings(monkeypatch)
    w = file_watcher.FileWatcher(vault)
    p = vault / "Knowledge Base" / "Notes" / "split-brain-exists.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# split brain\n", encoding="utf-8")

    w._dispatch_reconcile_delta([str(p)], [str(p)])

    assert len(ups) == 1 and ups[0] == [p]
    assert dels == []


def test_reconcile_delta_conflict_missing_file_routes_as_deleted_only(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirror case: a path present in both lists that is ABSENT on disk must
    dispatch as a delete only, never also as an upsert."""
    file_watcher.clear_self_write_registry()
    ups, dels = _stub_embeddings(monkeypatch)
    w = file_watcher.FileWatcher(vault)
    p = vault / "Knowledge Base" / "Notes" / "split-brain-gone.md"
    # Deliberately never created — absent on disk.

    w._dispatch_reconcile_delta([str(p)], [str(p)])

    assert ups == []
    assert dels == [["Knowledge Base/Notes/split-brain-gone.md"]]


# ---- Rel-level dispatch guard for dual-form path collapse (#126) ----------
#
# The abs-string guard above only catches a conflict when both event forms are
# the literal SAME string. Two DIFFERENT abs-path forms of one file (e.g. a
# Windows 8.3 short name vs. the long form) evade it but can still collapse to
# the SAME rel once `_rel()` resolves them. We drive that collapse platform-
# free by monkeypatching `_rel` so a distinct "alias" string resolves to the
# same rel a real path would — modeling the 8.3 short-name vector without
# depending on it actually being enabled on the test box.


def test_reconcile_delta_dual_form_collapse_existing_file_routes_as_changed_only(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two different abs-path STRING forms of the SAME on-disk file (e.g. a
    Windows 8.3 short name vs. the long form, #126) don't collide in the
    abs-string guard (different strings) but collapse to the identical rel
    once `_rel()` resolves them. That collapse must still route the live file
    as changed only — never also as a delete that would strip its index
    rows."""
    file_watcher.clear_self_write_registry()
    ups, dels = _stub_embeddings(monkeypatch)
    w = file_watcher.FileWatcher(vault)
    p = vault / "Knowledge Base" / "Notes" / "dual-form-exists.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# dual form\n", encoding="utf-8")

    long_form = str(p)
    alias_form = long_form + ".8dot3-alias"  # a distinct string; never touches disk
    rel = _vault_rel(vault, p)
    real_rel = w._rel

    def fake_rel(path: Path):
        if str(path) == alias_form:
            return rel
        return real_rel(path)

    monkeypatch.setattr(w, "_rel", fake_rel)

    w._dispatch_reconcile_delta([long_form], [alias_form])

    assert len(ups) == 1 and ups[0] == [p]
    assert dels == []


def test_reconcile_delta_dual_form_collapse_missing_file_routes_as_deleted_only(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirror case: the dual-form collapse resolves to a rel whose file is
    genuinely ABSENT — must dispatch as a delete only, never also as an
    upsert."""
    file_watcher.clear_self_write_registry()
    ups, dels = _stub_embeddings(monkeypatch)
    w = file_watcher.FileWatcher(vault)
    p = vault / "Knowledge Base" / "Notes" / "dual-form-gone.md"
    # Deliberately never created — absent on disk.

    long_form = str(p)
    alias_form = long_form + ".8dot3-alias"
    rel = "Knowledge Base/Notes/dual-form-gone.md"
    real_rel = w._rel

    def fake_rel(path: Path):
        if str(path) in (long_form, alias_form):
            return rel
        return real_rel(path)

    monkeypatch.setattr(w, "_rel", fake_rel)

    w._dispatch_reconcile_delta([long_form], [alias_form])

    assert ups == []
    assert dels == [[rel]]
