"""query_log is best-effort structured logging for the retrieval feedback loop.

The suite-wide conftest sets KB_MCP_DISABLE_EMBEDDINGS, which also disables
query_log — so these tests both (a) confirm the no-op-when-disabled contract and
(b) explicitly re-enable + redirect the JSONL paths to tmp to exercise writes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kb_mcp import query_log


class _FakeHit:
    def __init__(self, path: str, type_: str, signals: dict) -> None:
        self._d = {"path": path, "type": type_, "signals": signals}

    def as_dict(self) -> dict:
        return dict(self._d)


def test_noop_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_MCP_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setattr(query_log, "WRITES_PATH", tmp_path / "writes.jsonl")
    query_log.log_write_call(tool="note", written_path="x", cited_sources=[])
    assert not (tmp_path / "writes.jsonl").exists()


def test_logs_find_call_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KB_MCP_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.delenv("KB_MCP_DISABLE_QUERY_LOG", raising=False)
    qpath = tmp_path / "queries.jsonl"
    monkeypatch.setattr(query_log, "QUERIES_PATH", qpath)

    hits = [
        _FakeHit("Knowledge Base/Notes/Insights/a.md", "insight", {"vector_rank": 1}),
        _FakeHit("Knowledge Base/Sources/Articles/b.md", "source", {"bm25_rank": 2}),
    ]
    query_log.log_find_call(
        query="metabolism", mode="hybrid", scope="kb",
        types=None, projects=["health"], tags=None,
        limit=10, rerank=False, prefer_compiled=True, graph=True, hits=hits,
    )
    lines = qpath.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["query"] == "metabolism"
    assert rec["n_results"] == 2
    assert rec["filters"]["projects"] == ["health"]
    assert rec["top_k"][0] == {
        "path": "Knowledge Base/Notes/Insights/a.md",
        "type": "insight",
        "signals": {"vector_rank": 1},
    }
    # No bodies/excerpts leak into the log.
    assert "excerpt" not in rec["top_k"][0]


def test_logs_write_call_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KB_MCP_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.delenv("KB_MCP_DISABLE_QUERY_LOG", raising=False)
    wpath = tmp_path / "writes.jsonl"
    monkeypatch.setattr(query_log, "WRITES_PATH", wpath)

    query_log.log_write_call(
        tool="note",
        written_path="Knowledge Base/Notes/Insights/new.md",
        cited_sources=["Knowledge Base/Sources/Articles/src"],
    )
    rec = json.loads(wpath.read_text(encoding="utf-8").splitlines()[0])
    assert rec["tool"] == "note"
    assert rec["written_path"] == "Knowledge Base/Notes/Insights/new.md"
    assert rec["cited_sources"] == ["Knowledge Base/Sources/Articles/src"]


def test_explicit_query_log_disable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KB_MCP_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("KB_MCP_DISABLE_QUERY_LOG", "1")
    wpath = tmp_path / "writes.jsonl"
    monkeypatch.setattr(query_log, "WRITES_PATH", wpath)
    query_log.log_write_call(tool="note", written_path="x", cited_sources=[])
    assert not wpath.exists()
