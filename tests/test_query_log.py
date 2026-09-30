"""query_log is best-effort structured logging for the retrieval feedback loop.

The suite-wide conftest sets EXOMEM_DISABLE_EMBEDDINGS, which also disables
query_log — so these tests both (a) confirm the no-op-when-disabled contract and
(b) explicitly re-enable + redirect the JSONL paths to tmp to exercise writes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import query_log


class _FakeHit:
    def __init__(self, path: str, type_: str, signals: dict) -> None:
        self._d = {"path": path, "type": type_, "signals": signals}

    def as_dict(self) -> dict:
        return dict(self._d)


def test_noop_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setattr(query_log, "WRITES_PATH", tmp_path / "writes.jsonl")
    query_log.log_write_call(tool="note", written_path="x", cited_sources=[])
    assert not (tmp_path / "writes.jsonl").exists()


def test_logs_find_call_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_QUERY_LOG", raising=False)
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
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_QUERY_LOG", raising=False)
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
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_DISABLE_QUERY_LOG", "1")
    wpath = tmp_path / "writes.jsonl"
    monkeypatch.setattr(query_log, "WRITES_PATH", wpath)
    query_log.log_write_call(tool="note", written_path="x", cited_sources=[])
    assert not wpath.exists()


def test_logs_get_call_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_QUERY_LOG", raising=False)
    rpath = tmp_path / "reads.jsonl"
    monkeypatch.setattr(query_log, "READS_PATH", rpath)

    query_log.log_get_call(
        read_path="Knowledge Base/Notes/Insights/a.md",
        frontmatter_only=False,
        include_history=True,
    )
    rec = json.loads(rpath.read_text(encoding="utf-8").splitlines()[0])
    assert rec["tool"] == "get"
    assert rec["read_path"] == "Knowledge Base/Notes/Insights/a.md"
    assert rec["frontmatter_only"] is False
    assert rec["include_history"] is True


def test_get_call_noop_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_DISABLE_QUERY_LOG", "1")
    rpath = tmp_path / "reads.jsonl"
    monkeypatch.setattr(query_log, "READS_PATH", rpath)
    query_log.log_get_call(read_path="Knowledge Base/Notes/Insights/a.md")
    assert not rpath.exists()


def test_content_private_logging_disables_all_three_journals_in_cloud_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Design D1.2 "Journals": the query, read and write journals are off
    whenever content-private logging is enabled, checked in `query_log`
    itself so this fails closed even if the manifest forgets to set
    `EXOMEM_DISABLE_QUERY_LOG`. `EXOMEM_DISABLE_EMBEDDINGS` is explicitly
    removed here -- the suite's conftest sets it, which also disables
    `query_log` on its own and is exactly why the earlier version of this
    test suite missed this finding."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_QUERY_LOG", raising=False)
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    qpath = tmp_path / "queries.jsonl"
    wpath = tmp_path / "writes.jsonl"
    rpath = tmp_path / "reads.jsonl"
    monkeypatch.setattr(query_log, "QUERIES_PATH", qpath)
    monkeypatch.setattr(query_log, "WRITES_PATH", wpath)
    monkeypatch.setattr(query_log, "READS_PATH", rpath)

    hits = [_FakeHit("Knowledge Base/Notes/Insights/a.md", "insight", {})]
    query_log.log_find_call(
        query="a distinctive query phrase",
        mode="hybrid",
        scope="kb",
        types=None,
        projects=None,
        tags=None,
        limit=10,
        rerank=False,
        prefer_compiled=True,
        graph=True,
        hits=hits,
    )
    query_log.log_get_call(read_path="Knowledge Base/Notes/Insights/a.md")
    query_log.log_write_call(
        tool="note", written_path="Knowledge Base/Notes/Insights/a.md", cited_sources=[]
    )

    assert not qpath.exists()
    assert not rpath.exists()
    assert not wpath.exists()
