"""Delivering a due-state block records the emission beside the projection, not inside it.

Measured 2026-09-16 on the personal vault: every response that carried the
block reloaded (241 ms) and rewrote (315 to 405 ms) the 20 MB projection to
bump one counter, and that rewrite made the next recall rebuild the served
block from scratch because its memo keys on the projection file. The ledger
now lives in a small sidecar; the projection carries a copy for its readers
and changes only when governed state changes.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from exomem import due_state

PAGE = "Knowledge Base/Notes/Insights/one.md"


@pytest.fixture(autouse=True)
def _fresh() -> None:
    due_state.reset_serve_cache()
    due_state.reset_emission_state()
    yield
    due_state.reset_serve_cache()
    due_state.reset_emission_state()


def _persist(vault_root: Path, emission: dict | None = None) -> None:
    (vault_root / PAGE).parent.mkdir(parents=True, exist_ok=True)
    (vault_root / PAGE).write_text("# one\n", encoding="utf-8")
    payload = {
        "version": due_state.SCHEMA_VERSION,
        "categories": {"predictions": {PAGE: {"open": [{"path": PAGE}]}}},
    }
    if emission is not None:
        payload["emission"] = emission
    due_state.save(vault_root, payload)


def _token(path: Path) -> tuple[int, int, int]:
    info = path.stat()
    return (info.st_ino, info.st_mtime_ns, info.st_size)


def test_marking_a_block_emitted_leaves_the_projection_file_untouched(tmp_path: Path) -> None:
    _persist(tmp_path)
    before = _token(due_state.state_path(tmp_path))
    due_state.mark_emitted({"total": 1, "top": []}, vault_root=tmp_path)
    assert _token(due_state.state_path(tmp_path)) == before
    ledger = due_state.emission_ledger(tmp_path)
    assert ledger["emissions"] == 1
    assert ledger["due_total"] == 1
    assert due_state.emission_path(tmp_path).is_file()


def test_the_served_memo_survives_a_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _persist(tmp_path)
    real = due_state._served_entries_uncached
    calls: list[int] = []

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(due_state, "_served_entries_uncached", counted)
    now = dt.datetime(2026, 9, 16, 9, tzinfo=dt.UTC)
    due_state.served_entries(tmp_path, now=now)
    due_state.mark_emitted({"total": 1, "top": []}, vault_root=tmp_path)
    due_state.served_entries(tmp_path, now=now)
    assert len(calls) == 1


def test_a_projection_written_before_the_sidecar_seeds_it(tmp_path: Path) -> None:
    _persist(tmp_path, {"writes": 7, "emissions": 2, "last_digest": "d", "due_total": 3})
    assert not due_state.emission_path(tmp_path).exists()
    assert due_state.emission_ledger(tmp_path)["writes"] == 7
    due_state.mark_emitted({"total": 4, "top": []}, vault_root=tmp_path)
    ledger = due_state.emission_ledger(tmp_path)
    assert (ledger["writes"], ledger["emissions"], ledger["due_total"]) == (7, 3, 4)
    assert due_state.emission_path(tmp_path).is_file()


def test_a_governed_write_carries_the_ledger_into_the_projection_copy(tmp_path: Path) -> None:
    _persist(tmp_path)
    due_state.mark_emitted({"total": 1, "top": []}, vault_root=tmp_path)
    # The projection's copy is stale until a governed write rewrites it; the
    # sidecar is the truth in between, and the next write carries it over.
    assert due_state.load(tmp_path).get("emission", {}).get("emissions", 0) == 0
    section = due_state._bump_ledger(tmp_path, None, writes=1)
    assert (section["writes"], section["emissions"]) == (1, 1)
    assert due_state.emission_ledger(tmp_path) == section
