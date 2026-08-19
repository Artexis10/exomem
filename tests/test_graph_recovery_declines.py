"""A graph that will not converge has to be able to say why.

`recover_suspended_graph` declines for five distinct reasons, and every one of
them used to be a bare `return False`. The behaviour was right -- each is a
real reason not to pay a whole-vault rebuild -- but the diagnosis was
impossible: a scheduler polling a stuck graph called in, was turned away, and
logged nothing, so a failure that lasted minutes produced no evidence of its
own cause.

That is not hypothetical. A product E2E run answered readiness polls for 110
seconds against an unavailable graph and emitted not one graph line in that
window; the artifact shows the rebuild exhausting at +7.3s and then silence.
Reconstructing what happened afterwards meant guessing between five candidate
guards. `epistemic_graph.py` already carries the scar from an earlier round of
that -- "two published analyses of this incident named the wrong mechanism
before one measured it" -- which is exactly the cost this removes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import epistemic_graph, freshness


@pytest.fixture(autouse=True)
def _forget_previous_declines():
    epistemic_graph.clear_recovery_declines()
    epistemic_graph.clear_publication_memos()
    yield
    epistemic_graph.clear_recovery_declines()
    epistemic_graph.clear_publication_memos()


def _built_graph(vault_root: Path) -> epistemic_graph.EpistemicGraphIndex:
    index = epistemic_graph.EpistemicGraphIndex(vault_root)
    index.rebuild_all()
    return index


def test_a_vault_with_no_sidecar_names_that(tmp_path: Path) -> None:
    """Nothing to repair yet is a different answer from nothing to say."""
    assert (
        epistemic_graph.recovery_decline_reason(tmp_path)
        == epistemic_graph.RECOVERY_DECLINE_NO_SIDECAR
    )


def test_a_graph_with_no_barrier_names_that(tmp_path: Path) -> None:
    """The ordinary case: a healthy graph declines because there is no signal."""
    _built_graph(tmp_path)

    assert (
        epistemic_graph.recovery_decline_reason(tmp_path)
        == epistemic_graph.RECOVERY_DECLINE_NO_BARRIER
    )


def test_a_standing_barrier_is_not_a_decline(tmp_path: Path) -> None:
    """The state repair exists for must return None, or the token means nothing."""
    index = _built_graph(tmp_path)
    index.withdraw_availability()

    assert index.reads_suspended() is True
    assert epistemic_graph.recovery_decline_reason(tmp_path) is None


def test_a_refused_publication_names_that_rather_than_the_barrier(
    tmp_path: Path,
) -> None:
    """Contract R2's deferral, which is the one that looks most like a hang.

    The barrier is standing and repair still declines, so this is precisely the
    combination that reads as "nothing is happening" from outside. It is also
    bounded -- the memo expires -- and telling those two apart from a log is
    the whole point of naming it.
    """
    index = _built_graph(tmp_path)
    index.withdraw_availability()
    epistemic_graph.note_publication_refusal(tmp_path)

    assert (
        epistemic_graph.recovery_decline_reason(tmp_path)
        == epistemic_graph.RECOVERY_DECLINE_PUBLICATION_REFUSED
    )


def test_a_disabled_graph_names_that(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator switch and a broken graph must not look the same."""
    monkeypatch.setattr(epistemic_graph, "graph_enabled", lambda: False)

    assert (
        epistemic_graph.recovery_decline_reason(tmp_path)
        == epistemic_graph.RECOVERY_DECLINE_GRAPH_DISABLED
    )


def test_a_pending_external_change_names_that_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checked ahead of everything else, so it must report ahead of everything else.

    Rebuilding against a vault that is still changing underneath is the pass
    most likely to be wasted, so this guard is deliberately first -- and a
    reader who sees it knows the graph is waiting on the vault rather than on
    the graph.
    """
    _built_graph(tmp_path)
    monkeypatch.setattr(freshness, "external_pending", lambda _root: True)

    assert (
        epistemic_graph.recovery_decline_reason(tmp_path)
        == epistemic_graph.RECOVERY_DECLINE_EXTERNAL_PENDING
    )


def test_a_repeated_decline_is_logged_once_and_a_changed_one_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One line per change, not one per poll.

    The scheduler retries on a backoff that reaches an attempt every two
    minutes and runs for the life of the process, so logging every decline
    would bury a long-lived service's log in one repeated line. The transition
    is the part that answers the question anyone actually asks of a stuck
    graph, which is what changed.
    """
    _built_graph(tmp_path)

    with caplog.at_level("INFO", logger="exomem.epistemic_graph"):
        assert epistemic_graph.recover_suspended_graph(tmp_path) is False
        assert epistemic_graph.recover_suspended_graph(tmp_path) is False

        declines = [r for r in caplog.records if "repair declined" in r.getMessage()]
        assert len(declines) == 1
        assert epistemic_graph.RECOVERY_DECLINE_NO_BARRIER in declines[0].getMessage()

        monkeypatch.setattr(freshness, "external_pending", lambda _root: True)
        assert epistemic_graph.recover_suspended_graph(tmp_path) is False

        declines = [r for r in caplog.records if "repair declined" in r.getMessage()]
        assert len(declines) == 2
        assert (
            epistemic_graph.RECOVERY_DECLINE_EXTERNAL_PENDING
            in declines[1].getMessage()
        )
