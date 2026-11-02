"""What the write journal reports about itself (design D10): log rows, metrics, and the doctor check.

Real journal, real applier and a real vault copy. Each case names the failure only it catches.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from test_write_intent_accept import (
    _draft_kwargs,
    _journal,
    _log_rows,
    _manager,
    _remember,
    held_boundary_in_thread,
)
from test_write_intent_applier import _no_applier, _wait_for
from test_write_intent_decisions import _accept_edit, _edit_kwargs, _new_page

from exomem import doctor, metrics, writer_lease


@pytest.fixture(autouse=True)
def _journal_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_WRITE_INTENTS", "on")
    metrics.reset()
    yield
    writer_lease.reset_managers_for_tests()


def _counter(name: str, **labels: str) -> int:
    return sum(
        row["value"]
        for row in metrics.snapshot()["counters"]
        if row["name"] == name and all(row["labels"].get(k) == v for k, v in labels.items())
    )


def test_an_accepted_write_leaves_linked_rows_and_counts_in_the_metrics(vault: Path) -> None:
    """Without the link an operator cannot follow one write from accept to commit; without the counts the 7-day acceptance cannot be read."""
    manager = _manager()
    logged = len(_log_rows())
    with held_boundary_in_thread(vault, manager):
        accepted = _remember(manager, vault, _draft_kwargs(vault, 95), idempotency_key="observed")
        assert accepted["state"] == "accepted"
    _wait_for(_journal(manager, vault), accepted["intent_id"], ("committed",))
    rows = [row for row in _log_rows(logged) if row.get("intent_id") == accepted["intent_id"]]
    assert [row["outcome"] for row in rows] == ["accepted", "applied"]
    assert rows[0]["cause"], "the accepted row says why the write was journaled"
    assert rows[1]["duration_ms"] > 0 and rows[1]["request_id"] == rows[0]["request_id"]
    assert _counter("exomem_write_intents_total", outcome="accepted") == 1
    assert _counter("exomem_write_intents_total", outcome="applied") == 1
    assert _counter("exomem_write_accept_total", result="accepted") == 1
    depth = [
        g for g in metrics.snapshot()["gauges"] if g["name"] == "exomem_write_intent_queue_depth"
    ]
    assert depth and depth[0]["value"] == 0


def test_a_busy_refusal_row_names_its_cause(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The acceptance counts busy rows by cause; a row without one cannot be told from the journal's own refusals."""
    monkeypatch.setenv("EXOMEM_WRITE_INTENTS", "off")
    monkeypatch.setenv("EXOMEM_CAPTURE_WAIT_SECONDS", "0.3")
    manager = _manager()
    logged = len(_log_rows())
    with held_boundary_in_thread(vault, manager), pytest.raises(Exception):  # noqa: B017, PT011 - the refusal is the subject
        _remember(
            manager, vault, _draft_kwargs(vault, 96), implicit_idempotency_scope="principal:t"
        )
    busy = [row for row in _log_rows(logged) if row.get("error_code") == "MUTATION_BUSY"]
    assert busy and busy[0]["cause"] == "capture_wait_exhausted"


def test_the_doctor_fails_on_a_stale_accepted_write_with_no_applier(
    vault: Path, monkeypatch
) -> None:
    """A write that is saved and that nothing will apply looks healthy everywhere else; only this check can say so."""
    manager = _manager()
    path = _new_page(manager, vault, 97)
    assert doctor._check_write_journal(vault).status == "pass"
    _journal(manager, vault).clock = lambda: time.time() - 600  # accepted ten minutes ago
    with _no_applier(monkeypatch):
        _accept_edit(manager, vault, _edit_kwargs(path, "stale"), "stale-accepted")
    check = doctor._check_write_journal(vault)
    assert check.status == "fail" and check.details["applier_alive"] is False
    assert check.details["oldest_accepted_age_seconds"] > 300
