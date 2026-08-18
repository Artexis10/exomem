"""The skip decisions that keep a missing prerequisite from reading as a defect."""

from __future__ import annotations

import pytest

from benchmark_capabilities import declares_absent_sandbox, declares_absent_surface_timers


class BrokerContractError(ValueError):
    """Stands in for `epistemic.broker.BrokerContractError`.

    Matched by qualified name rather than imported, because the broker's own
    import reaches Linux-only paths -- a matcher that needs the module in order
    to decide whether the module is usable is no matcher at all.
    """


TIMER_REFUSALS = (
    "POSIX provider surface deadline primitives are unavailable",
    "POSIX provider surface timer state is unavailable",
    "provider surfaces require POSIX timer ownership on the main thread",
)

SANDBOX_REFUSALS = (
    "bwrap sandbox is unavailable before provider execution",
    "bwrap sandbox executable cannot be verified",
    "bwrap sandbox executable is not the trusted system binary",
    "bwrap sandbox executable is group/world writable",
    "sandbox system runtime or bound worker bytes are unavailable",
)


@pytest.mark.parametrize("message", TIMER_REFUSALS)
def test_every_timer_refusal_the_broker_can_raise_is_recognised(message: str) -> None:
    assert declares_absent_surface_timers(BrokerContractError(message))


@pytest.mark.parametrize("message", SANDBOX_REFUSALS)
def test_every_sandbox_refusal_the_broker_can_raise_is_recognised(message: str) -> None:
    assert declares_absent_sandbox(BrokerContractError(message))


def test_the_two_capabilities_are_never_confused_for_each_other() -> None:
    """macOS has the timers and not the sandbox, so one must not stand in for
    the other -- a sandbox refusal there has to survive the timer branch."""
    for message in TIMER_REFUSALS:
        assert not declares_absent_sandbox(BrokerContractError(message))
    for message in SANDBOX_REFUSALS:
        assert not declares_absent_surface_timers(BrokerContractError(message))


@pytest.mark.parametrize("refusal", TIMER_REFUSALS[:1] + SANDBOX_REFUSALS[:1])
def test_a_raises_match_mismatch_still_reveals_the_refusal_underneath(refusal: str) -> None:
    """`pytest.raises(..., match=...)` re-raises as an `AssertionError`.

    The refusal survives only as `__context__`, and 8 of the 19 broker failures
    arrived exactly this way -- a test that expected its own contract error and
    met an absent capability instead.
    """
    try:
        with pytest.raises(BrokerContractError, match="something else entirely"):
            raise BrokerContractError(refusal)
    except AssertionError as mismatch:
        assert declares_absent_surface_timers(mismatch) or declares_absent_sandbox(mismatch)
    else:  # pragma: no cover - the mismatch is the point of the test
        pytest.fail("expected the match to fail")


def test_an_unrelated_broker_error_is_never_swallowed() -> None:
    """The gate must not turn a real contract violation into a skip."""
    for message in (
        "receipt append is outside an active sandbox session",
        "sandbox session is unknown",
        "broker result exceeds driver IPC bounds",
        "sandbox driver result attestation is required",
    ):
        error = BrokerContractError(message)
        assert not declares_absent_surface_timers(error)
        assert not declares_absent_sandbox(error)


def test_a_matching_message_on_an_unrelated_exception_type_is_not_a_refusal() -> None:
    """Only the broker declares these; a coincidental string is not a capability."""
    assert not declares_absent_sandbox(ValueError(SANDBOX_REFUSALS[0]))
    assert not declares_absent_surface_timers(ValueError(TIMER_REFUSALS[0]))


def test_neither_matcher_loops_on_a_self_referential_cause() -> None:
    error = BrokerContractError("unrelated")
    error.__context__ = error

    assert declares_absent_sandbox(error) is False
    assert declares_absent_surface_timers(error) is False


def test_no_error_at_all_is_not_a_refusal() -> None:
    assert declares_absent_sandbox(None) is False
    assert declares_absent_surface_timers(None) is False
