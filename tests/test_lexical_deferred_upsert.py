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

from exomem import freshness, lexstore


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
    promotions = 0

    monkeypatch.setattr(lexstore, "_is_configured_runtime_vault", lambda _root: True)

    def promote(
        _root: Path,
        *,
        proof_out: dict[str, object] | None = None,
    ) -> bool:
        nonlocal promotions
        promotions += 1
        if promotions == 1:
            # The proof sees a newer projection while the published pass is
            # still active and requests repair for that uncovered generation.
            lexstore._schedule_repair(tmp_path)
            return False
        if proof_out is not None:
            proof_out.update(
                {
                    scope: freshness.RecallFreshnessCheckpoint(
                        "test-instance",
                        promotions,
                        (0, 0, f"digest-{promotions}"),
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
    assert promotions == 2


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
