"""query_log.py JSONL size-cap rotation (EXOMEM_JSONL_MAX_MB, keep one .1
generation) and usage.read_jsonl reading both the live file and the rotated
generation. Also: additive correlation fields never change existing shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import query_log, usage


@pytest.fixture(autouse=True)
def _reset_query_log_caches():
    query_log._size_cache.clear()
    query_log._append_counts.clear()
    yield
    query_log._size_cache.clear()
    query_log._append_counts.clear()


def _seed_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, n: int) -> Path:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_QUERY_LOG", raising=False)
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(tmp_path))
    for i in range(n):
        query_log.log_write_call(tool="note", written_path=f"path-{i}", cited_sources=[])
    return tmp_path / "writes.jsonl"


def test_rotates_when_size_cap_is_exceeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_JSONL_MAX_MB", "0.001")  # ~1KB cap, easy to exceed
    path = _seed_writes(tmp_path, monkeypatch, n=200)  # forces a stat resync + rotation
    rotated = path.with_name(path.name + ".1")
    assert rotated.exists(), "expected at least one rotation by 200 appends over a 1KB cap"


def test_keeps_exactly_one_rotated_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_JSONL_MAX_MB", "0.001")
    path = _seed_writes(tmp_path, monkeypatch, n=250)
    rotated = path.with_name(path.name + ".1")
    double_rotated = path.with_name(path.name + ".2")
    assert rotated.exists()
    assert not double_rotated.exists()


def test_no_rotation_under_the_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_JSONL_MAX_MB", "64")
    path = _seed_writes(tmp_path, monkeypatch, n=5)
    rotated = path.with_name(path.name + ".1")
    assert path.exists()
    assert not rotated.exists()


def test_usage_read_jsonl_reads_live_and_rotated_generation(tmp_path: Path) -> None:
    path = tmp_path / "writes.jsonl"
    rotated = path.with_name(path.name + ".1")
    rotated.write_text(json.dumps({"tool": "note", "written_path": "old"}) + "\n", encoding="utf-8")
    path.write_text(json.dumps({"tool": "note", "written_path": "new"}) + "\n", encoding="utf-8")

    records = usage.read_jsonl(path)
    written_paths = {r["written_path"] for r in records}
    assert written_paths == {"old", "new"}


def test_usage_read_jsonl_without_rotated_generation_is_unaffected(tmp_path: Path) -> None:
    path = tmp_path / "writes.jsonl"
    path.write_text(json.dumps({"tool": "note", "written_path": "solo"}) + "\n", encoding="utf-8")
    records = usage.read_jsonl(path)
    assert [r["written_path"] for r in records] == ["solo"]


def test_additive_fields_present_without_disturbing_original_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _seed_writes(tmp_path, monkeypatch, n=1)
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    # Original fields, byte-for-byte field names, still present.
    assert record["tool"] == "note"
    assert record["written_path"] == "path-0"
    assert record["cited_sources"] == []
    assert "ts" in record  # local-naive, untouched semantics
    # Additive correlation fields.
    assert "ts_utc" in record
    assert record["outcome"] == "success"
    assert "request_id" in record


def test_jsonl_max_mb_env_invalid_falls_back_to_default() -> None:
    import os

    os.environ["EXOMEM_JSONL_MAX_MB"] = "not-a-number"
    try:
        assert query_log._jsonl_max_bytes() == int(64 * 1024 * 1024)
    finally:
        del os.environ["EXOMEM_JSONL_MAX_MB"]
