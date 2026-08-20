from __future__ import annotations

import multiprocessing
import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import graph_sync
from exomem.cli_ops import OpError
from exomem.writer_lease import LeaseConfig, LeaseManager


def _checkpoint(generation: int = 1) -> graph_sync.GraphSyncCheckpoint:
    return graph_sync.GraphSyncCheckpoint.create(
        generation=generation,
        mutation_id=f"{generation:024x}",
        paths=(),
        created_paths=(),
        scope="full",
    )


def _claim_in_child(vault_root: str, temporary: str, claimed) -> None:  # noqa: ANN001
    from exomem import graph_sync as graph_sync_module

    result = graph_sync_module.claim_rebuild_owner(Path(vault_root), Path(temporary))
    claimed.put(result)
    if result:
        graph_sync_module.release_rebuild_owner(Path(vault_root), Path(temporary))


def _claim_in_child_at_root(
    vault_root: str, temporary: str, state_root: str, claimed  # noqa: ANN001
) -> None:
    from exomem import graph_sync as graph_sync_module

    root = Path(state_root)
    result = graph_sync_module.claim_rebuild_owner(
        Path(vault_root), Path(temporary), state_root=root
    )
    claimed.put(result)
    if result:
        graph_sync_module.release_rebuild_owner(
            Path(vault_root), Path(temporary), state_root=root
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode contract")
def test_rebuild_lock_requires_a_trusted_runtime_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_root = tmp_path / "runtime-state"
    state_root.mkdir(mode=0o777)
    state_root.chmod(0o777)
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(state_root))

    with pytest.raises(graph_sync.GraphRebuildLockUnavailable):
        graph_sync.claim_rebuild_owner(tmp_path / "vault", tmp_path / "temporary.sqlite")


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode contract")
def test_rebuild_lock_lives_directly_beneath_trusted_runtime_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_root = tmp_path / "runtime-state"
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(state_root))
    vault_root = tmp_path / "vault"
    temporary = tmp_path / "temporary.sqlite"

    assert graph_sync.claim_rebuild_owner(vault_root, temporary)
    try:
        lock = graph_sync._rebuild_lock_path(vault_root)
        assert lock.parent == state_root
        info = lock.stat()
        assert stat.S_IMODE(info.st_mode) == 0o600
        assert info.st_uid == os.getuid()
        assert lock.is_file()
    finally:
        graph_sync.release_rebuild_owner(vault_root, temporary)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode contract")
def test_rebuild_lock_rejects_an_existing_non_owner_only_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "runtime-state"
    state_root.mkdir(mode=0o700)
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(state_root))
    vault_root = tmp_path / "vault"
    lock = graph_sync._rebuild_lock_path(vault_root)
    lock.write_bytes(b"\0")
    lock.chmod(0o644)

    with pytest.raises(graph_sync.GraphRebuildLockUnavailable):
        graph_sync.claim_rebuild_owner(vault_root, tmp_path / "temporary.sqlite")


def test_legacy_lock_child_has_no_authority_over_the_direct_runtime_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "runtime-state"
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(state_root))
    vault_root = tmp_path / "vault"
    first = tmp_path / "first.sqlite"
    second = tmp_path / "second.sqlite"

    assert graph_sync.claim_rebuild_owner(vault_root, first)
    try:
        legacy_child = state_root / "graph-rebuild-locks"
        legacy_child.mkdir()
        os.replace(legacy_child, state_root / "renamed-legacy-locks")
        context = multiprocessing.get_context("spawn")
        claimed = context.Queue()
        contender = context.Process(
            target=_claim_in_child,
            args=(str(vault_root), str(second), claimed),
        )
        contender.start()
        contender.join(10)
        assert contender.exitcode == 0
        assert claimed.get(timeout=2) is False
    finally:
        graph_sync.release_rebuild_owner(vault_root, first)


def test_explicit_rebuild_runtime_root_overrides_ambient_environment_and_coalesces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ambient_root = tmp_path / "ambient-state"
    manager_root = tmp_path / "manager-state"
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(ambient_root))
    vault_root = tmp_path / "vault"
    first = tmp_path / "first.sqlite"
    second = tmp_path / "second.sqlite"

    assert graph_sync.claim_rebuild_owner(vault_root, first, state_root=manager_root)
    try:
        lock = graph_sync._rebuild_lock_path(vault_root, state_root=manager_root)
        assert lock.parent == manager_root
        assert lock.exists()
        assert not graph_sync._rebuild_lock_path(vault_root).exists()
        context = multiprocessing.get_context("spawn")
        claimed = context.Queue()
        contender = context.Process(
            target=_claim_in_child_at_root,
            args=(str(vault_root), str(second), str(manager_root), claimed),
        )
        contender.start()
        contender.join(10)
        assert contender.exitcode == 0
        assert claimed.get(timeout=2) is False
    finally:
        graph_sync.release_rebuild_owner(vault_root, first, state_root=manager_root)


def test_index_full_rebuild_claim_uses_its_bound_manager_runtime_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import epistemic_graph

    ambient_root = tmp_path / "ambient-state"
    manager_root = tmp_path / "manager-state"
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(ambient_root))
    vault_root = tmp_path / "vault"
    note = vault_root / "Knowledge Base/Notes/bound-rebuild.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Bound rebuild\n", encoding="utf-8")
    manager = LeaseManager(LeaseConfig(state_dir=manager_root))
    captured: dict[str, epistemic_graph.EpistemicGraphIndex] = {}
    command = SimpleNamespace(
        name="bound-rebuild",
        read_only=False,
        leaf=lambda root: captured.setdefault("index", epistemic_graph.EpistemicGraphIndex(root)),
    )

    manager.invoke(command, (vault_root,), {})
    captured["index"].rebuild_all()

    assert graph_sync._rebuild_lock_path(vault_root, state_root=manager_root).exists()
    assert not graph_sync._rebuild_lock_path(vault_root).exists()


def test_explicit_temp_sweep_uses_the_bound_runtime_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ambient_root = tmp_path / "ambient-state"
    manager_root = tmp_path / "manager-state"
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(ambient_root))
    vault_root = tmp_path / "vault"
    live = vault_root / "Knowledge Base/.graph.sqlite"
    live.parent.mkdir(parents=True)
    temporary = graph_sync.temporary_sidecar_path(live, _checkpoint())
    temporary.write_bytes(b"private")

    assert graph_sync.sweep_abandoned_temporaries(
        vault_root, live, live_paths=set(), state_root=manager_root
    ) == [temporary]
    assert graph_sync._rebuild_lock_path(vault_root, state_root=manager_root).exists()
    assert not graph_sync._rebuild_lock_path(vault_root).exists()


def test_overlapping_custom_manager_registrations_do_not_cross_coalesce(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    state_a = tmp_path / "state-a"
    state_b = tmp_path / "state-b"
    first_started = threading.Event()
    release_first = threading.Event()
    observed: list[tuple[str, int]] = []

    def build_a(checkpoint: graph_sync.GraphSyncCheckpoint) -> graph_sync.GraphBuildOutcome:
        observed.append(("a", checkpoint.generation))
        first_started.set()
        assert release_first.wait(2)
        return graph_sync.GraphBuildOutcome.covering(checkpoint)

    def build_b(checkpoint: graph_sync.GraphSyncCheckpoint) -> graph_sync.GraphBuildOutcome:
        observed.append(("b", checkpoint.generation))
        return graph_sync.GraphBuildOutcome.covering(checkpoint)

    first = graph_sync.register_rebuild(
        vault_root, _checkpoint(1), build_a, state_root=state_a
    )
    first.start()
    assert first_started.wait(1)
    second = graph_sync.register_rebuild(
        vault_root, _checkpoint(2), build_b, state_root=state_b
    )
    assert second.wait(1).covers(_checkpoint(2))
    release_first.set()
    assert first.wait(1).covers(_checkpoint(1))
    assert observed == [("a", 1), ("b", 2)]


def test_joined_builder_requires_reader_valid_sidecar_before_claiming_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import epistemic_graph

    vault_root = tmp_path / "vault"
    note = vault_root / "Knowledge Base/Notes/join-proof.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Join proof\n", encoding="utf-8")
    index = epistemic_graph.EpistemicGraphIndex(vault_root)
    index.rebuild_all()
    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=1,
        mutation_id="1" * 24,
        paths=(("Knowledge Base/Notes/join-proof.md", "a" * 64),),
        created_paths=(),
    )
    graph_sync._write_floor(vault_root, graph_sync.GraphSyncGenerationFloor.create(1))
    graph_sync._write_checkpoint(vault_root, checkpoint)
    with index._connect() as connection:
        index._write_graph_sync_acknowledgement(connection, checkpoint)
        connection.execute("DELETE FROM graph_meta WHERE key = 'schema_version'")
        connection.commit()
    assert graph_sync.status(vault_root)["state"] == "current"
    assert index.available() is False
    original_wait = graph_sync.wait_for_current
    monkeypatch.setattr(graph_sync, "claim_rebuild_owner", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        graph_sync,
        "wait_for_current",
        lambda root, required, **kwargs: original_wait(
            root, required, timeout_seconds=0.01, **kwargs
        ),
    )

    with pytest.raises(RuntimeError, match="did not publish a current sidecar"):
        index._rebuild_all_off_boundary()


def test_joined_builder_accepts_a_reader_valid_higher_generation_sidecar(tmp_path: Path) -> None:
    from exomem import epistemic_graph

    vault_root = tmp_path / "vault"
    note = vault_root / "Knowledge Base/Notes/superseded.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Superseded\n", encoding="utf-8")
    required = _checkpoint(1)
    current = _checkpoint(2)
    graph_sync._write_floor(vault_root, graph_sync.GraphSyncGenerationFloor.create(2))
    graph_sync._write_checkpoint(vault_root, current)
    index = epistemic_graph.EpistemicGraphIndex(vault_root)
    index.rebuild_all()

    assert graph_sync.status(vault_root)["state"] == "current"
    assert index.available()
    assert graph_sync.wait_for_current(
        vault_root, required, availability=index.available, timeout_seconds=0.1
    )


def test_pending_registrations_are_isolated_by_runtime_root(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    state_a = tmp_path / "state-a"
    state_b = tmp_path / "state-b"
    first = graph_sync.register_rebuild(
        vault_root,
        _checkpoint(1),
        lambda checkpoint: graph_sync.GraphBuildOutcome.covering(checkpoint),
        state_root=state_a,
    )
    second = graph_sync.register_rebuild(
        vault_root,
        _checkpoint(2),
        lambda checkpoint: graph_sync.GraphBuildOutcome.covering(checkpoint),
        state_root=state_b,
    )

    assert graph_sync.registered_checkpoint(vault_root, state_root=state_a) == _checkpoint(1)
    assert graph_sync.registered_checkpoint(vault_root, state_root=state_b) == _checkpoint(2)
    assert graph_sync.start_registered(vault_root, state_root=state_a) is not None
    assert graph_sync.start_registered(vault_root, state_root=state_b) is not None
    assert graph_sync.wait_for_registered(vault_root, state_root=state_a).covers(_checkpoint(1))
    assert graph_sync.wait_for_registered(vault_root, state_root=state_b).covers(_checkpoint(2))
    assert first.wait(1).covers(_checkpoint(1))
    assert second.wait(1).covers(_checkpoint(2))


def test_registration_start_does_not_consume_waiter_capacity(tmp_path: Path) -> None:
    coordinator = graph_sync.GraphRebuildCoordinator(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def build(checkpoint: graph_sync.GraphSyncCheckpoint) -> graph_sync.GraphBuildOutcome:
        entered.set()
        assert release.wait(2)
        return graph_sync.GraphBuildOutcome.covering(checkpoint)

    registration = graph_sync.GraphRebuildRegistration(coordinator, _checkpoint(), build)
    registration.start()
    assert entered.wait(1)
    for _ in range(255):
        registration.start()
    assert coordinator.waiter_count == 0
    release.set()


def test_join_releases_waiter_capacity_after_timeout(tmp_path: Path) -> None:
    coordinator = graph_sync.GraphRebuildCoordinator(tmp_path)
    release = threading.Event()

    def build(checkpoint: graph_sync.GraphSyncCheckpoint) -> graph_sync.GraphBuildOutcome:
        assert release.wait(2)
        return graph_sync.GraphBuildOutcome.covering(checkpoint)

    coordinator.ensure_started(_checkpoint(), build)
    with pytest.raises(TimeoutError):
        coordinator.join(_checkpoint()).wait(0.01)
    assert coordinator.waiter_count == 0
    release.set()


def test_full_rebuild_constructor_failure_releases_owned_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import epistemic_graph

    index = epistemic_graph.EpistemicGraphIndex(tmp_path)

    class BrokenTemporaryIndex:
        def __init__(self, _vault_root: Path, **_kwargs: object) -> None:
            raise RuntimeError("temporary index construction failed")

    monkeypatch.setattr(epistemic_graph, "EpistemicGraphIndex", BrokenTemporaryIndex)
    with pytest.raises(RuntimeError, match="temporary index construction failed"):
        index._rebuild_all_off_boundary()

    assert not graph_sync._REBUILD_LOCK_HANDLES
    assert not graph_sync.live_temporary_paths()
    assert not list((tmp_path / "Knowledge Base").glob(".graph-rebuild-*.sqlite"))


def test_temp_sweep_never_removes_an_in_process_registered_temp(tmp_path: Path) -> None:
    live = tmp_path / "Knowledge Base/.graph.sqlite"
    live.parent.mkdir(parents=True)
    temporary = graph_sync.temporary_sidecar_path(live, _checkpoint())
    temporary.write_bytes(b"private")
    graph_sync.register_temporary(temporary)
    try:
        assert graph_sync.sweep_abandoned_temporaries(tmp_path, live, live_paths=set()) == []
        assert temporary.exists()
    finally:
        graph_sync.unregister_temporary(temporary)


def test_final_graph_publish_uses_the_lease_manager_mutation_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import epistemic_graph, writer_lease

    state_root = tmp_path / "configured-state"
    vault_root = tmp_path / "vault"
    manager = LeaseManager(
        LeaseConfig(state_dir=state_root), mutation_timeout_seconds=0.01
    )
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    index = epistemic_graph.EpistemicGraphIndex(vault_root)
    publisher = index._canonical_mutation_coordinator()
    entered = threading.Event()
    release = threading.Event()

    def hold_final_publish() -> None:
        with publisher.hold(operation="epistemic_graph_publish_rebuild", holder_kind="graph"):
            entered.set()
            assert release.wait(2)

    thread = threading.Thread(target=hold_final_publish)
    thread.start()
    assert entered.wait(1)
    try:
        with pytest.raises(OpError, match="MUTATION_BUSY"):
            with manager.mutation_guard(vault_root):
                pytest.fail("writer entered during final graph publication")
    finally:
        release.set()
        thread.join(timeout=2)
    assert not thread.is_alive()
    assert publisher.state_root == state_root


def test_registered_builder_retains_the_originating_custom_lease_boundary(
    tmp_path: Path,
) -> None:
    from exomem import epistemic_graph

    state_root = tmp_path / "custom-state"
    vault_root = tmp_path / "vault"
    manager = LeaseManager(LeaseConfig(state_dir=state_root))
    captured: dict[str, epistemic_graph.EpistemicGraphIndex] = {}
    command = SimpleNamespace(
        name="graph-boundary-test",
        read_only=False,
        leaf=lambda root: captured.setdefault("index", epistemic_graph.EpistemicGraphIndex(root)),
    )

    manager.invoke(command, (vault_root,), {})
    index = captured["index"]
    observed_roots: list[Path] = []
    registration = graph_sync.register_rebuild(
        vault_root,
        _checkpoint(),
        lambda checkpoint: (
            observed_roots.append(index._canonical_mutation_coordinator().state_root),
            graph_sync.GraphBuildOutcome.covering(checkpoint),
        )[1],
    )

    assert registration.wait(1).covers(_checkpoint())
    assert observed_roots == [state_root]


# Deadlock valves for the ordering test below, sized so the hold outlasts the
# observation. A whole-vault rebuild on a loaded Windows runner is the thing
# being waited on, and two seconds does not bound it.
_HOLD_SECONDS = 45.0
_OBSERVE_SECONDS = 30.0
_SWAP_WAIT_SECONDS = 30.0


def test_graph_swap_waits_for_a_real_lease_manager_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import epistemic_graph, writer_lease

    state_root = tmp_path / "configured-state"
    vault_root = tmp_path / "vault"
    note = vault_root / "Knowledge Base/Notes/graph-race.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Graph race\n", encoding="utf-8")
    # How long the graph swap will wait for the writer's lease. It only has to
    # outlast the gap between the private build finishing and this thread
    # getting scheduled to release the writer -- microseconds when the machine
    # is idle, and unbounded when it is not. One second made a busy runner look
    # like a swap that refused to wait.
    manager = LeaseManager(
        LeaseConfig(state_dir=state_root), mutation_timeout_seconds=_SWAP_WAIT_SECONDS
    )
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    index = epistemic_graph.EpistemicGraphIndex(vault_root)
    writer_entered = threading.Event()
    release_writer = threading.Event()
    private_build_finished = threading.Event()
    publish_entered = threading.Event()
    rebuilt: list[object] = []

    def hold_writer() -> None:
        with manager.mutation_guard(vault_root):
            writer_entered.set()
            # The hold has to outlast the observation below. If it expires
            # first the writer releases on its own, the swap publishes, and
            # `assert not publish_entered.is_set()` fails -- reporting a
            # publication that raced the writer when really the test let go.
            assert release_writer.wait(_HOLD_SECONDS)

    def publication_seam(_temporary: Path, _live: Path) -> None:
        publish_entered.set()

    original_rebuild = epistemic_graph.EpistemicGraphIndex._rebuild_all_locked

    def private_rebuild_finished(self):  # noqa: ANN001
        report = original_rebuild(self)
        private_build_finished.set()
        return report

    index._before_publish_replacement = publication_seam  # type: ignore[method-assign]
    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex, "_rebuild_all_locked", private_rebuild_finished
    )
    writer = threading.Thread(target=hold_writer)
    writer.start()
    assert writer_entered.wait(_OBSERVE_SECONDS)
    rebuilder = threading.Thread(target=lambda: rebuilt.append(index.rebuild_all()))
    rebuilder.start()
    try:
        assert private_build_finished.wait(_OBSERVE_SECONDS)
        assert not publish_entered.is_set()
    finally:
        release_writer.set()
        writer.join(timeout=_OBSERVE_SECONDS)
        rebuilder.join(timeout=_HOLD_SECONDS)
    assert not writer.is_alive()
    assert not rebuilder.is_alive()
    assert publish_entered.is_set()
    assert rebuilt
