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

import time
from pathlib import Path

import pytest

from exomem import lexstore


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
                lexstore._DEFERRED_UPSERTS.clear()
                lexstore._FULL_REBUILD_REQUESTED.clear()
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

    def rebuild_atomic(self) -> None:
        self.rebuilds += 1


def _install(monkeypatch: pytest.MonkeyPatch, store: _FakeStore) -> None:
    monkeypatch.setattr(lexstore, "get_store", lambda _root: store)


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
