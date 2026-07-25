"""`logs/mutations.jsonl`: one record per mutation attempt at the
terminal/interrupted seam in `writer_lease.invoke()`, with boundary wait/hold
timing, content-classified targets, and best-effort (never-raises) writes.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import metrics, mutation_journal
from exomem.cli_ops import OpError
from exomem.writer_lease import LeaseConfig, LeaseManager


def _command(*, writes: bool, leaf):  # noqa: ANN001
    return SimpleNamespace(name="mutate" if writes else "read", read_only=not writes, leaf=leaf)


def _standalone_manager(tmp_path: Path) -> LeaseManager:
    return LeaseManager(LeaseConfig(state_dir=tmp_path))


@pytest.fixture(autouse=True)
def _journal_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(log_dir))
    yield log_dir


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset()
    yield
    metrics.reset()


def _read_journal(log_dir: Path) -> list[dict]:
    path = log_dir / "mutations.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_committed_mutation_is_journaled_with_timing(
    tmp_path: Path, _journal_log_dir: Path
) -> None:
    manager = _standalone_manager(tmp_path / "state")
    command = _command(writes=True, leaf=lambda: "ok")
    assert manager.invoke(command, (), {}, mutation_request_id="req-committed") == "ok"

    records = _read_journal(_journal_log_dir)
    assert len(records) == 1
    record = records[0]
    assert record["request_id"] == "req-committed"
    assert record["command"] == "mutate"
    assert record["outcome"] == "committed"
    assert record["error_code"] is None
    assert isinstance(record["duration_ms"], (int, float))
    assert isinstance(record["boundary_wait_ms"], (int, float))
    assert isinstance(record["boundary_hold_ms"], (int, float))
    assert record["lease_role"] == "standalone"
    assert record["target_count"] == 1
    assert record["targets"]


def test_failed_mutation_is_journaled_with_error_code(
    tmp_path: Path, _journal_log_dir: Path
) -> None:
    manager = _standalone_manager(tmp_path / "state")

    def boom():
        raise OpError("MUTATION_BUSY", "boundary busy")

    command = _command(writes=True, leaf=boom)
    with pytest.raises(OpError):
        manager.invoke(command, (), {}, mutation_request_id="req-failed")

    records = _read_journal(_journal_log_dir)
    assert len(records) == 1
    assert records[0]["outcome"] == "failed"
    assert records[0]["error_code"] == "MUTATION_BUSY"


def test_hosted_journal_records_target_count_only(
    tmp_path: Path, _journal_log_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_HOSTED_CELL", "1")
    manager = _standalone_manager(tmp_path / "state")
    command = _command(writes=True, leaf=lambda: "ok")
    manager.invoke(command, (), {}, mutation_request_id="req-hosted")

    records = _read_journal(_journal_log_dir)
    assert len(records) == 1
    assert records[0]["target_count"] == 1
    assert "targets" not in records[0]


def test_record_mutation_never_raises_on_write_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom_path():
        raise OSError("disk unavailable")

    monkeypatch.setattr(mutation_journal, "journal_path", boom_path)
    mutation_journal.record_mutation(
        request_id="r1",
        tool="mutate",
        command="mutate",
        receipt_id=None,
        outcome="committed",
        error_code=None,
        duration_ms=1.0,
    )
    snap = metrics.snapshot()
    counters = {(c["name"], tuple(sorted(c["labels"].items()))): c["value"] for c in snap["counters"]}
    assert counters[("exomem_log_write_errors_total", (("where", "mutation_journal"),))] == 1


def test_rotates_at_size_cap(tmp_path: Path, _journal_log_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_JSONL_MAX_MB", "0.001")
    mutation_journal._size_cache.clear()
    mutation_journal._append_counts.clear()
    for i in range(150):
        mutation_journal.record_mutation(
            request_id=f"r{i}",
            tool="mutate",
            command="mutate",
            receipt_id=None,
            outcome="committed",
            error_code=None,
            duration_ms=1.0,
            targets=["a" * 50],
        )
    rotated = _journal_log_dir / "mutations.jsonl.1"
    assert rotated.exists()
