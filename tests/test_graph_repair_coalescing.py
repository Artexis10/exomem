"""Whole-vault graph repair coalesces: one publication retires the debt it covers.

A whole-vault rebuild is owed through one durable marker, and more than one
actor can run the rebuild that pays it: the exact-checkpoint coordinator a
write registers, the drain's full-marker dispatcher, barrier recovery. Until
now only the dispatcher retired the marker, and only after a rebuild it ran
itself. So whenever the coordinator published first -- the ordinary order,
because a write starts it straight away and the drain waits out a debounce --
the marker outlived the publication that covered it, and the drain then spent
a second whole-vault pass on a graph that was already current.

The live service showed exactly that pair: a coordinator publication followed
within a minute by a drain publication for the same generation, each a
whole-vault pass. On a vault of a few thousand pages that is tens of seconds of
CPU per write burst, spent on nothing.

The rule these tests pin: every whole-vault publication retires the marker it
observed before sampling its epoch, after it leaves the publication hold, and
only if no debt was raised since -- including a repeat that left the marker's
value unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from exomem import deferred_index, epistemic_graph, freshness, graph_sync
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.epistemic_graph import EpistemicGraphIndex

PAGE_A = "Knowledge Base/Notes/Insights/coalesce-a.md"
PAGE_B = "Knowledge Base/Notes/Insights/coalesce-b.md"


def _page(title: str, body: str) -> str:
    return f"---\ntype: insight\nstatus: active\n---\n# {title}\n\n## Claim\n\n{body}\n"


def _seed_live_freshness(root: Path) -> None:
    freshness.seed(
        root,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(root)),
    )
    freshness.seed(
        root,
        "kb",
        (
            (str(path), freshness.stat_signature(path))
            for path in find_module._walk_md(root / "Knowledge Base")
        ),
    )


@pytest.fixture
def vault(tmp_path: Path) -> Any:
    root = tmp_path / "vault"
    (root / "Knowledge Base/Notes/Insights").mkdir(parents=True)
    (root / PAGE_A).write_text(_page("A", "A claims against [[coalesce-b]]."), encoding="utf-8")
    (root / PAGE_B).write_text(_page("B", "B is a plain claim."), encoding="utf-8")
    _seed_live_freshness(root)
    EpistemicGraphIndex(root).rebuild_all()
    epistemic_graph.clear_publication_memos()
    deferred_index.clear_graph_full_rebuild(root)
    yield root
    graph_sync.drain_active_rebuilds(timeout=30.0)
    epistemic_graph.clear_publication_memos()


def _count_whole_vault_passes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    passes: list[int] = []
    original = EpistemicGraphIndex._rebuild_all_pass

    def counted(self: EpistemicGraphIndex, *args: Any, **kwargs: Any) -> Any:
        passes.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(EpistemicGraphIndex, "_rebuild_all_pass", counted)
    return passes


def _current_generation(root: Path) -> int:
    return int(graph_sync.status(root).get("generation") or 0)


def test_a_whole_vault_publication_retires_the_marker_it_covers(vault: Path) -> None:
    """Any published whole-vault pass pays the debt that existed when it began."""
    deferred_index.mark_graph_full_rebuild(vault, generation=_current_generation(vault))
    assert deferred_index.graph_full_rebuild_pending(vault) is not None

    EpistemicGraphIndex(vault).rebuild_all()

    assert EpistemicGraphIndex(vault).available()
    assert deferred_index.graph_full_rebuild_pending(vault) is None, (
        "a whole-vault publication left the marker it covers standing, so the "
        "drain will pay for the same rebuild a second time"
    )


def test_a_coordinator_publication_leaves_the_drain_nothing_to_rebuild(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live pair: coordinator publishes, then the drain rebuilds the same graph.

    A write registers exact rebuild work with the coordinator and starts it
    straight away; the drain reaches the full marker a debounce later. Once the
    coordinator has published, the dispatcher must find nothing owed.
    """
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(vault / PAGE_B, _page("B", "B now cites [[coalesce-a]]."))],
        vault_root=vault,
        post_commit_fanout=False,
    )
    _seed_live_freshness(vault)
    checkpoint = graph_sync.read_checkpoint(vault)
    assert checkpoint is not None
    deferred_index.mark_graph_full_rebuild(vault, generation=checkpoint.generation)
    index = EpistemicGraphIndex(vault)

    registration = graph_sync.register_rebuild(
        vault,
        checkpoint,
        lambda required: epistemic_graph._rebuild_outcome(index, required),
        state_root=index._mutation_coordinator.state_root,
    )
    assert registration.start().builder_started
    assert graph_sync.drain_active_rebuilds(timeout=60.0)
    assert EpistemicGraphIndex(vault).available()

    passes = _count_whole_vault_passes(monkeypatch)
    result = epistemic_graph.converge_full_graph_marker(vault)

    assert passes == [], (
        f"the dispatcher ran {len(passes)} whole-vault pass(es) for a marker the "
        "coordinator's publication already covered"
    )
    assert result.outcome == "not_required"
    assert deferred_index.graph_full_rebuild_pending(vault) is None


def test_debt_raised_while_the_pass_runs_survives_its_publication(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retirement is compare-and-swap: newer debt is never erased by an older pass."""
    deferred_index.mark_graph_full_rebuild(vault, generation=_current_generation(vault))
    before = deferred_index.graph_full_rebuild_pending(vault)
    assert before is not None
    raised: list[int] = []
    original = EpistemicGraphIndex._rebuild_all_pass

    def pass_with_concurrent_debt(self: EpistemicGraphIndex, *args: Any, **kwargs: Any) -> Any:
        if not raised:
            # A direct refresh that could not name its scope appends
            # whole-vault debt while this pass is already running.
            raised.append(deferred_index.advance_graph_full_rebuild(vault))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(EpistemicGraphIndex, "_rebuild_all_pass", pass_with_concurrent_debt)

    EpistemicGraphIndex(vault).rebuild_all()

    assert raised and raised[0] > before
    assert deferred_index.graph_full_rebuild_pending(vault) == raised[0], (
        "a publication erased whole-vault debt raised after its pass began"
    )


def test_a_publication_with_no_marker_writes_no_marker(vault: Path) -> None:
    """Retirement is a no-op when nothing is owed; it never creates debt."""
    assert deferred_index.graph_full_rebuild_pending(vault) is None

    EpistemicGraphIndex(vault).rebuild_all()

    assert deferred_index.graph_full_rebuild_pending(vault) is None


def test_a_repeated_mark_during_the_pass_survives_its_publication(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Debt re-raised at the same value is still new debt.

    `mark_graph_full_rebuild` keeps the marker's value when a new request is
    not higher, so a compare-and-swap on the value alone would erase debt a
    full-scope batch raised while the pass was already running.
    """
    generation = _current_generation(vault)
    deferred_index.mark_graph_full_rebuild(vault, generation=generation)
    before = deferred_index.graph_full_rebuild_pending(vault)
    remarked: list[int | None] = []
    original = EpistemicGraphIndex._rebuild_all_pass

    def pass_with_repeated_debt(self: EpistemicGraphIndex, *args: Any, **kwargs: Any) -> Any:
        if not remarked:
            deferred_index.mark_graph_full_rebuild(vault, generation=generation)
            remarked.append(deferred_index.graph_full_rebuild_pending(vault))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(EpistemicGraphIndex, "_rebuild_all_pass", pass_with_repeated_debt)

    EpistemicGraphIndex(vault).rebuild_all()

    assert remarked == [before], "the repeat should leave the marker's value unchanged"
    assert deferred_index.graph_full_rebuild_pending(vault) == before, (
        "a publication erased debt re-raised at the same value while its pass ran"
    )


def test_the_dispatcher_keeps_a_same_value_repeat_raised_after_its_publication(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full-marker dispatcher retires by raise count too, not by value.

    Its own rebuild publishes and then a full-scope batch raises the marker
    again at the same value. The publication's retirement correctly declines,
    and the dispatcher's own clear must decline for the same reason: clearing
    by value erased that batch's debt and left its change out of the graph
    until an availability backstop noticed.
    """
    value = deferred_index.advance_graph_full_rebuild(vault, after_generation=_current_generation(vault))
    repeated: list[tuple[int, int] | None] = []
    real_retire = epistemic_graph._retire_covered_full_marker

    def retire_after_a_repeat(root: Path, observed: Any) -> None:
        if not repeated:
            deferred_index.mark_graph_full_rebuild(vault, generation=value)
            repeated.append(deferred_index.graph_full_rebuild_observation(vault))
        real_retire(root, observed)

    monkeypatch.setattr(epistemic_graph, "_retire_covered_full_marker", retire_after_a_repeat)

    result = epistemic_graph.converge_full_graph_marker(vault)

    assert result.outcome == "completed"
    assert repeated and repeated[0] is not None and repeated[0][0] == value
    assert deferred_index.graph_full_rebuild_observation(vault) == repeated[0], (
        "the dispatcher erased whole-vault debt re-raised at the same value after its "
        "publication"
    )


def test_marker_retirement_stays_outside_the_publication_hold(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The publication hold stays bounded to its ticket checks and the replacement.

    Retiring the marker is a write to the deferred-work store; it runs once the
    hold and the rebuild-owner claim are released, and the raise-count
    compare-and-swap is what keeps that safe.
    """
    from contextlib import contextmanager

    deferred_index.mark_graph_full_rebuild(vault, generation=_current_generation(vault))
    index = EpistemicGraphIndex(vault)
    coordinator = index._mutation_coordinator
    real_hold = coordinator.hold
    publishing: list[bool] = []

    @contextmanager
    def hold(*args: Any, **kwargs: Any) -> Any:
        with real_hold(*args, **kwargs):
            publishing.append(kwargs.get("operation") == "epistemic_graph_publish_rebuild")
            try:
                yield
            finally:
                publishing.pop()

    monkeypatch.setattr(coordinator, "hold", hold)
    real_retire = deferred_index.retire_observed_graph_full_rebuild
    retirements: list[bool] = []

    def retire(*args: Any, **kwargs: Any) -> bool:
        retirements.append(any(publishing))
        return real_retire(*args, **kwargs)

    monkeypatch.setattr(deferred_index, "retire_observed_graph_full_rebuild", retire)

    index.rebuild_all()

    assert retirements == [False], (
        f"marker retirement ran {retirements} (True = under the publication hold)"
    )
    assert deferred_index.graph_full_rebuild_pending(vault) is None


def test_the_dispatcher_never_waits_on_a_committing_batch_under_the_boundary(
    vault: Path,
) -> None:
    """A recovery write under the writers' boundary must not queue behind a batch.

    Measured at five writers (CG-2): the dispatcher held the canonical boundary,
    found the epoch recoverable and called `recover_checkpoint`, whose batch
    write waited on the in-process batch lock. The batch holding that lock was
    in its post-commit fan-out, waiting five seconds for the canonical boundary
    the dispatcher held; each writer behind it did the same, so writers were
    refused `MUTATION_BUSY` one after another for 26-42 s. The dispatcher now
    leaves recovery to the next tick while a batch is committing.
    """
    import threading
    import time

    from exomem import vault as vault_module

    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(vault / PAGE_B, _page("B", "B is revised."))],
        vault_root=vault,
        post_commit_fanout=False,
    )
    checkpoint = graph_sync.read_checkpoint(vault)
    assert checkpoint is not None
    graph_sync._write_floor(
        vault, graph_sync.GraphSyncGenerationFloor.create(int(checkpoint.generation) + 1)
    )
    assert graph_sync.classify_epoch(vault).kind == "recoverable"
    deferred_index.mark_graph_full_rebuild(vault, generation=int(checkpoint.generation))

    committing = threading.Event()
    release = threading.Event()

    def hold_the_batch_lock() -> None:
        with vault_module._BATCH_COMMIT_LOCK:
            committing.set()
            release.wait(30.0)

    holder = threading.Thread(target=hold_the_batch_lock)
    holder.start()
    assert committing.wait(5.0)
    outcome: list[Any] = []
    dispatcher = threading.Thread(
        target=lambda: outcome.append(epistemic_graph.converge_full_graph_marker(vault))
    )
    try:
        started = time.monotonic()
        dispatcher.start()
        dispatcher.join(timeout=5.0)
        waited = time.monotonic() - started
        assert not dispatcher.is_alive(), (
            "the dispatcher waited on a committing batch while holding the writers' boundary"
        )
        assert waited < 5.0
        assert outcome and outcome[0].outcome != "completed"
        assert deferred_index.graph_full_rebuild_pending(vault) is not None
    finally:
        release.set()
        holder.join(timeout=30.0)
        dispatcher.join(timeout=60.0)
