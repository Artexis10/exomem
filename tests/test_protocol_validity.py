from __future__ import annotations

import pytest


def test_validity_terminal_vocabulary_and_reason_requirements() -> None:
    from protocol.validity import ABORTED_BUDGET, BLOCKED, INVALID, READINESS_UNVERIFIABLE, VALID, is_terminal

    assert all(is_terminal(status) for status in (VALID, INVALID("leakage"), READINESS_UNVERIFIABLE, ABORTED_BUDGET, BLOCKED("provider unavailable")))
    assert not is_terminal("started")
    with pytest.raises(ValueError):
        INVALID("")
    with pytest.raises(ValueError):
        BLOCKED("")
