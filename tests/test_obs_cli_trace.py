"""`exomem trace <request_id>` joins the server log, queries/writes/reads.jsonl,
and mutations.jsonl for one request id, time-ordered. `exomem logs tail|grep`
reads the per-process JSONL files directly."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import obs_cli


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
