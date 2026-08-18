"""`exomem trace <request_id>` joins the server log, queries/writes/reads.jsonl,
mutations.jsonl, and the call ledger for one request id, time-ordered.
`exomem logs tail|grep|verify` reads the per-process JSONL files directly."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import call_ledger, obs_cli


@pytest.fixture(autouse=True)
def _log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(log_dir))
    return log_dir


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def test_trace_joins_server_log_and_query_log_for_one_request_id(_log_dir: Path) -> None:
    _write_jsonl(
        _log_dir / "exomem.log",
        [
            {
                "ts": "2026-07-24T10:00:00.000",
                "event": "tool_start",
                "fields": {"request_id": "req-1", "tool": "remember"},
            },
            {
                "ts": "2026-07-24T10:00:01.000",
                "event": "tool_success",
                "fields": {"request_id": "req-1", "tool": "remember"},
            },
            {
                "ts": "2026-07-24T10:00:02.000",
                "event": "tool_start",
                "fields": {"request_id": "req-other", "tool": "ask_memory"},
            },
        ],
    )
    _write_jsonl(
        _log_dir / "writes.jsonl",
        [{"ts": "2026-07-24 10:00:00", "ts_utc": "2026-07-24T10:00:00.500Z", "tool": "note", "request_id": "req-1"}],
    )
    _write_jsonl(
        _log_dir / "mutations.jsonl",
        [{"ts_utc": "2026-07-24T10:00:01.500Z", "request_id": "req-1", "outcome": "committed"}],
    )

    records = obs_cli.trace("req-1")

    sources = [r["_source"] for r in records]
    assert sources == ["server", "writes", "server", "mutations"]
    assert all(r.get("request_id") == "req-1" or r.get("fields", {}).get("request_id") == "req-1" for r in records)


def test_trace_returns_empty_list_for_unknown_request_id(_log_dir: Path) -> None:
    _write_jsonl(
        _log_dir / "exomem.log",
        [{"ts": "2026-07-24T10:00:00.000", "event": "tool_start", "fields": {"request_id": "req-1"}}],
    )
    assert obs_cli.trace("nonexistent-request-id") == []


def test_trace_skips_malformed_lines_without_raising(_log_dir: Path) -> None:
    (_log_dir / "exomem.log").write_text(
        "not json at all\n"
        + json.dumps({"event": "tool_start", "fields": {"request_id": "req-1"}})
        + "\n",
        encoding="utf-8",
    )
    records = obs_cli.trace("req-1")
    assert len(records) == 1


def test_trace_reads_rotated_generation_too(_log_dir: Path) -> None:
    _write_jsonl(
        _log_dir / "writes.jsonl.1",
        [{"ts_utc": "2026-07-24T09:00:00.000Z", "request_id": "req-1", "tool": "note"}],
    )
    _write_jsonl(
        _log_dir / "writes.jsonl",
        [{"ts_utc": "2026-07-24T10:00:00.000Z", "request_id": "req-1", "tool": "note"}],
    )
    records = obs_cli.trace("req-1")
    assert len(records) == 2
    assert records[0]["ts_utc"] < records[1]["ts_utc"]


def test_resolve_log_file_rejects_unknown_alias() -> None:
    with pytest.raises(ValueError, match="unknown log file"):
        obs_cli.resolve_log_file("not-a-real-file")


def test_tail_lines_returns_last_n_lines(_log_dir: Path) -> None:
    path = _log_dir / "exomem.log"
    path.write_text("\n".join(f"line-{i}" for i in range(50)) + "\n", encoding="utf-8")
    assert obs_cli.tail_lines(path, 3) == ["line-47", "line-48", "line-49"]


def test_tail_lines_missing_file_returns_empty(_log_dir: Path) -> None:
    assert obs_cli.tail_lines(_log_dir / "does-not-exist.log", 10) == []


def test_grep_lines_matches_pattern_across_rotated_and_live(_log_dir: Path) -> None:
    (_log_dir / "exomem.log.1").write_text("event=tool_failure code=MUTATION_BUSY\n", encoding="utf-8")
    (_log_dir / "exomem.log").write_text("event=tool_success tool=remember\n", encoding="utf-8")
    matches = obs_cli.grep_lines(_log_dir / "exomem.log", r"tool_failure")
    assert matches == ["event=tool_failure code=MUTATION_BUSY"]


# ----------------------------------------------------------------- call ledger


@pytest.fixture
def _fresh_chain(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EXOMEM_CALL_LEDGER_DIR", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_CALL_LEDGER", raising=False)
    call_ledger.reset_chain_cache()
    yield
    call_ledger.reset_chain_cache()


def _record(tool: str, request_id: str = "req-1") -> dict:
    row = call_ledger.record_call(
        request_id=request_id, tool=tool, outcome="ok", duration_ms=1.0
    )
    assert row is not None
    return row


def test_trace_includes_the_call_ledger(_log_dir: Path, _fresh_chain) -> None:
    """The ledger is the only source covering *every* tool call, so it is what
    makes a trace complete: a tool that is neither a read, a query, nor a
    mutation previously joined nothing but prose."""
    _record("browse_memory")
    _record("browse_memory", request_id="req-other")

    records = obs_cli.trace("req-1")

    assert [record["_source"] for record in records] == ["ledger"]
    assert records[0]["tool"] == "browse_memory"


def test_trace_reads_the_ledger_archive_too(
    _log_dir: Path, _fresh_chain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Archive filenames are content-addressed, so the rotated generation is
    not `<name>.1` and filename order says nothing about age. A trace that only
    knew about `.1` would silently stop at the live file's first row."""
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_ROTATE_BYTES", "1")
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_KEEP_ROWS", "2")

    for _ in range(6):
        _record("browse_memory")

    records = obs_cli.trace("req-1")
    assert [record["sequence"] for record in records] == [1, 2, 3, 4, 5, 6]


def test_verify_ledger_is_clean_on_an_intact_chain(_log_dir: Path, _fresh_chain) -> None:
    for _ in range(4):
        _record("browse_memory")
    assert obs_cli.verify_ledger() == []


def _rotated(monkeypatch: pytest.MonkeyPatch, count: int = 6) -> list[Path]:
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_ROTATE_BYTES", "1")
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_KEEP_ROWS", "2")
    for _ in range(count):
        _record("browse_memory")
    segments = obs_cli._ledger_archive_generations()
    assert len(segments) >= 2, "the fixture must actually rotate more than once"
    return segments


def test_verify_ledger_reports_a_break_at_a_rotation_boundary(
    _log_dir: Path, _fresh_chain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifying each segment in isolation would miss exactly the break that
    rotation itself could cause, so the walk is archive-then-live as one chain."""
    segments = _rotated(monkeypatch)
    assert obs_cli.verify_ledger() == []

    segments[-1].unlink()  # the newest archived segment: a hole mid-chain

    assert obs_cli.verify_ledger(), "a missing archive segment must not verify clean"


def test_verify_ledger_reports_a_dropped_oldest_segment(
    _log_dir: Path, _fresh_chain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The subtle one: with the head gone, nothing precedes the first surviving
    row to contradict it, so the chain merely appears to start later than it
    did. Only anchoring the walk to the genesis row catches it."""
    segments = _rotated(monkeypatch)
    assert obs_cli.verify_ledger() == []

    segments[0].unlink()

    problems = obs_cli.verify_ledger()
    assert problems, "a missing oldest segment must not verify clean"
    assert any("genesis" in problem for problem in problems)


def test_the_ledger_is_a_first_class_logs_file(_log_dir: Path) -> None:
    assert "ledger" in obs_cli.file_aliases()
    assert obs_cli.resolve_log_file("ledger") == _log_dir / "ledger.jsonl"


def test_logs_verify_exits_nonzero_on_a_broken_chain(_log_dir: Path, _fresh_chain) -> None:
    """The exit code is the contract a runbook or a CI step reads, so an intact
    chain and a broken one must not both look like success."""
    from exomem.__main__ import _logs_main

    for _ in range(3):
        _record("browse_memory")
    assert _logs_main(["verify"]) == 0

    path = _log_dir / "ledger.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")

    assert _logs_main(["verify"]) == 1
