"""The parts of a recall that live outside the retrieval stages name their cost.

Measured 2026-09-16 on the personal service after 0.85.5: a keyword recall
took 1.8 s of which the `recall.*` stages explained 0.75 s. The rest ran in
places no span covered -- the served memo's re-checks, the emission decision,
the MCP-layer post-filter, and the lexical publication barrier the first read
after a promotion waited 30 s on. Each of those is a named span now.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import call_spans, commands, due_state, lexstore

PAGE = "Knowledge Base/Notes/Insights/one.md"


@pytest.fixture()
def token():
    handle = call_spans.MCP_CALL_TOKEN.set("spans-test")
    due_state.reset_serve_cache()
    try:
        yield "spans-test"
    finally:
        call_spans.pop_call_spans("spans-test")
        call_spans.MCP_CALL_TOKEN.reset(handle)
        due_state.reset_serve_cache()


def _names(token: str) -> dict[str, dict]:
    return {row["name"]: row for row in call_spans.pop_call_spans(token)}


def _persist(vault_root: Path) -> None:
    (vault_root / PAGE).parent.mkdir(parents=True, exist_ok=True)
    (vault_root / PAGE).write_text("# one\n", encoding="utf-8")
    due_state.save(
        vault_root,
        {
            "version": due_state.SCHEMA_VERSION,
            "categories": {"predictions": {PAGE: {"open": [{"path": PAGE}]}}},
        },
    )


def test_a_served_miss_names_the_build_and_a_hit_names_its_rechecks(
    tmp_path: Path, token: str
) -> None:
    _persist(tmp_path)
    who = SimpleNamespace(audience_id="a", authorization_session_id="s", resolved=True)
    now = dt.datetime(2026, 9, 18, 9, tzinfo=dt.UTC)
    due_state.served_entries(tmp_path, now=now, principal=who)
    first = _names(token)
    assert "recall.due_state.build" in first
    assert "recall.due_state.verdicts" not in first
    due_state.served_entries(tmp_path, now=now, principal=who)
    second = _names(token)
    assert "recall.due_state.build" not in second
    assert "recall.due_state.verdicts" in second
    assert "recall.due_state.exists" in second


def test_the_emission_decision_has_its_own_span(
    tmp_path: Path, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(due_state, "served", lambda *_a, **_k: {"total": 1, "top": []})
    monkeypatch.setattr(due_state, "should_emit", lambda *_a, **_k: True)
    out = commands._with_due_state(tmp_path, {"hits": []}, purpose=None)
    assert out["due_state"] == {"total": 1, "top": []}
    names = _names(token)
    assert "recall.due_state" in names
    assert "recall.due_state.emit" in names


def test_waiting_on_the_publication_barrier_is_attributed_to_the_call(token: str) -> None:
    with lexstore._timed_barrier(contextlib.nullcontext(), 0.05):
        pass
    row = _names(token)["lexical.publication_wait"]
    assert row["fields"] == {"timeout_ms": 50, "acquired": 1}

    @contextlib.contextmanager
    def refused():
        raise TimeoutError("held")
        yield

    with pytest.raises(TimeoutError):
        with lexstore._timed_barrier(refused(), 30.0):
            pass
    row = _names(token)["lexical.publication_wait"]
    assert row["fields"] == {"timeout_ms": 30000, "acquired": 0}


def test_off_the_mcp_path_the_barrier_span_is_a_no_op() -> None:
    assert call_spans.MCP_CALL_TOKEN.get() is None
    with lexstore._timed_barrier(contextlib.nullcontext(), 0.05):
        pass
