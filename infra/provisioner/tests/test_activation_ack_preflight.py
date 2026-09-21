from __future__ import annotations

import pytest

from exomem_provisioner.activation_ack_preflight import (
    CLEAN,
    LEGACY_UNCERTAIN,
    PROTOCOL_QUALIFIED,
    STRANDED,
    classify_activation_ack_target,
)

TUPLE = ("store-a", 7, "d" * 64)
AHEAD = ("store-a", 8, "e" * 64)
NOW = 1_700_000_000
HOUR = 3_600


def _classify(**overrides):
    arguments = {
        "custody_activation": TUPLE,
        "store_activation": TUPLE,
        "capability_bound": True,
        "release_compatible": True,
        "replica_state": "SERVING",
        "attestation_expires_at": NOW + HOUR,
        "now": NOW,
        "required_seconds": 300,
        "legacy_uncertain_mark": False,
    }
    arguments.update(overrides)
    return classify_activation_ack_target(**arguments)


def test_an_acknowledged_cell_inside_its_window_is_clean_and_may_begin() -> None:
    result = _classify()

    assert result.status == CLEAN
    assert result.may_begin is True
    assert result.acknowledgement_outstanding is False
    assert result.remaining_seconds == HOUR


def test_a_capable_cell_with_an_outstanding_acknowledgement_is_recoverable() -> None:
    result = _classify(store_activation=AHEAD)

    assert result.status == PROTOCOL_QUALIFIED
    assert result.may_begin is True
    assert result.acknowledgement_outstanding is True


def test_an_outstanding_acknowledgement_without_the_capability_stays_unknown() -> None:
    """Restoring parity would hide the question rather than answer it."""

    result = _classify(store_activation=AHEAD, capability_bound=False)

    assert result.status == LEGACY_UNCERTAIN
    assert result.may_begin is False
    assert result.acknowledgement_outstanding is True
    assert "no protocol to recover it" in result.reason
    # The verdict is worthless unless it survives its own inputs, so the caller
    # is told to write it down before acting on anything else here.
    assert result.mark_legacy_uncertain is True


def test_a_recorded_legacy_uncertain_verdict_survives_everything_that_would_hide_it() -> None:
    """The whole point of the mark: the live inputs stop being able to clear it.

    Both routes back to looking healthy are live. Deploying the capability
    makes `capability_bound` true, and one later write brings the tuples back
    into parity. Neither produces the receipt the cell never wrote, so neither
    may reclassify it -- and the comparison is direction-blind precisely
    because parity is not evidence about what happened.
    """

    for arguments in (
        {"store_activation": AHEAD, "capability_bound": False},
        {"store_activation": AHEAD, "capability_bound": True},
        {"store_activation": TUPLE},
    ):
        marked = _classify(**arguments, legacy_uncertain_mark=True)
        assert marked.status == LEGACY_UNCERTAIN
        assert marked.may_begin is False
        assert "has not been adjudicated" in marked.reason
        # Already recorded, so nothing asks for it to be recorded again.
        assert marked.mark_legacy_uncertain is False

    # Without the mark the same clean cell is clean; the mark is doing the work.
    assert _classify(store_activation=TUPLE).status == CLEAN


def test_a_recorded_mark_never_hides_a_stranded_cell() -> None:
    """Stranded is the harder fact and the one an operator pages on."""

    result = _classify(
        store_activation=AHEAD,
        capability_bound=False,
        legacy_uncertain_mark=True,
        attestation_expires_at=NOW - 1,
    )

    assert result.status == STRANDED
    assert result.may_begin is False


def test_nothing_but_a_legacy_uncertain_verdict_asks_for_a_mark() -> None:
    assert _classify().mark_legacy_uncertain is False
    assert _classify(store_activation=AHEAD).mark_legacy_uncertain is False
    assert _classify(release_compatible=False).mark_legacy_uncertain is False
    assert _classify(required_seconds=HOUR + 1).mark_legacy_uncertain is False
    assert (
        _classify(
            store_activation=AHEAD, replica_state="SERVING", attestation_expires_at=NOW - 1
        ).mark_legacy_uncertain
        is False
    )


def test_a_lapsed_window_on_a_cell_that_has_served_is_stranded() -> None:
    for state in ("SERVING", "DRAINING"):
        result = _classify(
            store_activation=AHEAD, replica_state=state, attestation_expires_at=NOW - 1
        )
        assert result.status == STRANDED
        assert result.may_begin is False
        assert result.remaining_seconds == -1

    # A cell with no serving attestation at all can still be minted, so a
    # lapsed window is not terminal for it. `None` is the only way to say that:
    # an unrecognised state is bad input, not a quiet "never served".
    fresh = _classify(
        store_activation=AHEAD, replica_state=None, attestation_expires_at=NOW - 1
    )
    assert fresh.status == PROTOCOL_QUALIFIED
    assert fresh.may_begin is False
    assert "cannot complete inside the remaining attestation window" in fresh.reason


def test_an_unrecognised_replica_state_is_refused_rather_than_read_as_never_served() -> None:
    """Failing open here would discard STRANDED on the cells that have it.

    A caller paging on `status == STRANDED` would instead see a cell that has
    served and whose window has lapsed reported as clean or recoverable.
    """

    for state in ("Serving", "serving", "READY", "", "PENDING"):
        with pytest.raises(ValueError, match="known serving-membership state"):
            _classify(replica_state=state)

    # The two the attestation can actually carry, and the honest absence.
    for state in ("SERVING", "DRAINING", None):
        assert _classify(replica_state=state).status == CLEAN


def test_work_that_cannot_finish_inside_the_window_never_starts() -> None:
    """Starting it would turn a recoverable stall into a stranded cell."""

    assert _classify(required_seconds=HOUR).may_begin is True
    assert _classify(required_seconds=HOUR + 1).may_begin is False

    # A zero budget is a green light that promises nothing finishes, so it is
    # refused rather than treated as "no work to do".
    for budget in (0, -1):
        with pytest.raises(ValueError, match="must be positive"):
            _classify(required_seconds=budget)


def test_the_capability_must_be_bound_to_compatible_releases() -> None:
    result = _classify(store_activation=AHEAD, release_compatible=False)

    assert result.status == PROTOCOL_QUALIFIED
    assert result.may_begin is False
    assert "compatible runtime, provisioner and chart releases" in result.reason

    # An incompatible release on a cell with nothing outstanding is still a
    # refusal: the capability it claims is not the one that is deployed.
    assert _classify(release_compatible=False).may_begin is False


def test_the_tuple_comparison_is_direction_blind() -> None:
    """A tuple that differs is not evidence that either side is ahead."""

    forward = _classify(custody_activation=TUPLE, store_activation=AHEAD)
    backward = _classify(custody_activation=AHEAD, store_activation=TUPLE)

    assert forward.status == backward.status == PROTOCOL_QUALIFIED
    assert forward.acknowledgement_outstanding is backward.acknowledgement_outstanding is True

    # Every component participates, and a digest difference alone is enough.
    for store in (
        ("store-b", 7, "d" * 64),
        ("store-a", 9, "d" * 64),
        ("store-a", 7, "f" * 64),
    ):
        assert _classify(store_activation=store).acknowledgement_outstanding is True


def test_an_unprovisioned_activation_tuple_matches_only_another_absent_one() -> None:
    absent = (None, None, None)

    assert _classify(custody_activation=absent, store_activation=absent).status == CLEAN
    assert _classify(custody_activation=absent, store_activation=TUPLE).status == PROTOCOL_QUALIFIED
    assert _classify(custody_activation=TUPLE, store_activation=absent).status == PROTOCOL_QUALIFIED


def test_a_long_drained_cell_is_stranded_here_and_repairable_on_the_migration_path() -> None:
    """Pin why this classifier is not wired into the migration coordinator.

    `refresh_drained_source_bundle` exists to reissue the window of a cell that
    drained more than one window ago -- safe exactly because the cell stopped
    serving, and without it "a migration starting more than one window after
    the drain strands its cell". `DRAINING` is in `_SERVED_STATES`, so this
    classifier calls that same cell STRANDED and refuses it.

    Both are right for their own caller. The classifier answers "is this cell
    recoverable as it stands", and a lapsed window on a cell that has served is
    not re-mintable. The coordinator answers "can this migration proceed", and
    it holds the one action that changes the answer. Wiring the classifier onto
    that path would replace a repair with a refusal on precisely the cells the
    repair is for. See design Decision 14.
    """
    drained = _classify(replica_state="DRAINING", attestation_expires_at=NOW - 1)

    assert drained.status == STRANDED
    assert drained.may_begin is False
    assert drained.remaining_seconds == -1


def test_a_stranded_cell_still_asks_for_the_mark_so_a_restore_cannot_launder_it() -> None:
    """The strand is not permanent, and the uncertainty behind it is.

    A cell can be both past its window and legacy-uncertain. The strand is the
    louder verdict and wins the status, but this repo has restore paths that
    can hand such a cell a fresh window. Reclassified afterwards, with the
    capability deployed by then, it would read `protocol-qualified` and
    `may_begin=True` -- a genuinely unknown mutation outcome reporting itself
    as recoverable, which is the one thing Decision 12 exists to prevent.

    So the mark is decided from the inputs, not from which verdict wins.
    """

    stranded = _classify(
        store_activation=AHEAD, capability_bound=False, attestation_expires_at=NOW - 1
    )

    assert stranded.status == STRANDED
    assert stranded.may_begin is False
    assert stranded.mark_legacy_uncertain is True

    # Already recorded: nothing to ask for a second time, on either verdict.
    assert (
        _classify(
            store_activation=AHEAD,
            capability_bound=False,
            attestation_expires_at=NOW - 1,
            legacy_uncertain_mark=True,
        ).mark_legacy_uncertain
        is False
    )
    assert (
        _classify(
            store_activation=AHEAD, capability_bound=False, legacy_uncertain_mark=True
        ).mark_legacy_uncertain
        is False
    )


def test_forgetting_the_recorded_mark_is_a_type_error_not_a_reclassification() -> None:
    """The default that was there would have reclassified from live inputs."""

    with pytest.raises(TypeError):
        classify_activation_ack_target(
            custody_activation=TUPLE,
            store_activation=AHEAD,
            capability_bound=True,
            release_compatible=True,
            replica_state="SERVING",
            attestation_expires_at=NOW + HOUR,
            now=NOW,
            required_seconds=300,
        )
