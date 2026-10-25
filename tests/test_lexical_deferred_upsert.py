"""A contended lexical upsert repairs the page it deferred, not the corpus.

A governed write's incremental lexstore upsert cannot take the publication
barrier while the write's own vault lock is held, so it waits 50ms and gives up.
That much is right: a governed write may not block on the lexical sidecar.

The response was not. One deferred page scheduled a full `rebuild_atomic()` --
O(vault) work from an O(1) cause (#526). Contention says nothing about the
store's health; the rows are fine and the writer simply held the lock. The
background thread, which may wait the full 30s, asks again for exactly those
paths and escalates only if they do not land.
"""

from __future__ import annotations

import errno
import logging
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from exomem import freshness, lexstore


@pytest.fixture
def live_catalogue(tmp_path: Path):
    """A real catalogue and both watcher checkpoints for two ordinary pages."""
    freshness.clear()
    changed = tmp_path / "Knowledge Base" / "changed.md"
    removed = tmp_path / "Knowledge Base" / "removed.md"
    changed.parent.mkdir()
    changed.write_text("# Before\n\nbeforechange\n", encoding="utf-8")
    removed.write_text("# Removed\n\nbeforeremoval\n", encoding="utf-8")
    entries = [(str(path), freshness.stat_signature(path)) for path in (changed, removed)]
    for scope in freshness.SCOPES:
        freshness.seed(tmp_path, scope, entries)
    store = lexstore.get_store(tmp_path)
    assert store.rebuild_atomic()
    yield store, changed, removed
    freshness.clear()
    lexstore.clear_stores()


def _contend_once(monkeypatch: pytest.MonkeyPatch, store) -> None:
    """Make the foreground publication lose once without delaying its retry."""
    from exomem.vault import VaultLockTimeout

    real_lock = store._publication_lock
    contended = False

    @contextmanager
    def publication_lock(*args, **kwargs):
        nonlocal contended
        if not contended:
            contended = True
            raise VaultLockTimeout("VAULT_LOCK_TIMEOUT", "publication is busy")
        with real_lock(*args, **kwargs):
            yield

    monkeypatch.setattr(store, "_publication_lock", publication_lock)


@contextmanager
def _held_by_another_thread(lock):
    held = threading.Event()
    release = threading.Event()

    def hold():
        with lock:
            held.set()
            assert release.wait(20), "the test did not release the contended lock"

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(20), "the test did not acquire the contended lock"
        yield
    finally:
        release.set()
        holder.join(20)
        assert not holder.is_alive(), "the lock holder outlived the test"


@pytest.mark.parametrize("lag_only", [False, True], ids=["deferred-paths", "lag-only"])
@pytest.mark.parametrize("busy_lock", ["publication", "reserved-identity"])
def test_busy_repair_keeps_claims_until_pending_recovery_can_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    live_catalogue, lag_only: bool, busy_lock: str,
) -> None:
    """A still-busy barrier must preserve updates, deletions, and unavailable admission."""
    from exomem import readiness, reserved_paths, writer_lease

    store, changed, removed = live_catalogue
    key = tmp_path.resolve()
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(tmp_path))
    monkeypatch.setattr(lexstore, "_PUBLICATION_TIMEOUT_FOREGROUND", 0.01)
    monkeypatch.setattr(lexstore, "_PUBLICATION_TIMEOUT_BACKGROUND", 0.03)
    if busy_lock == "publication":
        lock = store._publication_lock()
    else:
        monkeypatch.setattr(writer_lease.active_manager(), "_mutation_timeout_seconds", 0.03)
        lock = reserved_paths._identity_coordination_scope(tmp_path)
    caplog.set_level(logging.INFO, logger="exomem.lexstore")
    changed.write_text("# After\n\nafterchange\n", encoding="utf-8")
    removed.unlink()
    freshness.on_files_changed(tmp_path, changed=[changed], deleted=[removed])
    rebuilds = []

    def rebuild():
        rebuilds.append(True)
        return False

    monkeypatch.setattr(store, "rebuild_atomic", rebuild)
    readiness.reset()
    readiness.mark_ready("retrieval_catalog")
    try:
        with _held_by_another_thread(lock):
            if lag_only:
                # The lag proof consumes both kinds of pending work together.
                with lexstore._REPAIRS_LOCK:
                    lexstore._DEFERRED_UPSERTS[key] = {changed, removed}
                if busy_lock == "publication":
                    assert lexstore.runtime_retrieval_catalog_proof(tmp_path) is None
                else:
                    lexstore._schedule_runtime_catalog_repair(tmp_path, lag_only=True)
            elif busy_lock == "publication":
                assert store.apply_watcher_batch(
                    [changed], [removed.relative_to(tmp_path).as_posix()]
                ) is False
            else:
                lexstore._schedule_runtime_catalog_repair(tmp_path, deferred_paths=[changed, removed])
            assert _wait_for_flight_end(tmp_path)
            code = "VAULT_LOCK_TIMEOUT" if busy_lock == "publication" else "MUTATION_BUSY"
            assert any(code in message for message in caplog.messages)
            assert rebuilds == [], "lock contention escalated to a full rebuild"
            with lexstore._REPAIRS_LOCK:
                assert lexstore._DEFERRED_UPSERTS.get(key) == {changed, removed}
                assert (key in lexstore._FULL_REBUILD_REQUESTED) is lag_only
                assert (key in lexstore._LAG_ONLY_REBUILDS) is lag_only
                assert key not in lexstore._FULL_REBUILDS_IN_FLIGHT
            assert not readiness.is_ready("retrieval_catalog")
            if busy_lock == "publication":
                assert lexstore.runtime_retrieval_catalog_proof(
                    tmp_path, require_live_projection=True, schedule_repair=False
                ) is None

        assert lexstore.retry_pending_repair(key) is True
        assert _wait_for_flight_end(tmp_path)
        assert rebuilds == []
        assert readiness.is_ready("retrieval_catalog")
        assert lexstore.runtime_retrieval_catalog_proof(
            tmp_path, require_live_projection=True, schedule_repair=False
        ) is not None
        assert lexstore.search_bm25(tmp_path, "afterchange", k=3, scope="kb")
        assert lexstore.search_bm25(tmp_path, "beforeremoval", k=3, scope="kb") == []
    finally:
        readiness.reset()


def test_contended_watcher_retries_changed_and_deleted_rows_without_rebuilding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live_catalogue
) -> None:
    """A mixed reconcile batch must persist both checkpoints without a full scan."""
    store, changed, removed = live_catalogue
    changed.write_text("# After\n\nafterchange\n", encoding="utf-8")
    removed.unlink()
    for scope in freshness.SCOPES:
        freshness.reconcile(tmp_path, scope, [(str(changed), freshness.stat_signature(changed))])
    rebuilds = []
    real_rebuild = store.rebuild_atomic

    def rebuild():
        rebuilds.append(True)
        return real_rebuild()

    monkeypatch.setattr(store, "rebuild_atomic", rebuild)
    _contend_once(monkeypatch, store)
    assert store.apply_watcher_batch([changed], [removed.relative_to(tmp_path).as_posix()]) is False
    assert _wait_for_flight_end(tmp_path)

    assert rebuilds == [], "watcher contention rebuilt the entire catalogue"
    assert lexstore.runtime_retrieval_catalog_proof(
        tmp_path, require_live_projection=True, schedule_repair=False
    ) is not None
    assert lexstore.search_bm25(tmp_path, "afterchange", k=3, scope="kb")
    assert lexstore.search_bm25(tmp_path, "beforeremoval", k=3, scope="kb") == []


def test_deferred_watcher_reconciles_current_files_after_newer_foreground_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live_catalogue
) -> None:
    """A delayed delete cannot erase a recreation or resurrect a later deletion."""
    store, changed, removed = live_catalogue
    changed.write_text("# Changed\n\nintermediatechange\n", encoding="utf-8")
    removed.unlink()
    freshness.on_files_changed(tmp_path, changed=[changed], deleted=[removed])
    started = threading.Event()
    release = threading.Event()
    real_retry = store.retry_deferred_upsert

    def retry(paths):
        started.set()
        assert release.wait(20), "the test did not release the deferred batch"
        return real_retry(paths)

    monkeypatch.setattr(store, "retry_deferred_upsert", retry)
    _contend_once(monkeypatch, store)
    try:
        assert store.apply_watcher_batch([changed], [removed.relative_to(tmp_path).as_posix()]) is False
        assert started.wait(20), "watcher contention did not enter targeted repair"
        removed.write_text("# Recreated\n\nrecreatedcontent\n", encoding="utf-8")
        changed.unlink()
        freshness.on_files_changed(tmp_path, changed=[removed], deleted=[changed])
        assert store.apply_watcher_batch([removed], [changed.relative_to(tmp_path).as_posix()])
    finally:
        release.set()
    assert _wait_for_flight_end(tmp_path)

    assert lexstore.runtime_retrieval_catalog_proof(
        tmp_path, require_live_projection=True, schedule_repair=False
    ) is not None
    assert lexstore.search_bm25(tmp_path, "recreatedcontent", k=3, scope="kb")
    assert lexstore.search_bm25(tmp_path, "intermediatechange", k=3, scope="kb") == []


def test_a_stranded_catalogue_catches_up_without_a_whole_corpus_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live_catalogue
) -> None:
    """A missed watcher batch must not cost a whole-corpus rebuild.

    Once one batch misses the publication barrier, later batches cannot bless
    their uncovered delta, so the stored checkpoint stays behind and every
    request asks for a full pass, whose own barrier makes the next batch miss.
    The worker replays the exact registry delta instead, deletions included.
    """
    store, changed, removed = live_catalogue
    changed.write_text("# After\n\nafterchange\n", encoding="utf-8")
    removed.unlink()
    # The registry records this generation; its lexical batch never lands.
    freshness.on_files_changed(tmp_path, changed=[changed], deleted=[removed])
    later = changed.parent / "later.md"
    later.write_text("# Later\n\nlatercontent\n", encoding="utf-8")
    freshness.on_files_changed(tmp_path, changed=[later])
    assert store.apply_watcher_batch([later], [])
    assert lexstore.runtime_retrieval_catalog_proof(
        tmp_path, require_live_projection=True, schedule_repair=False
    ) is None, "a batch blessed a delta it did not cover"
    rebuilds = []
    real_rebuild = store.rebuild_atomic

    def rebuild():
        rebuilds.append(True)
        return real_rebuild()

    monkeypatch.setattr(store, "rebuild_atomic", rebuild)

    # Every recall runs this strict proof; a lagging checkpoint queues repair.
    assert lexstore.runtime_retrieval_catalog_proof(tmp_path) is None
    assert _wait_for_flight_end(tmp_path)

    assert rebuilds == [], "a lagging catalogue was rebuilt from the whole corpus"
    assert lexstore.runtime_retrieval_catalog_proof(
        tmp_path, require_live_projection=True, schedule_repair=False
    ) is not None
    assert lexstore.search_bm25(tmp_path, "afterchange", k=3, scope="kb")
    assert lexstore.search_bm25(tmp_path, "latercontent", k=3, scope="kb")
    assert lexstore.search_bm25(tmp_path, "beforeremoval", k=3, scope="kb") == []


def test_a_page_saved_during_catch_up_converges_without_a_whole_corpus_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live_catalogue
) -> None:
    """An edit between a bounded upsert's read and its commit is ordinary writing.

    The rows roll back and the catalogue stays unproven, as before. Nothing
    about the store is wrong, though, and the save's own event asks for the
    page again, so a catch-up that meets a second save keeps its claim for a
    later attempt instead of rebuilding the corpus while the editor is busy.
    """
    store, changed, _removed = live_catalogue
    saves = ["# Edited\n\nsecondsave\n", "# Edited again\n\nthirdsave\n"]
    real_insert = store._insert_page

    def insert_while_the_editor_saves(conn, path, *args, **kwargs):
        real_insert(conn, path, *args, **kwargs)
        if path == changed and saves:
            changed.write_text(saves.pop(0), encoding="utf-8")
            freshness.on_files_changed(tmp_path, changed=[changed])

    monkeypatch.setattr(store, "_insert_page", insert_while_the_editor_saves)
    rebuilds = []
    real_rebuild = store.rebuild_atomic

    def rebuild():
        rebuilds.append(True)
        return real_rebuild()

    monkeypatch.setattr(store, "rebuild_atomic", rebuild)
    changed.write_text("# After\n\nfirstsave\n", encoding="utf-8")
    freshness.on_files_changed(tmp_path, changed=[changed])

    # The second save lands inside the watcher batch, the third inside the
    # lag-only catch-up that batch's failure scheduled.
    assert store.apply_watcher_batch([changed], []) is False
    assert _wait_for_flight_end(tmp_path)
    assert saves == []
    assert rebuilds == [], "a page saved during catch-up rebuilt the entire catalogue"
    assert lexstore.runtime_retrieval_catalog_proof(
        tmp_path, require_live_projection=True, schedule_repair=False
    ) is None, "a catalogue missing the latest save proved current"

    # The editor has stopped; the runtime's recovery retries the kept claim.
    assert lexstore.retry_pending_repair(tmp_path.resolve()) is True
    assert _wait_for_flight_end(tmp_path)

    assert rebuilds == []
    assert lexstore.runtime_retrieval_catalog_proof(
        tmp_path, require_live_projection=True, schedule_repair=False
    ) is not None
    assert lexstore.search_bm25(tmp_path, "thirdsave", k=3, scope="kb")
    assert lexstore.search_bm25(tmp_path, "firstsave", k=3, scope="kb") == []


def test_a_page_deleted_during_a_deferred_retry_keeps_its_claim_without_rebuilding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live_catalogue
) -> None:
    """A deletion mid-apply is the same moving source as an edit, on the contended path."""
    store, changed, _removed = live_catalogue
    changed.write_text("# After\n\nafterchange\n", encoding="utf-8")
    freshness.on_files_changed(tmp_path, changed=[changed])
    real_insert = store._insert_page
    deleted = False

    def insert_while_the_page_is_deleted(conn, path, *args, **kwargs):
        nonlocal deleted
        real_insert(conn, path, *args, **kwargs)
        if path == changed and not deleted:
            deleted = True
            changed.unlink()
            freshness.on_files_changed(tmp_path, deleted=[changed])

    rebuilds = []
    real_rebuild = store.rebuild_atomic

    def rebuild():
        rebuilds.append(True)
        return real_rebuild()

    monkeypatch.setattr(store, "rebuild_atomic", rebuild)
    _contend_once(monkeypatch, store)
    monkeypatch.setattr(store, "_insert_page", insert_while_the_page_is_deleted)

    assert store.apply_watcher_batch([changed], []) is False
    assert _wait_for_flight_end(tmp_path)
    assert deleted
    assert rebuilds == [], "a page deleted during its deferred retry rebuilt the catalogue"

    # True only while preserved work is pending: the deferred claim survived.
    assert lexstore.retry_pending_repair(tmp_path.resolve()) is True
    assert _wait_for_flight_end(tmp_path)

    assert rebuilds == []
    assert lexstore.runtime_retrieval_catalog_proof(
        tmp_path, require_live_projection=True, schedule_repair=False
    ) is not None
    assert lexstore.search_bm25(tmp_path, "beforechange", k=3, scope="kb") == []
    assert lexstore.search_bm25(tmp_path, "afterchange", k=3, scope="kb") == []


def test_a_catch_up_read_failure_that_is_not_source_movement_still_rebuilds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    live_catalogue,
) -> None:
    """Only a source that moved is worth waiting for; any other failure keeps the full pass."""
    caplog.set_level(logging.INFO, logger="exomem.lexstore")
    store, changed, _removed = live_catalogue
    changed.write_text("# After\n\nafterchange\n", encoding="utf-8")
    freshness.on_files_changed(tmp_path, changed=[changed])
    real_insert = store._insert_page
    failed = False

    def insert_with_one_read_failure(conn, path, *args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError(errno.EIO, "injected read failure")
        return real_insert(conn, path, *args, **kwargs)

    monkeypatch.setattr(store, "_insert_page", insert_with_one_read_failure)
    rebuilds = []
    real_rebuild = store.rebuild_atomic

    def rebuild():
        rebuilds.append(True)
        return real_rebuild()

    monkeypatch.setattr(store, "rebuild_atomic", rebuild)

    assert lexstore.runtime_retrieval_catalog_proof(tmp_path) is None
    assert _wait_for_flight_end(tmp_path)

    assert failed
    assert rebuilds == [True]
    assert any("catch-up declined (replay_failed)" in message for message in caplog.messages)
    assert lexstore.search_bm25(tmp_path, "afterchange", k=3, scope="kb")


@pytest.mark.parametrize(
    "code", ["VAULT_LOCK_NESTED", "VAULT_LOCK_IO", "MUTATION_LOCK_UNAVAILABLE"]
)
def test_a_catch_up_lock_failure_that_is_not_contention_still_rebuilds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live_catalogue, code: str
) -> None:
    """An unsafe lock or unavailable coordinator keeps the existing full-repair fallback."""
    from exomem.cli_ops import OpError
    from exomem.vault import VaultLockError

    store, changed, _removed = live_catalogue
    changed.write_text("# After\n\nafterchange\n", encoding="utf-8")
    freshness.on_files_changed(tmp_path, changed=[changed])
    real_lock = store._publication_lock
    failed = False

    @contextmanager
    def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            error_type = OpError if code == "MUTATION_LOCK_UNAVAILABLE" else VaultLockError
            raise error_type(code, "injected lock failure")
        with real_lock(*args, **kwargs):
            yield

    rebuilds = []
    real_rebuild = store.rebuild_atomic

    def rebuild():
        rebuilds.append(True)
        return real_rebuild()

    monkeypatch.setattr(store, "_publication_lock", fail_once)
    monkeypatch.setattr(store, "rebuild_atomic", rebuild)
    assert lexstore.runtime_retrieval_catalog_proof(tmp_path) is None
    assert _wait_for_flight_end(tmp_path)

    assert failed
    assert rebuilds == [True]
    assert lexstore.search_bm25(tmp_path, "afterchange", k=3, scope="kb")


def test_a_lag_over_the_foreground_cap_still_rebuilds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    live_catalogue,
) -> None:
    """Replaying an oversized delta under the barrier would block writers
    longer than the detached build it replaces. The log names that refusal, so
    an operator can tell this full pass from one a guard forced."""
    caplog.set_level(logging.INFO, logger="exomem.lexstore")
    store, changed, _removed = live_catalogue
    bulk = []
    for index in range(lexstore.CATALOG_FOREGROUND_DELTA_CAP + 1):
        page = changed.parent / f"bulk-{index}.md"
        page.write_text(f"# Bulk {index}\n\nbulkcontent\n", encoding="utf-8")
        bulk.append(page)
    freshness.on_files_changed(tmp_path, changed=bulk)
    rebuilds = []
    real_rebuild = store.rebuild_atomic

    def rebuild():
        rebuilds.append(True)
        return real_rebuild()

    monkeypatch.setattr(store, "rebuild_atomic", rebuild)

    assert lexstore.runtime_retrieval_catalog_proof(tmp_path) is None
    assert _wait_for_flight_end(tmp_path)

    assert rebuilds == [True]
    assert any("catch-up declined (over_cap)" in message for message in caplog.messages)


def test_a_full_request_with_nothing_lagging_still_rebuilds_drifted_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live_catalogue
) -> None:
    """Checkpoint metadata cannot attest rows.

    Callers such as an exact purge that lost the barrier request a full pass
    because rows drifted while every checkpoint stayed current. That request
    must still rebuild, not be acknowledged by an exact-current proof.
    """
    store, changed, _removed = live_catalogue
    row = changed.relative_to(tmp_path).as_posix()
    with closing(sqlite3.connect(store.path)) as conn, conn:
        conn.execute("DELETE FROM pages WHERE path = ?", (row,))
    rebuilds = []
    real_rebuild = store.rebuild_atomic

    def rebuild():
        rebuilds.append(True)
        return real_rebuild()

    monkeypatch.setattr(store, "rebuild_atomic", rebuild)

    lexstore._schedule_repair(tmp_path)
    assert _wait_for_flight_end(tmp_path)

    assert rebuilds == [True]
    with closing(sqlite3.connect(store.path)) as conn:
        assert conn.execute("SELECT 1 FROM pages WHERE path = ?", (row,)).fetchone()


@pytest.fixture(autouse=True)
def _clean_repair_state():
    """Every test starts with no pending repair work and leaves none behind."""
    _quiesce()
    yield
    assert _quiesce(), "a background lexical repair outlived its test"


def _quiesce(timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with lexstore._REPAIRS_LOCK:
            if not lexstore._REPAIRS_IN_FLIGHT:
                leaked_full_pass = bool(lexstore._FULL_REBUILDS_IN_FLIGHT)
                leaked_reobservation = bool(lexstore._FULL_REBUILD_REOBSERVED)
                lexstore._DEFERRED_UPSERTS.clear()
                lexstore._FULL_REBUILD_REQUESTED.clear()
                lexstore._LAG_ONLY_REBUILDS.clear()
                lexstore._FULL_REBUILDS_IN_FLIGHT.clear()
                lexstore._FULL_REBUILD_REOBSERVED.clear()
                lexstore._PUBLISHED_HANDOFF_PENDING.clear()
                return not leaked_full_pass and not leaked_reobservation
        time.sleep(0.01)
    return False


def _wait_for_flight_end(root: Path, timeout: float = 20.0) -> bool:
    """Wait without clearing pending work, so a test can inspect handoff state."""
    key = root.resolve()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with lexstore._REPAIRS_LOCK:
            if key not in lexstore._REPAIRS_IN_FLIGHT:
                return True
        time.sleep(0.01)
    return False


class _FakeStore:
    """Records which repair the worker chose, and whether it was given paths."""

    def __init__(self, *, retry_applies: bool = True):
        self.retry_applies = retry_applies
        self.retried: list[list[Path]] = []
        self.rebuilds = 0

    def retry_deferred_upsert(self, paths: list[Path]) -> bool:
        self.retried.append(list(paths))
        return self.retry_applies

    def rebuild_atomic(self) -> bool:
        self.rebuilds += 1
        return True


def _install(monkeypatch: pytest.MonkeyPatch, store: _FakeStore) -> None:
    monkeypatch.setattr(lexstore, "get_store", lambda _root: store)
    checkpoint = freshness.RecallFreshnessCheckpoint(
        "test-instance",
        1,
        (0, 0, "test-digest"),
        "test-policy",
        "test-access",
    )
    monkeypatch.setattr(
        lexstore,
        "runtime_retrieval_catalog_proof",
        lambda _root, **_kwargs: {
            scope: checkpoint for scope in freshness.SCOPES
        },
    )


def test_a_contended_page_is_retried_not_rebuilt_around(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: O(changed) instead of O(vault) for an O(1) cause."""
    store = _FakeStore(retry_applies=True)
    _install(monkeypatch, store)
    page = tmp_path / "Knowledge Base" / "Notes" / "one.md"

    lexstore._schedule_repair(tmp_path, deferred_paths=[page])
    assert _quiesce()

    assert store.retried == [[page]]
    assert store.rebuilds == 0, "a contended single page must not rebuild the corpus"


def test_idle_recovery_neither_resolves_sources_nor_opens_a_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = tmp_path.resolve()

    def unexpected(*_args, **_kwargs):
        pytest.fail("idle recovery inspected the filesystem or opened a store")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", unexpected)
        patch.setattr(lexstore, "get_store", unexpected)
        patch.setattr(lexstore, "_live_repair_generation", unexpected)
        assert lexstore.retry_pending_repair(key) is False


def test_recovery_preserves_a_targeted_only_retry_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Store(_FakeStore):
        def retry_deferred_upsert(self, paths):
            self.retried.append(list(paths))
            if len(self.retried) == 1:
                raise OSError("transient targeted failure")
            return True

    store = Store()
    _install(monkeypatch, store)
    page = tmp_path / "Knowledge Base" / "one.md"
    lexstore._schedule_repair(tmp_path, deferred_paths=[page])
    assert _wait_for_flight_end(tmp_path)

    assert lexstore.retry_pending_repair(tmp_path.resolve()) is True
    assert _wait_for_flight_end(tmp_path)
    assert store.retried == [[page], [page]]
    assert store.rebuilds == 0, "recovery turned a targeted claim into a full rebuild"


def test_a_targeted_retry_that_does_not_land_still_rebuilds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Escalation is what makes the cheap path safe to try first.

    The retry can decline for reasons that are not contention at all -- a
    noncurrent schema, a missing sidecar -- and none of those is fixed by
    re-applying rows. Repair must end up no weaker than it was.
    """
    store = _FakeStore(retry_applies=False)
    _install(monkeypatch, store)
    page = tmp_path / "Knowledge Base" / "Notes" / "one.md"

    lexstore._schedule_repair(tmp_path, deferred_paths=[page])
    assert _quiesce()

    assert store.retried == [[page]]
    assert store.rebuilds == 1


def test_targeted_rows_without_current_admission_preserve_full_repair_demand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Applied rows must not strand retrieval when their admission proof fails."""
    store = _FakeStore()
    _install(monkeypatch, store)
    monkeypatch.setattr(lexstore, "_is_configured_runtime_vault", lambda _root: True)
    monkeypatch.setattr(
        lexstore,
        "_mark_runtime_retrieval_ready_if_current",
        lambda _root, **_kwargs: store.rebuilds > 0,
    )
    page = tmp_path / "Knowledge Base" / "one.md"

    lexstore._schedule_repair(tmp_path, deferred_paths=[page])
    assert _wait_for_flight_end(tmp_path)

    assert store.retried == [[page]]
    assert store.rebuilds == 1, "declined admission lost the full repair request"


@pytest.mark.parametrize(("second_lag_only", "rebuilds"), [(True, 0), (False, 1)])
def test_one_request_with_another_cause_restores_the_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, second_lag_only: bool, rebuilds: int
) -> None:
    """Folded requests stay lag-only only while every one of them was lag."""

    class CatchingUpStore(_FakeStore):
        def catch_up_lagging_delta(self) -> bool:
            return True

    store = CatchingUpStore()
    _install(monkeypatch, store)
    with lexstore._REPAIRS_LOCK:
        lexstore._request_full_rebuild(tmp_path.resolve(), lag_only=True)

    lexstore._schedule_repair(tmp_path, lag_only=second_lag_only)
    assert _quiesce()

    assert store.rebuilds == rebuilds


def test_every_other_caller_still_gets_the_full_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only contention is cheap. A failed store or a sqlite error is not.

    Those callers pass no paths, and a rebuild is the only correct answer to
    them, so the targeted path must never be reached on their behalf.
    """
    store = _FakeStore(retry_applies=True)
    _install(monkeypatch, store)

    lexstore._schedule_repair(tmp_path)
    assert _quiesce()

    assert store.retried == []
    assert store.rebuilds == 1


def test_duplicate_full_request_during_full_rebuild_is_coalesced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Readiness polling must not turn one full rebuild into an endless chain.

    A stale managed catalogue can be observed by several health and request
    probes while its detached rebuild is still running. Those observations all
    describe the same stale generation, so another full request arriving during
    that full pass is already covered by the active work. It must not queue a
    second whole-corpus pass behind it.
    """

    class _RequestingStore(_FakeStore):
        def rebuild_atomic(self) -> bool:
            self.rebuilds += 1
            if self.rebuilds == 1:
                # Arrives while this vault's full repair pass is active.
                lexstore._schedule_repair(tmp_path)
            return True

    store = _RequestingStore()
    _install(monkeypatch, store)

    lexstore._schedule_repair(tmp_path)
    assert _quiesce()

    assert store.rebuilds == 1, "an active full rebuild queued a duplicate pass"


def test_pending_request_after_published_pass_skips_a_redundant_current_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An idle handoff must re-prove before repeating a successful full scan."""
    store = _FakeStore()
    _install(monkeypatch, store)
    key = tmp_path.resolve()
    with lexstore._REPAIRS_LOCK:
        lexstore._set_repair_progress(key, "queued")
        lexstore._set_repair_progress(key, "idle", result="published")
        lexstore._FULL_REBUILD_REQUESTED.add(key)
        lexstore._PUBLISHED_HANDOFF_PENDING.add(key)

    lexstore._schedule_repair(tmp_path)
    assert _quiesce()

    assert store.rebuilds == 0, "a current published catalogue was scanned again"


def test_a_published_repair_reproves_after_another_proof_wins_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost admission CAS must not rebuild an already-published catalogue."""
    from exomem import readiness

    store = _FakeStore()
    _install(monkeypatch, store)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(tmp_path))
    proof = lexstore.runtime_retrieval_catalog_proof
    competing_proof = True

    def prove(root, **kwargs):
        nonlocal competing_proof
        if competing_proof:
            competing_proof = False
            readiness.mark_ready("retrieval_catalog")
        return proof(root, **kwargs)

    monkeypatch.setattr(lexstore, "runtime_retrieval_catalog_proof", prove)
    readiness.reset()
    try:
        lexstore._schedule_repair(tmp_path)
        assert _wait_for_flight_end(tmp_path)
        assert readiness.is_ready("retrieval_catalog")

        assert lexstore.retry_pending_repair(tmp_path.resolve()) is True
        assert _wait_for_flight_end(tmp_path)
        assert store.rebuilds == 1, "another proof's admission triggered a redundant rebuild"
    finally:
        readiness.reset()


def test_pending_request_after_published_pass_rebuilds_when_proof_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cheap handoff proof may only suppress work it proves unnecessary."""
    store = _FakeStore()
    _install(monkeypatch, store)
    monkeypatch.setattr(
        lexstore,
        "runtime_retrieval_catalog_proof",
        lambda _root, **_kwargs: None,
    )
    key = tmp_path.resolve()
    with lexstore._REPAIRS_LOCK:
        lexstore._set_repair_progress(key, "queued")
        lexstore._set_repair_progress(key, "idle", result="published")
        lexstore._FULL_REBUILD_REQUESTED.add(key)
        lexstore._PUBLISHED_HANDOFF_PENDING.add(key)

    lexstore._schedule_repair(tmp_path)
    assert _quiesce()

    assert store.rebuilds == 1


def test_request_arriving_during_current_handoff_proof_is_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cheap proof cannot acknowledge a request that arrives after it starts."""
    store = _FakeStore()
    _install(monkeypatch, store)
    current_proof = lexstore.runtime_retrieval_catalog_proof
    fired = False

    def prove_then_request(root: Path, **kwargs: object) -> dict[str, object] | None:
        nonlocal fired
        proof = current_proof(root, **kwargs)
        if not fired:
            fired = True
            lexstore._schedule_repair(tmp_path)
        return proof

    monkeypatch.setattr(lexstore, "runtime_retrieval_catalog_proof", prove_then_request)
    key = tmp_path.resolve()
    with lexstore._REPAIRS_LOCK:
        lexstore._set_repair_progress(key, "queued")
        lexstore._set_repair_progress(key, "idle", result="published")
        lexstore._FULL_REBUILD_REQUESTED.add(key)
        lexstore._PUBLISHED_HANDOFF_PENDING.add(key)

    lexstore._schedule_repair(tmp_path)
    assert _quiesce()

    assert fired
    assert store.rebuilds == 1


def test_full_request_during_declined_rebuild_survives_a_bounded_idle_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declined pass covers nothing, but must not spin into the next pass."""

    class _DecliningOnceStore(_FakeStore):
        def rebuild_atomic(self) -> bool:
            self.rebuilds += 1
            if self.rebuilds == 1:
                lexstore._schedule_repair(tmp_path)
                return False
            return True

    store = _DecliningOnceStore()
    _install(monkeypatch, store)
    key = tmp_path.resolve()

    lexstore._schedule_repair(tmp_path)
    assert _wait_for_flight_end(tmp_path)

    with lexstore._REPAIRS_LOCK:
        assert key in lexstore._FULL_REBUILD_REQUESTED
        assert key not in lexstore._FULL_REBUILDS_IN_FLIGHT
    assert store.rebuilds == 1, "a declined pass chained another whole-vault scan"

    # A later caller crosses the idle boundary and drains the preserved request.
    lexstore._schedule_repair(tmp_path)
    assert _quiesce()
    assert store.rebuilds == 2


def test_declined_full_rebuild_preserves_demand_without_reobservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed full pass must not rely on another probe to request repair."""

    class _DecliningOnceStore(_FakeStore):
        def rebuild_atomic(self) -> bool:
            self.rebuilds += 1
            return self.rebuilds > 1

    store = _DecliningOnceStore()
    _install(monkeypatch, store)
    key = tmp_path.resolve()

    lexstore._schedule_repair(tmp_path)
    assert _wait_for_flight_end(tmp_path)

    with lexstore._REPAIRS_LOCK:
        assert key in lexstore._FULL_REBUILD_REQUESTED
        assert key not in lexstore._FULL_REBUILDS_IN_FLIGHT
    assert store.rebuilds == 1, "a declined pass retried without an idle boundary"

    lexstore._schedule_repair(tmp_path)
    assert _quiesce()
    assert store.rebuilds == 2


def test_newer_generation_observed_during_promotion_survives_for_next_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A publish that cannot prove current must not acknowledge a newer request."""
    store = _FakeStore()
    _install(monkeypatch, store)
    key = tmp_path.resolve()
    newer_requested = False

    monkeypatch.setattr(lexstore, "_is_configured_runtime_vault", lambda _root: True)

    def promote(
        _root: Path,
        *,
        proof_out: dict[str, object] | None = None,
    ) -> bool:
        nonlocal newer_requested
        if store.rebuilds == 1:
            # The proof sees a newer projection while the published pass is
            # still active and requests repair for that uncovered generation.
            if not newer_requested:
                newer_requested = True
                lexstore._schedule_repair(tmp_path)
            return False
        if proof_out is not None:
            proof_out.update(
                {
                    scope: freshness.RecallFreshnessCheckpoint(
                        "test-instance",
                        store.rebuilds,
                        (0, 0, f"digest-{store.rebuilds}"),
                        "test-policy",
                        "test-access",
                    )
                    for scope in freshness.SCOPES
                }
            )
        return True

    monkeypatch.setattr(lexstore, "_mark_runtime_retrieval_ready_if_current", promote)

    lexstore._schedule_repair(tmp_path)
    assert _wait_for_flight_end(tmp_path)

    with lexstore._REPAIRS_LOCK:
        assert key in lexstore._FULL_REBUILD_REQUESTED
        assert key not in lexstore._FULL_REBUILDS_IN_FLIGHT
    assert store.rebuilds == 1

    lexstore._schedule_repair(tmp_path)
    assert _quiesce()
    assert store.rebuilds == 2


def test_post_promotion_new_generation_is_not_acknowledged_by_older_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Close the proof-to-active-marker race with generation-tagged requests."""
    store = _FakeStore()
    _install(monkeypatch, store)
    key = tmp_path.resolve()

    def generation(number: int) -> tuple[object, ...]:
        return tuple(
            freshness.RecallFreshnessCheckpoint(
                "test-instance",
                number,
                (number, number, f"digest-{number}"),
                "test-policy",
                "test-access",
            )
            for _scope in freshness.SCOPES
        )

    proved = generation(1)
    newer = generation(2)
    observed = [proved]
    monkeypatch.setattr(lexstore, "_is_configured_runtime_vault", lambda _root: True)
    monkeypatch.setattr(
        lexstore,
        "_live_repair_generation",
        lambda _root: observed[0],
    )

    def promote(
        _root: Path,
        *,
        proof_out: dict[str, object] | None = None,
    ) -> bool:
        assert proof_out is not None
        proof_out.update(dict(zip(freshness.SCOPES, proved, strict=True)))
        # Generation 2 lands after generation 1 was proved but before the
        # worker clears its active marker.
        observed[0] = newer
        lexstore._schedule_repair(tmp_path)
        return True

    monkeypatch.setattr(lexstore, "_mark_runtime_retrieval_ready_if_current", promote)

    lexstore._schedule_repair(tmp_path)
    assert _wait_for_flight_end(tmp_path)

    with lexstore._REPAIRS_LOCK:
        assert key in lexstore._FULL_REBUILD_REQUESTED
        assert key not in lexstore._FULL_REBUILDS_IN_FLIGHT
    assert store.rebuilds == 1


def test_a_rebuild_requested_during_a_targeted_retry_is_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Single-flight must not silently discard the stronger request.

    Repair is one worker per vault, so a rebuild arriving while a targeted retry
    is running finds a flight already in progress. Before, a full rebuild was
    the only thing the worker ever did, so nothing could be lost this way;
    now the worker has two modes and has to re-read its queues before exiting.
    """
    page = tmp_path / "Knowledge Base" / "Notes" / "one.md"

    class _RequestingStore(_FakeStore):
        def retry_deferred_upsert(self, paths: list[Path]) -> bool:
            # Arrives while this worker holds the only flight for the vault.
            lexstore._schedule_repair(tmp_path)
            return super().retry_deferred_upsert(paths)

    store = _RequestingStore(retry_applies=True)
    _install(monkeypatch, store)

    lexstore._schedule_repair(tmp_path, deferred_paths=[page])
    assert _quiesce()

    assert store.retried == [[page]]
    assert store.rebuilds == 1, "the rebuild queued mid-flight was dropped"


def test_a_full_request_queued_during_contended_catch_up_keeps_its_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A current checkpoint cannot cancel a genuine full request queued during a busy replay."""
    from exomem.vault import VaultLockTimeout

    class _ContendingStore(_FakeStore):
        def catch_up_lagging_delta(self) -> bool:
            lexstore._schedule_repair(tmp_path)
            raise VaultLockTimeout("VAULT_LOCK_TIMEOUT", "publication is busy")

    store = _ContendingStore()
    _install(monkeypatch, store)
    key = tmp_path.resolve()
    lexstore._schedule_repair(tmp_path, lag_only=True)
    assert _wait_for_flight_end(tmp_path)
    assert store.rebuilds == 0
    with lexstore._REPAIRS_LOCK:
        assert key in lexstore._FULL_REBUILD_REQUESTED
        assert key not in lexstore._LAG_ONLY_REBUILDS

    assert lexstore.retry_pending_repair(key) is True
    assert _wait_for_flight_end(tmp_path)
    assert store.rebuilds == 1


def test_paths_deferred_during_a_flight_are_still_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same for the cheap request: a second contended page must not vanish."""
    first = tmp_path / "Knowledge Base" / "Notes" / "one.md"
    second = tmp_path / "Knowledge Base" / "Notes" / "two.md"

    class _RequestingStore(_FakeStore):
        def retry_deferred_upsert(self, paths: list[Path]) -> bool:
            if not self.retried:
                lexstore._schedule_repair(tmp_path, deferred_paths=[second])
            return super().retry_deferred_upsert(paths)

    store = _RequestingStore(retry_applies=True)
    _install(monkeypatch, store)

    lexstore._schedule_repair(tmp_path, deferred_paths=[first])
    assert _quiesce()

    assert store.retried == [[first], [second]]
    assert store.rebuilds == 0


def test_the_contention_branch_hands_over_the_paths_it_deferred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`upsert_paths` must name what it could not write, or nothing can retry it.

    Pinned at the call site rather than only end to end, because the deferral
    is a `VaultLockError` catch that is easy to leave passing paths-less while
    every other test still goes green through the rebuild escalation.
    """
    from exomem.vault import VaultLockError

    seen: list[dict] = []
    monkeypatch.setattr(
        lexstore,
        "_schedule_repair",
        lambda root, **kwargs: seen.append({"root": root, **kwargs}),
    )

    store = lexstore.LexicalStore.__new__(lexstore.LexicalStore)
    store.vault_root = tmp_path
    store._failed = False
    page = tmp_path / "Knowledge Base" / "Notes" / "one.md"

    def refuse(timeout: float):  # noqa: ANN202, ARG001
        raise VaultLockError("VAULT_LOCK_NESTED", "held by the writer")

    monkeypatch.setattr(store, "_publication_lock", refuse)

    assert store.upsert_paths([page]) is False
    assert seen == [{"root": tmp_path, "deferred_paths": [page]}]


def test_a_failed_pass_puts_its_work_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker that dies must not take the request with it.

    The pass consumes both queues before doing the work, so an exception used to
    drop whatever it had claimed -- and the next caller would find nothing
    pending and start a worker with nothing to do. That is the same
    "invalidate cheaply, leave the cost to whoever asks next" shape this whole
    change exists to remove, reintroduced inside the fix.
    """
    page = tmp_path / "Knowledge Base" / "Notes" / "one.md"

    class _Exploding(_FakeStore):
        def retry_deferred_upsert(self, paths: list[Path]) -> bool:
            raise RuntimeError("sidecar vanished mid-repair")

    _install(monkeypatch, _Exploding())
    lexstore._schedule_repair(tmp_path, deferred_paths=[page])

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        with lexstore._REPAIRS_LOCK:
            if not lexstore._REPAIRS_IN_FLIGHT:
                break
        time.sleep(0.01)

    with lexstore._REPAIRS_LOCK:
        assert lexstore._DEFERRED_UPSERTS.get(tmp_path.resolve()) == {page}

    # And the next scheduling actually drains it rather than starting empty.
    store = _FakeStore(retry_applies=True)
    _install(monkeypatch, store)
    lexstore._schedule_repair(tmp_path, deferred_paths=[page])
    assert _quiesce()

    assert store.retried == [[page]]
    assert store.rebuilds == 0
