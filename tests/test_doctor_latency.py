"""`exomem doctor` `latency`: the watch's figures read from the ledger on disk."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from exomem import call_ledger, doctor, latency_watch


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "logs"
    directory.mkdir()
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(directory))
    monkeypatch.delenv("EXOMEM_DISABLE_CALL_LEDGER", raising=False)
    call_ledger.reset_chain_cache()
    yield directory
    call_ledger.reset_chain_cache()


def _row(*, age_s: float, tool="ask_memory", client="openai-mcp/1.0.0", total_ms=200.0, deep=False, spans=None):
    ts = datetime.fromtimestamp(time.time() - age_s, tz=UTC).isoformat(timespec="milliseconds")
    deep_raw = call_ledger.canonical_json({"v": deep})
    return {
        "ts_utc": ts,
        "tool": tool,
        "client_name": client,
        "total_ms": total_ms,
        "duration_ms": total_ms - 5,
        "spans": spans or [],
        "args": {"query": {"len": 9, "sha256": "0" * 64}, "deep": {"len": len(deep_raw), "sha256": hashlib.sha256(deep_raw).hexdigest()}},
    }


def _write(path: Path, rows: list[dict], *, junk: bool = False) -> None:
    lines = [json.dumps(r) for r in rows]
    if junk:
        lines.insert(1, "{not json")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_a_slow_client_is_diagnosed_not_just_measured(ledger: Path) -> None:
    rows = [_row(age_s=60 * i, total_ms=150) for i in range(20)]
    rows += [
        _row(age_s=3000 + i, total_ms=4000, spans=[{"name": "embeddings.matrix_load", "ms": 3500, "count": 1}])
        for i in range(5)
    ]
    rows += [_row(age_s=100, tool="remember", total_ms=20000)]  # not a recall; ignored
    _write(ledger / "ledger.jsonl", rows, junk=True)
    check = doctor._check_latency()
    assert check.status == "warn"
    assert "ask_memory from openai-mcp/1.0.0: p90 4000 ms over the 1000 ms ceiling (25 calls)" in check.message
    assert "embeddings.matrix_load 17500 ms over 5 call(s)" in check.message
    [row] = check.details["rows"]
    assert row["samples"] == 25 and row["breach"] is True


def test_deep_recalls_are_classified_from_the_row_shape(ledger: Path) -> None:
    rows = [_row(age_s=10 * i, total_ms=4200, deep=True) for i in range(25)]
    _write(ledger / "ledger.jsonl", rows)
    check = doctor._check_latency()
    assert check.status == "pass"
    [row] = check.details["rows"]
    assert row["deep"] is True and row["ceiling_ms"] == 5000 and row["breach"] is False


def test_the_window_reaches_into_the_archive_and_stops_at_old_generations(ledger: Path) -> None:
    active = [_row(age_s=10 * i, total_ms=3000) for i in range(10)]
    _write(ledger / "ledger.jsonl", active)
    archive = ledger / "ledger-archive"
    archive.mkdir()
    recent = [dict(_row(age_s=3600 + 10 * i, total_ms=3000), sequence=500 + i) for i in range(15)]
    stale = [dict(_row(age_s=latency_watch.WINDOW_SECONDS + 3600 + i, total_ms=1), sequence=1 + i) for i in range(50)]
    _write(archive / "ledger-000001.jsonl", stale)
    _write(archive / "ledger-000002.jsonl", recent)
    check = doctor._check_latency()
    [row] = check.details["rows"]
    assert row["samples"] == 25  # 10 active + 15 recent archive, none of the 50 stale
    assert check.status == "warn"


def test_archive_generations_are_placed_by_sequence_not_mtime(ledger: Path) -> None:
    """The archive is content-addressed; a restore or a copy rewrites every
    mtime. Reading newest-by-mtime first and stopping at the first generation
    with nothing in the window would skip the generation that holds the breach."""
    import os

    _write(ledger / "ledger.jsonl", [_row(age_s=10, total_ms=100)])
    archive = ledger / "ledger-archive"
    archive.mkdir()
    stale = [dict(_row(age_s=latency_watch.WINDOW_SECONDS + 3600 + i, total_ms=1), sequence=100 + i) for i in range(5)]
    recent = [dict(_row(age_s=3600 + i, total_ms=3000), sequence=900 + i) for i in range(20)]
    _write(archive / "ledger-aaaa.jsonl", recent)
    _write(archive / "ledger-bbbb.jsonl", stale)
    now = time.time()
    os.utime(archive / "ledger-aaaa.jsonl", (now - 500, now - 500))  # newest rows, oldest mtime
    os.utime(archive / "ledger-bbbb.jsonl", (now - 1, now - 1))
    check = doctor._check_latency()
    [row] = check.details["rows"]
    assert row["samples"] == 21 and row["breach"] is True
    assert check.status == "warn"


def test_a_string_deep_is_classified_the_same_from_disk_as_live(ledger: Path) -> None:
    """A connector may send `deep` as a string; the live path counts it as deep,
    so the row read back must land in the same bucket, or the two surfaces
    disagree about which ceiling applies."""
    rows = [_row(age_s=10 * i, total_ms=4200, deep="true") for i in range(25)]
    _write(ledger / "ledger.jsonl", rows)
    assert latency_watch.deep_flag({"deep": "true"}) is True
    check = doctor._check_latency()
    [row] = check.details["rows"]
    assert row["deep"] is True and row["ceiling_ms"] == 5000 and row["breach"] is False


def test_too_few_calls_passes_with_a_note(ledger: Path) -> None:
    _write(ledger / "ledger.jsonl", [_row(age_s=i, total_ms=9000) for i in range(5)])
    check = doctor._check_latency()
    assert check.status == "pass"
    assert "fewer than 20 calls" in check.message


def test_an_absent_ledger_is_not_a_failure(ledger: Path) -> None:
    check = doctor._check_latency()
    assert check.status == "pass"
    assert "nothing to measure" in check.message.lower()


def test_the_check_is_registered(ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = tmp_path / "vault"
    (vault / "Knowledge Base").mkdir(parents=True)
    monkeypatch.setenv("EXOMEM_STATE_DIR", str(tmp_path / "state"))
    report = doctor.doctor(vault=str(vault), probe=False)
    assert "latency" in {check.id for check in report.checks}
