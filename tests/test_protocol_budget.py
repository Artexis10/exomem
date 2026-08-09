from __future__ import annotations

from pathlib import Path

import pytest


def test_budget_refusal_stop_unknown_model_and_immutable_caps(tmp_path: Path) -> None:
    from protocol.budget import BudgetExceeded, BudgetLedger, CapImmutableError, UnpricedModelError

    ledger = BudgetLedger(tmp_path, caps={"usd": 5})
    ledger.reserve(ts="2026-01-01T00:00:00Z", seq=1, actor="test", op="ingest", units=4)
    with pytest.raises(BudgetExceeded):
        ledger.reserve(ts="2026-01-01T00:00:01Z", seq=2, actor="test", op="ingest", units=2)
    assert (tmp_path / "STOP").is_file()
    with pytest.raises(BudgetExceeded):
        BudgetLedger(tmp_path).reserve(ts="2026-01-01T00:00:02Z", seq=3, actor="test", op="ingest", units=1)
    with pytest.raises(CapImmutableError):
        BudgetLedger(tmp_path, caps={"usd": 7})
    other = BudgetLedger(tmp_path / "models", caps={"usd": 5})
    with pytest.raises(UnpricedModelError):
        other.reserve(ts="2026-01-01T00:00:00Z", seq=1, actor="test", op="call", units=1, model_id="unknown")
