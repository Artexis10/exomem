"""Issue #576: an interactive write must not park on a full-corpus graph rebuild.

Three contracts live here, because the production livelock needed all three at
once:

1. **The join is bounded, at every site that holds a request.** `writer_lease`
   joined `graph_sync.wait_for_registered` with no timeout, so a committed write
   returned only when the `exomem-graph-rebuild` daemon published or died --
   75-155 s against a ~750 ms commit budget, and 300 s for the worst
   `observe_memory` measured. There were *two* such joins,
   `after_operation_guard` and `mutation_guard`; both are covered here, and
   `test_every_graph_join_site_is_bounded_or_declared` fails if a third appears.
   The canonical bytes are durable at `canonical_files_committed`; derived graph
   work is self-healing.
2. **The bounded return is honest.** Returning fast while implying a fresh graph
   would be worse than returning slow. A write that leaves before the registered
   rebuild converges says so, in the same `graph_sync*` vocabulary a failure
   already uses, and it says so in the *compact* projection, which is the
   default response detail.
3. **A rebuild invalidated by a moving projection re-targets.** Two stabilization
   attempts cannot converge against a corpus still being written to, and the
   Class C failure is exactly what strands the availability marker and forces the
   *next* write down `fallback()` into another full rebuild. That is the loop;
   `test_convergence_republishes_the_marker_...` is the test that proves it
   broken.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from exomem import epistemic_graph, freshness, graph_sync
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.epistemic_graph import EpistemicGraphIndex

PAGE_A = "Knowledge Base/Notes/Insights/bounded-join-a.md"
PAGE_B = "Knowledge Base/Notes/Insights/bounded-join-b.md"


def _page(title: str, body: str) -> str:
    return f"---\ntype: insight\nstatus: active\n---\n# {title}\n\n## Claim\n\n{body}\n"


def _seed_live_freshness(root: Path) -> None:
    freshness.seed(
        root,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(root)),
    )
    kb = root / "Knowledge Base"
    freshness.seed(
        root,
        "kb",
        ((str(path), freshness.stat_signature(path)) for path in find_module._walk_md(kb)),
    )


@pytest.fixture
def vault(tmp_path: Path) -> Any:
    root = tmp_path / "vault"
    (root / "Knowledge Base/Notes/Insights").mkdir(parents=True)
    (root / PAGE_A).write_text(
        _page("A", "A claims against [[bounded-join-b]]."), encoding="utf-8"
    )
    (root / PAGE_B).write_text(_page("B", "B is a plain claim."), encoding="utf-8")
    _seed_live_freshness(root)
    EpistemicGraphIndex(root).rebuild_all()
    epistemic_graph.clear_publication_memos()
    yield root
    epistemic_graph.clear_publication_memos()


def _checkpoint(generation: int) -> graph_sync.GraphSyncCheckpoint:
    return graph_sync.GraphSyncCheckpoint.create(
        generation=generation,
        mutation_id=f"{generation:024x}",
        paths=((PAGE_A, "d" * 64),),
        created_paths=(PAGE_A,),
    )


def _invoke_with_registered_rebuild(
    vault_root: Path,
    builder: Any,
    *,
    checkpoint: graph_sync.GraphSyncCheckpoint | None = None,
    name: str = "remember",
    kwargs: dict[str, Any] | None = None,
) -> Any:
    """Drive a real committed mutation whose leaf registers `builder`."""
    from exomem.writer_lease import LeaseConfig, LeaseManager, mark_active_mutation_committed

    required = checkpoint or _checkpoint(1)
    state_dir = vault_root / "state"
    manager = LeaseManager(LeaseConfig(state_dir=state_dir))

    def leaf(root: Path, **_kwargs: Any) -> dict[str, Any]:
        (root / PAGE_A).write_text(
            _page("A", "A now claims something else."), encoding="utf-8"
        )
        mark_active_mutation_committed()
        graph_sync.register_rebuild(root, required, builder, state_root=state_dir)
        return {"status": "committed", "mutated": True}

    command = SimpleNamespace(name=name, read_only=False, leaf=leaf)
    return manager.invoke(command, (vault_root,), dict(kwargs or {}))


# --- 1. The join is bounded --------------------------------------------------


def test_interactive_write_returns_while_a_registered_rebuild_is_still_running(
    vault: Path,
) -> None:
    """The write returns within the bound; the rebuild keeps running behind it."""
    entered = threading.Event()
    release = threading.Event()
    published = threading.Event()

    def build(required: graph_sync.GraphSyncCheckpoint) -> graph_sync.GraphBuildOutcome:
        entered.set()
        # Far longer than the bound, so a pass here cannot come from a lucky
        # race with a rebuild that happened to be quick.
        assert release.wait(30)
        published.set()
        return graph_sync.GraphBuildOutcome.covering(required)

    started = time.monotonic()
    try:
        result = _invoke_with_registered_rebuild(vault, build)
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert entered.is_set() is True, "the registered rebuild must still be started"
    assert published.is_set() is False, "the write must not have waited for publication"
    # A literal, not the bound constant: the point is the *behaviour*, and this
    # assertion has to be able to fail on a tree where the constant does not
    # exist yet. 15 s sits well above the bound plus process overhead and well
    # below the 30 s the builder holds, so neither side is a coin flip.
    assert elapsed < 15.0, (
        f"interactive write parked {elapsed:.1f}s on a registered rebuild"
    )
    assert result["state"] == "committed"


def test_a_direct_mutation_guard_also_returns_while_its_rebuild_runs(
    vault: Path,
) -> None:
    """The second unbounded join, and the worst case measured in production.

    `mutation_guard` joins its own registered rebuild after canonical release,
    on a path `after_operation_guard` never sees -- which is how a single-unit
    `observe_memory` append blocked for 300 s while the `remember` before it
    took 14.3 s. Same defect, second site, so the same bound has to reach it.
    """
    from exomem.writer_lease import LeaseConfig, LeaseManager

    entered = threading.Event()
    release = threading.Event()
    published = threading.Event()

    def build(required: graph_sync.GraphSyncCheckpoint) -> graph_sync.GraphBuildOutcome:
        entered.set()
        assert release.wait(30)
        published.set()
        return graph_sync.GraphBuildOutcome.covering(required)

    state_dir = vault / "state"
    manager = LeaseManager(LeaseConfig(state_dir=state_dir))
    started = time.monotonic()
    try:
        with manager.mutation_guard(vault):
            (vault / PAGE_A).write_text(
                _page("A", "A now claims something else."), encoding="utf-8"
            )
            graph_sync.register_rebuild(
                vault, _checkpoint(1), build, state_root=state_dir
            )
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert entered.is_set() is True, "the registered rebuild must still be started"
    assert published.is_set() is False, "the guard must not have waited for publication"
    assert elapsed < 15.0, (
        f"direct mutation guard parked {elapsed:.1f}s on a registered rebuild"
    )


#: Every `wait_for_registered` caller in `src/exomem/`, and why it is or is not
#: bounded. The first pass at #576 bounded one site and left an identical
#: unbounded join two hundred lines away in the same file, which then produced
#: the worst case in production. An enumeration is the only thing that makes a
#: *third* site fail loudly instead of surviving another analysis.
_DECLARED_JOIN_SITES = {
    # Bounded: held while serving a request.
    "writer_lease.py": "bounded via join_registered_bounded, plus the reconcile opt-out",
    # Unbounded by design: `reconcile`'s terminal exists to prove the graph is
    # readable, so it must not return before the rebuild lands.
    "reconcile.py": "reconcile opt-in; its terminal asserts graph currency",
    # Unbounded by design: the standalone library path, gated on
    # `active_mutation_request_id() is None` and no active direct guard, so it
    # is unreachable while any request is being served. It has no envelope to
    # carry `pending` and its contract is a converged result.
    "epistemic_graph.py": "standalone library join, no request boundary held",
    "delete_file.py": "standalone library join, no request boundary held",
    "delete_directory.py": "standalone library join, no request boundary held",
    "recover_from_trash.py": "standalone library join, no request boundary held",
}


def test_every_graph_join_site_is_bounded_or_declared() -> None:
    source = Path(epistemic_graph.__file__).parent
    found = {
        path.name
        for path in sorted(source.glob("*.py"))
        if "wait_for_registered(" in path.read_text(encoding="utf-8")
        and path.name != "graph_sync.py"  # the definition and the helper itself
    }

    assert found == set(_DECLARED_JOIN_SITES), (
        "a graph rebuild join site appeared or moved: classify it as bounded "
        "(graph_sync.join_registered_bounded) or declare why it may block"
    )


def test_the_bounded_helper_applies_the_interactive_bound(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one seam every request-serving join goes through."""
    observed: list[float | None] = []

    def never_finishes(
        vault_root: Path, timeout: float | None = None, *, state_root: Path | None = None
    ) -> Any:
        observed.append(timeout)
        raise TimeoutError("graph rebuild did not finish before the wait deadline")

    monkeypatch.setattr(graph_sync, "wait_for_registered", never_finishes)

    assert graph_sync.join_registered_bounded(vault) is False
    assert observed == [graph_sync.INTERACTIVE_GRAPH_JOIN_TIMEOUT_SECONDS]


def test_the_bounded_helper_does_not_launder_a_real_failure_into_pending(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`False` must mean "still running", never "failed"."""

    def fails(
        vault_root: Path, timeout: float | None = None, *, state_root: Path | None = None
    ) -> Any:
        raise graph_sync.GraphRebuildRegistrationError(
            "GRAPH_SYNC_HANDOFF_MISSING", "Run reconcile to recover the derived graph."
        )

    monkeypatch.setattr(graph_sync, "wait_for_registered", fails)

    with pytest.raises(graph_sync.GraphRebuildRegistrationError):
        graph_sync.join_registered_bounded(vault)


# --- 2. The bounded return is honest ----------------------------------------


def test_a_write_that_leaves_before_convergence_says_the_graph_is_catching_up(
    vault: Path,
) -> None:
    release = threading.Event()

    def build(required: graph_sync.GraphSyncCheckpoint) -> graph_sync.GraphBuildOutcome:
        assert release.wait(30)
        return graph_sync.GraphBuildOutcome.covering(required)

    required = _checkpoint(1)
    try:
        compact = _invoke_with_registered_rebuild(vault, build, checkpoint=required)
    finally:
        release.set()

    assert compact["graph_sync"] == "pending"
    assert compact["graph_sync_code"] == "GRAPH_SYNC_REBUILD_IN_PROGRESS"
    assert compact["graph_sync_checkpoint"] == required.checkpoint_sha256
    assert isinstance(compact["graph_sync_remediation"], str)
    assert compact["graph_sync_remediation"]


def test_a_write_whose_rebuild_converges_in_time_does_not_claim_to_be_pending(
    vault: Path,
) -> None:
    """The honest signal is not a blanket downgrade: fast work still completes."""

    def build(required: graph_sync.GraphSyncCheckpoint) -> graph_sync.GraphBuildOutcome:
        return graph_sync.GraphBuildOutcome.covering(required)

    compact = _invoke_with_registered_rebuild(vault, build)

    assert compact["graph_sync"] == "completed"
    assert "graph_sync_code" not in compact


def test_a_pending_graph_outcome_survives_the_compact_projection() -> None:
    """`compact` is the default response detail, so the signal has to reach it.

    `project_terminal` whitelists the `graph_sync` values a client may branch
    on. A new value the projection silently drops is indistinguishable from
    saying nothing, which is precisely the dishonest fast write this change
    exists to avoid.
    """
    from exomem.mutation_terminal import committed_terminal, project_terminal

    required = _checkpoint(3)
    pending = graph_sync.committed_graph_pending(required)
    terminal = committed_terminal(
        {"status": "committed", **pending},
        request_id="11111111-1111-4111-8111-111111111111",
        receipt_id=None,
        idempotency_key=None,
    )

    compact = project_terminal({**terminal, **pending}, detail="compact")

    assert compact["graph_sync"] == "pending"
    assert compact["graph_sync_code"] == "GRAPH_SYNC_REBUILD_IN_PROGRESS"
    assert compact["graph_sync_checkpoint"] == required.checkpoint_sha256


def test_a_caller_that_must_block_opts_in_rather_than_every_write_paying(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`reconcile` exists to make the graph current, so it still joins unbounded."""
    observed: list[float | None] = []
    original = graph_sync.wait_for_registered

    def record(
        vault_root: Path, timeout: float | None = None, *, state_root: Path | None = None
    ) -> Any:
        observed.append(timeout)
        return original(vault_root, timeout, state_root=state_root)

    def build(required: graph_sync.GraphSyncCheckpoint) -> graph_sync.GraphBuildOutcome:
        return graph_sync.GraphBuildOutcome.covering(required)

    monkeypatch.setattr(graph_sync, "wait_for_registered", record)
    _invoke_with_registered_rebuild(vault, build, name="remember")
    _invoke_with_registered_rebuild(
        vault,
        build,
        checkpoint=_checkpoint(2),
        name="maintain_memory",
        kwargs={"mode": "reconcile"},
    )

    assert observed[0] == graph_sync.INTERACTIVE_GRAPH_JOIN_TIMEOUT_SECONDS
    assert observed[-1] is None


# --- 3. A moving projection re-targets the newer baseline -------------------


def _move_the_projection_identity_for(
    monkeypatch: pytest.MonkeyPatch, *, passes: int | None
) -> dict[str, int]:
    """Move the recall projection across the first `passes` stabilization attempts.

    This is the exact production cause: `moved_cause` reads "the recall
    projection identity moved across the pass". `_rebuild_all_locked` samples
    the identity twice per attempt (`before`, then `after_identity`), so moving
    only the even-numbered sample invalidates that attempt and leaves the next
    one a clean, newer baseline to re-target against. `passes=None` never
    settles.
    """
    real = epistemic_graph._recall_projection_identity
    calls = {"n": 0}

    def moving(vault_root: Path, *, disk_freshness: tuple[int, int, str]) -> Any:
        identity = real(vault_root, disk_freshness=disk_freshness)
        calls["n"] += 1
        invalidated = calls["n"] // 2 if calls["n"] % 2 == 0 else None
        if invalidated is not None and (passes is None or invalidated <= passes):
            return (*identity[:-1], f"moved-{calls['n']}")
        return identity

    monkeypatch.setattr(epistemic_graph, "_recall_projection_identity", moving)
    return calls


def test_a_projection_that_moves_twice_still_converges_instead_of_raising(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two attempts cannot converge against a corpus still being written to."""
    calls = _move_the_projection_identity_for(monkeypatch, passes=2)

    report = EpistemicGraphIndex(vault)._rebuild_all_locked()

    assert report["indexed_files"] >= 1
    assert calls["n"] > 2 * epistemic_graph.REBUILD_STABILIZATION_ATTEMPTS


# --- 4. Continuous writes must not become an unbounded restart loop ---------


def test_continuous_writes_terminate_on_the_attempt_ceiling_and_stay_class_c(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _move_the_projection_identity_for(monkeypatch, passes=None)

    started = time.monotonic()
    with pytest.raises(epistemic_graph.GraphProjectionMoved) as raised:
        EpistemicGraphIndex(vault)._rebuild_all_locked()
    elapsed = time.monotonic() - started

    message = str(raised.value)
    assert type(raised.value) is epistemic_graph.GraphProjectionMoved
    assert "Class C" in message
    assert "projection moved" in message
    assert calls["n"] <= 2 * epistemic_graph.REBUILD_STABILIZATION_MAX_ATTEMPTS
    assert elapsed < 30.0


def test_a_slow_rebuild_terminates_on_the_elapsed_deadline_not_the_ceiling(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wall-clock bound is what protects a big corpus under continuous writes.

    An attempt ceiling alone is not a bound when one full-corpus pass costs
    20-175 s. With the deadline already spent, the run must stop at the
    `REBUILD_STABILIZATION_ATTEMPTS` floor -- never fewer than today.
    """
    calls = _move_the_projection_identity_for(monkeypatch, passes=None)
    monkeypatch.setattr(epistemic_graph, "REBUILD_STABILIZATION_DEADLINE_SECONDS", 0.0)

    with pytest.raises(epistemic_graph.GraphProjectionMoved):
        EpistemicGraphIndex(vault)._rebuild_all_locked()

    assert calls["n"] == 2 * epistemic_graph.REBUILD_STABILIZATION_ATTEMPTS


# --- 5. The marker is republished, so the next write is incremental ---------


def test_convergence_republishes_the_marker_so_the_next_write_is_incremental(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The livelock: Class C strands the marker, so every later write rebuilds.

    On `origin/main` a projection that moves under the first two passes exhausts
    stabilization, raises Class C, leaves the sidecar unavailable and marks the
    registry externally pending -- which is exactly what sends the *next* write
    back down `fallback()` into another full rebuild, self-sustaining. The
    re-target lets the same run converge, publish the availability marker, and
    leave no external-pending epoch behind, so the next write can take the
    incremental path.
    """
    _move_the_projection_identity_for(monkeypatch, passes=2)

    EpistemicGraphIndex(vault)._rebuild_all_locked()

    assert freshness.external_pending(vault) is False
    assert EpistemicGraphIndex(vault).available() is True


# --- 6. Regression: a genuinely broken projection still fails as today ------


def test_a_genuinely_broken_projection_still_fails_the_way_it_does_today(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolver that never names its bytes stays Class C, marked exactly once."""
    monkeypatch.setattr(
        EpistemicGraphIndex, "_resolver_source_versions", lambda *_args, **_kwargs: None
    )
    epoch_probe = tmp_path / "epoch-probe-root"
    clock_before = freshness.mark_external_pending(epoch_probe)

    with pytest.raises(epistemic_graph.GraphProjectionMoved, match="did not stabilize") as raised:
        EpistemicGraphIndex(vault)._rebuild_all_locked()

    error = raised.value
    assert type(error) is epistemic_graph.GraphProjectionMoved
    assert epistemic_graph.is_publication_failure(error) is False
    assert epistemic_graph.may_mark_external_pending(error) is False
    assert "resolver bytes" in str(error)
    assert freshness.external_pending(vault) is True
    assert epistemic_graph.publication_refusal_active(vault) is False
    assert freshness.mark_external_pending(epoch_probe) == clock_before + 2
