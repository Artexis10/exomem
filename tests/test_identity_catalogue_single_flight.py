"""Cold private-identity inventory construction is single-flighted and versioned.

Concurrent startup readers used to each build the same inventory; generation
churn then invalidated every optimistic scan and forced one caller into a
whole-vault locked scan while it held the all-domain reserved-identity gate.
These tests pin the repair: exactly one cold build, a catalogue that carries the
reserved-identity generation it was proved against, a non-blocking readiness
mode that neither scans nor takes the gate, and unchanged fail-closed alias
refusal on both the warm and the rebuilt-after-mismatch path.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

from exomem import request_budget, reserved_paths, writer_lease

CALLERS = 8


def _vault(tmp_path: Path) -> Path:
    vault_root = tmp_path / "vault"
    (vault_root / "Knowledge Base" / "Notes").mkdir(parents=True)
    (vault_root / "Knowledge Base" / "Notes" / "note.md").write_text(
        "# Note\n", encoding="utf-8"
    )
    return vault_root


def _manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mutation_timeout_seconds: float = 5.0,
) -> writer_lease.LeaseManager:
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "state"),
        mutation_timeout_seconds=mutation_timeout_seconds,
    )
    monkeypatch.setattr(writer_lease, "active_manager", lambda: manager)
    return manager


def _current_token(
    manager: writer_lease.LeaseManager, vault_root: Path
) -> str:
    """The shared token, read the way the coordination scope reads it."""

    return writer_lease._read_reserved_identity_generation(
        manager.config.state_dir, vault_root
    )


def _bump_generation(vault_root: Path) -> None:
    """Advance the shared token the way an exact owner does before publishing."""

    with reserved_paths._subsystem_authority_scope("embedding_index"):
        with reserved_paths._identity_coordination_scope(
            vault_root,
            descriptor_ids=("embeddings-store",),
        ):
            pass


def _run_callers(target, count: int = CALLERS) -> list[threading.Thread]:
    threads = [
        threading.Thread(target=target, name=f"catalogue-caller-{index}", daemon=True)
        for index in range(count)
    ]
    for thread in threads:
        thread.start()
    return threads


def test_c1_concurrent_cold_callers_cause_exactly_one_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1: N concurrent cold callers build the inventory once, never N times."""

    vault_root = _vault(tmp_path)
    _manager(tmp_path, monkeypatch)

    builds = 0
    counter_lock = threading.Lock()
    build_entered = threading.Event()
    release_build = threading.Event()
    followers_parked = threading.Event()
    parked = 0
    original_from_vault = reserved_paths.IdentityCatalogue.from_vault.__func__

    def counted_from_vault(
        cls: type[reserved_paths.IdentityCatalogue], root: Path
    ) -> reserved_paths.IdentityCatalogue:
        nonlocal builds
        with counter_lock:
            builds += 1
            if builds == CALLERS:
                # Base-code signal: every caller reached its own cold build.
                followers_parked.set()
        build_entered.set()
        assert release_build.wait(20)
        return original_from_vault(cls, root)

    monkeypatch.setattr(
        reserved_paths.IdentityCatalogue,
        "from_vault",
        classmethod(counted_from_vault),
    )

    # Park detection: on the repaired code a follower waits inside the flight
    # rather than starting a build, so the wait is where arrival is observable.
    flight_wait = getattr(reserved_paths, "_await_baseline_flight", None)
    if flight_wait is not None:

        def counted_wait(flight, timeout: float):
            nonlocal parked
            with counter_lock:
                parked += 1
                if parked == CALLERS - 1:
                    followers_parked.set()
            return flight_wait(flight, timeout)

        monkeypatch.setattr(reserved_paths, "_await_baseline_flight", counted_wait)

    results: list[reserved_paths.IdentityCatalogue] = []
    failures: list[BaseException] = []

    def call() -> None:
        try:
            results.append(reserved_paths._baseline_identity_catalogue(vault_root))
        except BaseException as error:  # noqa: BLE001 - preserve worker failure
            failures.append(error)

    threads = _run_callers(call)
    assert build_entered.wait(20)
    assert followers_parked.wait(20)
    release_build.set()
    for thread in threads:
        thread.join(20)

    assert [thread.name for thread in threads if thread.is_alive()] == []
    assert failures == []
    assert builds == 1
    assert len(results) == CALLERS
    first = results[0]
    assert all(result.identities == first.identities for result in results)


def test_c2_generation_bump_rejects_the_stale_catalogue_and_rebuild_catches_the_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C2: a bumped generation rejects the catalogue; the rebuild still aliases."""

    vault_root = _vault(tmp_path)
    manager = _manager(tmp_path, monkeypatch)

    stale = reserved_paths._baseline_identity_catalogue(vault_root)
    proved = stale.generation
    assert proved is not None
    assert proved == _current_token(manager, vault_root)

    # An alias the stale inventory cannot know about: the private family and an
    # ordinary spelling are one physical file, created after the snapshot.
    kb = vault_root / "Knowledge Base"
    private = kb / ".embeddings.sqlite"
    private.write_bytes(b"private index bytes")
    alias = kb / "Notes" / "ordinary.bin"
    os.link(private, alias)
    alias_identity = reserved_paths._lstat_identity(alias)
    assert stale.descriptor_for(alias_identity) is None

    _bump_generation(vault_root)
    current = _current_token(manager, vault_root)
    assert current != proved

    assert reserved_paths.revalidate_identity_catalogue_generation(vault_root) is False
    assert reserved_paths.identity_catalogue_ready(vault_root) is False

    rebuilt = reserved_paths._baseline_identity_catalogue(vault_root)
    assert rebuilt is not stale
    assert rebuilt.generation is not None
    assert rebuilt.generation != proved
    assert rebuilt.descriptor_for(alias_identity) == "embeddings-store"

    with pytest.raises(reserved_paths.ReservedPathLeafError) as error:
        reserved_paths.read_generic_bytes(
            vault_root,
            "Knowledge Base/Notes/ordinary.bin",
        )
    assert error.value.code == "RESERVED_PATH"


def test_c2_revalidation_keeps_a_catalogue_whose_generation_still_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C2: an unchanged generation is not an excuse to discard a proved inventory."""

    vault_root = _vault(tmp_path)
    _manager(tmp_path, monkeypatch)

    built = reserved_paths._baseline_identity_catalogue(vault_root)
    assert reserved_paths.revalidate_identity_catalogue_generation(vault_root) is True
    assert reserved_paths.identity_catalogue_ready(vault_root) is True
    assert reserved_paths._baseline_identity_catalogue(vault_root) is built


def test_c3_readiness_reports_cold_without_scanning_or_taking_the_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C3: an interactive caller learns 'not ready' with no scan and no gate."""

    vault_root = _vault(tmp_path)
    monkeypatch.setattr(
        reserved_paths.IdentityCatalogue,
        "from_vault",
        classmethod(
            lambda cls, root: pytest.fail("non-blocking readiness scanned the vault")
        ),
    )
    monkeypatch.setattr(
        writer_lease,
        "active_manager",
        lambda: pytest.fail("non-blocking readiness took the all-domain identity gate"),
    )

    assert reserved_paths.identity_catalogue_ready(vault_root) is False


def test_c4_physical_alias_conflict_is_refused_on_the_warm_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C4 (negative guard): warm-path alias refusal is identical before and after."""

    vault_root = _vault(tmp_path)
    _manager(tmp_path, monkeypatch)

    kb = vault_root / "Knowledge Base"
    private = kb / ".embeddings.sqlite"
    private.write_bytes(b"private index bytes")
    alias = kb / "Notes" / "ordinary.bin"
    os.link(private, alias)

    catalogue = reserved_paths._baseline_identity_catalogue(vault_root)
    assert (
        catalogue.descriptor_for(reserved_paths._lstat_identity(alias))
        == "embeddings-store"
    )

    for _repeat in range(2):
        with pytest.raises(reserved_paths.ReservedPathLeafError) as error:
            reserved_paths.read_generic_bytes(
                vault_root,
                "Knowledge Base/Notes/ordinary.bin",
            )
        assert error.value.code == "RESERVED_PATH"

    with pytest.raises(reserved_paths.ReservedPathLeafError) as reserved_error:
        reserved_paths.read_generic_bytes(
            vault_root,
            "Knowledge Base/.embeddings.sqlite",
        )
    assert reserved_error.value.code == "RESERVED_PATH"


def test_c5_a_failed_build_releases_the_flight_and_never_hangs_a_follower(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C5: one shared failure, no hung followers, and the next caller retries."""

    vault_root = _vault(tmp_path)
    _manager(tmp_path, monkeypatch)

    attempts = 0
    counter_lock = threading.Lock()
    build_entered = threading.Event()
    release_build = threading.Event()
    followers_parked = threading.Event()
    parked = 0
    original_from_vault = reserved_paths.IdentityCatalogue.from_vault.__func__

    def failing_from_vault(
        cls: type[reserved_paths.IdentityCatalogue], root: Path
    ) -> reserved_paths.IdentityCatalogue:
        nonlocal attempts
        with counter_lock:
            attempts += 1
            first = attempts == 1
            if attempts == CALLERS:
                followers_parked.set()
        if not first:
            return original_from_vault(cls, root)
        build_entered.set()
        assert release_build.wait(20)
        raise RuntimeError("reserved identity catalogue could not acquire the vault")

    monkeypatch.setattr(
        reserved_paths.IdentityCatalogue,
        "from_vault",
        classmethod(failing_from_vault),
    )

    flight_wait = getattr(reserved_paths, "_await_baseline_flight", None)
    if flight_wait is not None:

        def counted_wait(flight, timeout: float):
            nonlocal parked
            with counter_lock:
                parked += 1
                if parked == CALLERS - 1:
                    followers_parked.set()
            return flight_wait(flight, timeout)

        monkeypatch.setattr(reserved_paths, "_await_baseline_flight", counted_wait)

    failures: list[BaseException] = []
    successes: list[reserved_paths.IdentityCatalogue] = []

    def call() -> None:
        try:
            successes.append(reserved_paths._baseline_identity_catalogue(vault_root))
        except BaseException as error:  # noqa: BLE001 - preserve worker failure
            failures.append(error)

    threads = _run_callers(call)
    assert build_entered.wait(20)
    assert followers_parked.wait(20)
    release_build.set()
    for thread in threads:
        thread.join(20)

    assert [thread.name for thread in threads if thread.is_alive()] == []
    assert successes == []
    assert len(failures) == CALLERS
    assert all(isinstance(error, RuntimeError) for error in failures)
    # The failed build was shared, not repeated once per caller.
    assert attempts == 1

    retried = reserved_paths._baseline_identity_catalogue(vault_root)
    assert retried.generation is not None
    assert attempts == 2
    assert reserved_paths.identity_catalogue_ready(vault_root) is True


def test_every_non_nested_build_path_stamps_the_token_its_scan_validated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The locked fail-safe proves its inventory too, not only the seqlock path.

    Both optimistic attempts are made to lose their seqlock by bumping the
    shared token across each walk, so the build falls through to the fail-safe
    branch that holds the gate for the whole scan.  That branch reads a token as
    well, and the inventory it returns must carry it.
    """

    vault_root = _vault(tmp_path)
    manager = _manager(tmp_path, monkeypatch)

    walks = 0
    original_from_vault = reserved_paths.IdentityCatalogue.from_vault.__func__

    def churning_from_vault(
        cls: type[reserved_paths.IdentityCatalogue], root: Path
    ) -> reserved_paths.IdentityCatalogue:
        nonlocal walks
        walks += 1
        # Only the two optimistic walks run outside the gate; bumping inside the
        # fail-safe's own scope would wait on the gate that scope is holding.
        if walks <= 2:
            _bump_generation(vault_root)
        return original_from_vault(cls, root)

    monkeypatch.setattr(
        reserved_paths.IdentityCatalogue,
        "from_vault",
        classmethod(churning_from_vault),
    )

    catalogue = reserved_paths._baseline_identity_catalogue(vault_root)

    assert walks == 3
    assert catalogue.generation is not None
    assert catalogue.generation == _current_token(manager, vault_root)
    assert reserved_paths.revalidate_identity_catalogue_generation(vault_root) is True


def test_c6_a_follower_that_outwaits_its_budget_gets_the_typed_unavailable_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C6: the bounded wait ends in the typed outcome, never in a second build."""

    vault_root = _vault(tmp_path)
    _manager(tmp_path, monkeypatch)
    monkeypatch.setattr(reserved_paths, "_FLIGHT_WAIT_SECONDS", 0.05)

    builds = 0
    build_entered = threading.Event()
    release_build = threading.Event()
    original_from_vault = reserved_paths.IdentityCatalogue.from_vault.__func__

    def parked_from_vault(
        cls: type[reserved_paths.IdentityCatalogue], root: Path
    ) -> reserved_paths.IdentityCatalogue:
        nonlocal builds
        builds += 1
        build_entered.set()
        assert release_build.wait(20)
        return original_from_vault(cls, root)

    monkeypatch.setattr(
        reserved_paths.IdentityCatalogue,
        "from_vault",
        classmethod(parked_from_vault),
    )

    leader_failures: list[BaseException] = []

    def lead() -> None:
        try:
            reserved_paths._baseline_identity_catalogue(vault_root)
        except BaseException as error:  # noqa: BLE001 - preserve worker failure
            leader_failures.append(error)

    leader = threading.Thread(target=lead, daemon=True)
    leader.start()
    assert build_entered.wait(20)

    with pytest.raises(reserved_paths.ReservedPathLeafError) as error:
        reserved_paths._baseline_identity_catalogue(vault_root)
    assert error.value.code == "CAPABILITY_UNAVAILABLE"
    assert builds == 1

    release_build.set()
    leader.join(20)
    assert not leader.is_alive()
    assert leader_failures == []
    assert builds == 1


def _warm_thread() -> threading.Thread | None:
    return next(
        (
            thread
            for thread in threading.enumerate()
            if thread.name == "exomem-identity-catalogue-warm"
        ),
        None,
    )


def test_warm_scheduler_builds_off_the_calling_thread_and_returns_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scheduler hands the cold walk to a background thread and returns."""

    vault_root = _vault(tmp_path)
    _manager(tmp_path, monkeypatch)

    builds = 0
    build_entered = threading.Event()
    release_build = threading.Event()
    build_threads: list[str] = []
    original_from_vault = reserved_paths.IdentityCatalogue.from_vault.__func__

    def parked_from_vault(
        cls: type[reserved_paths.IdentityCatalogue], root: Path
    ) -> reserved_paths.IdentityCatalogue:
        nonlocal builds
        builds += 1
        build_threads.append(threading.current_thread().name)
        build_entered.set()
        assert release_build.wait(20)
        return original_from_vault(cls, root)

    monkeypatch.setattr(
        reserved_paths.IdentityCatalogue,
        "from_vault",
        classmethod(parked_from_vault),
    )

    caller = threading.current_thread().name
    assert reserved_paths.schedule_identity_catalogue_warm(vault_root) is None
    # The caller is back while the walk is still parked: it did not pay for it.
    assert build_entered.wait(20)
    assert reserved_paths.identity_catalogue_ready(vault_root) is False

    release_build.set()
    thread = _warm_thread()
    assert thread is not None
    thread.join(20)

    assert build_threads == ["exomem-identity-catalogue-warm"]
    assert caller not in build_threads
    assert builds == 1
    assert reserved_paths.identity_catalogue_ready(vault_root) is True


def test_warm_scheduler_starts_nothing_when_warm_or_already_in_flight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inventory that exists, or is already being built, needs no second walk."""

    vault_root = _vault(tmp_path)
    _manager(tmp_path, monkeypatch)

    builds = 0
    build_entered = threading.Event()
    release_build = threading.Event()
    original_from_vault = reserved_paths.IdentityCatalogue.from_vault.__func__

    def parked_from_vault(
        cls: type[reserved_paths.IdentityCatalogue], root: Path
    ) -> reserved_paths.IdentityCatalogue:
        nonlocal builds
        builds += 1
        build_entered.set()
        assert release_build.wait(20)
        return original_from_vault(cls, root)

    monkeypatch.setattr(
        reserved_paths.IdentityCatalogue,
        "from_vault",
        classmethod(parked_from_vault),
    )

    leader = threading.Thread(
        target=lambda: reserved_paths._baseline_identity_catalogue(vault_root),
        daemon=True,
    )
    leader.start()
    assert build_entered.wait(20)

    # In flight: the scheduler defers to the build already running.
    reserved_paths.schedule_identity_catalogue_warm(vault_root)
    assert _warm_thread() is None

    release_build.set()
    leader.join(20)
    assert not leader.is_alive()
    assert builds == 1

    # Warm: nothing left to do.
    reserved_paths.schedule_identity_catalogue_warm(vault_root)
    assert _warm_thread() is None
    assert builds == 1


def test_warm_scheduler_releases_the_flight_and_logs_only_the_failure_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A background failure never raises, never leaks detail, never wedges the flight."""

    vault_root = _vault(tmp_path)
    _manager(tmp_path, monkeypatch)

    builds = 0
    original_from_vault = reserved_paths.IdentityCatalogue.from_vault.__func__

    def failing_from_vault(
        cls: type[reserved_paths.IdentityCatalogue], root: Path
    ) -> reserved_paths.IdentityCatalogue:
        nonlocal builds
        builds += 1
        if builds == 1:
            raise RuntimeError("private-detail-that-must-not-be-logged")
        return original_from_vault(cls, root)

    monkeypatch.setattr(
        reserved_paths.IdentityCatalogue,
        "from_vault",
        classmethod(failing_from_vault),
    )

    with caplog.at_level("WARNING", logger="exomem.reserved_paths"):
        reserved_paths.schedule_identity_catalogue_warm(vault_root)
        thread = _warm_thread()
        assert thread is not None
        thread.join(20)

    assert "RuntimeError" in caplog.text
    assert "private-detail-that-must-not-be-logged" not in caplog.text
    assert reserved_paths.identity_catalogue_ready(vault_root) is False

    # The flight was released, so the next caller builds rather than hanging.
    retried = reserved_paths._baseline_identity_catalogue(vault_root)
    assert retried.generation is not None
    assert builds == 2


def test_f1_an_unreadable_generation_refuses_the_inventory_instead_of_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1: a token that cannot be read is a mismatch, not evidence of currency.

    Raising here would escape into ``promote()`` after it has already taken the
    writer lease, leaving a lease-holding process that never finished promoting.
    """

    vault_root = _vault(tmp_path)
    manager = _manager(tmp_path, monkeypatch)
    reserved_paths._baseline_identity_catalogue(vault_root)
    assert reserved_paths.identity_catalogue_ready(vault_root) is True

    token = writer_lease._reserved_identity_generation_path(
        manager.config.state_dir, vault_root
    )
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("not-a-generation", encoding="ascii")

    assert reserved_paths.revalidate_identity_catalogue_generation(vault_root) is False
    assert reserved_paths.identity_catalogue_ready(vault_root) is False


class _ExplodingLock:
    """Raise once on release, after the flight has been registered."""

    def __init__(self, real) -> None:
        self._real = real
        self.armed = True

    def __enter__(self):
        return self._real.__enter__()

    def __exit__(self, *exc_info):
        result = self._real.__exit__(*exc_info)
        if self.armed and reserved_paths._BASELINE_IDENTITY_FLIGHTS:
            self.armed = False
            raise KeyboardInterrupt("interrupted once the flight was registered")
        return result


def test_f6_a_leader_interrupted_at_registration_leaves_no_orphan_flight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F6: every exit releases the flight, so no caller is permanently refused."""

    vault_root = _vault(tmp_path)
    _manager(tmp_path, monkeypatch, mutation_timeout_seconds=0.05)
    monkeypatch.setattr(reserved_paths, "_FLIGHT_WAIT_SECONDS", 0.05)

    lock = _ExplodingLock(reserved_paths._PUBLISHED_IDENTITY_LOCK)
    monkeypatch.setattr(reserved_paths, "_PUBLISHED_IDENTITY_LOCK", lock)

    with pytest.raises(KeyboardInterrupt):
        reserved_paths._baseline_identity_catalogue(vault_root)

    assert lock.armed is False
    assert reserved_paths._BASELINE_IDENTITY_FLIGHTS == {}

    # The next caller builds rather than waiting on a flight nobody will finish.
    catalogue = reserved_paths._baseline_identity_catalogue(vault_root)
    assert catalogue.generation is not None
    assert reserved_paths.identity_catalogue_ready(vault_root) is True


def test_f2_the_flight_wait_respects_a_request_deadline_but_not_the_gate_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2: a bound request deadline caps the wait; an unbound caller waits it out.

    The follower waits on an in-process Event, holding no gate and blocking no
    writer, so the gate budget is the wrong ceiling: a cold build outlives it and
    every concurrent reader but the leader would be refused while it runs.
    """

    vault_root = _vault(tmp_path)
    _manager(tmp_path, monkeypatch, mutation_timeout_seconds=0.05)

    builds = 0
    build_entered = threading.Event()
    release_build = threading.Event()
    original_from_vault = reserved_paths.IdentityCatalogue.from_vault.__func__

    def parked_from_vault(
        cls: type[reserved_paths.IdentityCatalogue], root: Path
    ) -> reserved_paths.IdentityCatalogue:
        nonlocal builds
        builds += 1
        build_entered.set()
        assert release_build.wait(20)
        return original_from_vault(cls, root)

    monkeypatch.setattr(
        reserved_paths.IdentityCatalogue,
        "from_vault",
        classmethod(parked_from_vault),
    )

    leader = threading.Thread(
        target=lambda: reserved_paths._baseline_identity_catalogue(vault_root),
        daemon=True,
    )
    leader.start()
    assert build_entered.wait(20)

    # A caller whose own deadline has run out is refused inside that deadline,
    # not at the flight ceiling.
    spent = request_budget.RequestBudget(seconds=0.0)
    assert spent.remaining() == 0.0
    token = request_budget.set_current(spent)
    try:
        with pytest.raises(reserved_paths.ReservedPathLeafError) as error:
            reserved_paths._baseline_identity_catalogue(vault_root)
        assert error.value.code == "CAPABILITY_UNAVAILABLE"
    finally:
        request_budget.reset_current(token)

    # A caller with no bound budget waits for the build instead of being refused
    # by the 5 s gate budget this deliberately does not use.
    assert reserved_paths._flight_wait_seconds() == reserved_paths._FLIGHT_WAIT_SECONDS
    assert reserved_paths._FLIGHT_WAIT_SECONDS > 12.0

    waited: list[reserved_paths.IdentityCatalogue] = []
    follower = threading.Thread(
        target=lambda: waited.append(
            reserved_paths._baseline_identity_catalogue(vault_root)
        ),
        daemon=True,
    )
    follower.start()
    release_build.set()
    follower.join(20)
    leader.join(20)

    assert not follower.is_alive()
    assert not leader.is_alive()
    assert len(waited) == 1
    assert waited[0].generation is not None
    assert builds == 1


def test_f1b_a_refused_coordination_scope_refuses_the_inventory_instead_of_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gate we cannot enter is not evidence of currency either.

    Reading the token under the coordination scope made the scope's own refusal
    reachable from revalidation. Escaping here would strand promotion after it
    had already taken the writer lease, exactly as an unreadable token would.
    """

    from exomem.cli_ops import OpError

    vault_root = _vault(tmp_path)
    _manager(tmp_path, monkeypatch)
    reserved_paths._baseline_identity_catalogue(vault_root)
    assert reserved_paths.identity_catalogue_ready(vault_root) is True

    def refused_scope(*_args: object, **_kwargs: object):
        raise OpError(
            "MUTATION_BUSY",
            "vault mutation boundary is busy",
            "Retry after the current mutation completes.",
        )

    monkeypatch.setattr(
        reserved_paths, "_identity_coordination_scope", refused_scope
    )

    assert reserved_paths.revalidate_identity_catalogue_generation(vault_root) is False
    assert reserved_paths.identity_catalogue_ready(vault_root) is False


def test_r1_revalidation_imports_nothing_at_call_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1: a missing dependency must fail at startup, not mid-promotion.

    An import inside the call sits outside the ``try`` it serves, so an
    ``ImportError`` would escape revalidation after the writer lease was taken
    -- the exact failure mode the catch one line below it exists to prevent.
    """

    vault_root = _vault(tmp_path)
    _manager(tmp_path, monkeypatch)
    reserved_paths._baseline_identity_catalogue(vault_root)

    # Anything this function still imports at call time now fails to resolve.
    monkeypatch.setitem(sys.modules, "exomem.cli_ops", None)

    assert reserved_paths.revalidate_identity_catalogue_generation(vault_root) is True
    assert reserved_paths.identity_catalogue_ready(vault_root) is True
