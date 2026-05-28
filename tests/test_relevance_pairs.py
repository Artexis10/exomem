"""Unit tests for the query->citation feedback-loop join (pure, no torch)."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import derive_relevance_pairs as drp  # noqa: E402


def _q(ts: str, query: str, paths: list[str]) -> dict:
    return {"ts": ts, "query": query, "top_k": [{"path": p} for p in paths]}


def _w(ts: str, written: str, cited: list[str], tool: str = "note") -> dict:
    return {"ts": ts, "tool": tool, "written_path": written, "cited_sources": cited}


def test_pair_when_query_precedes_write_in_window() -> None:
    queries = [_q(
        "2026-05-29T10:00:00", "binding problem in llms",
        ["Knowledge Base/Notes/Insights/binding.md",
         "Knowledge Base/Sources/Articles/src-a.md"],
    )]
    writes = [_w(
        "2026-05-29T10:05:00", "Knowledge Base/Notes/Insights/new.md",
        ["Knowledge Base/Sources/Articles/src-a"],
    )]
    pairs = drp.derive_pairs(queries, writes, window_seconds=2 * 3600)
    assert len(pairs) == 1
    p = pairs[0]
    assert p["query"] == "binding problem in llms"
    assert p["cited_path"] == "sources/articles/src-a"
    assert p["rank_in_results"] == 2
    assert 0.0 < p["confidence"] <= 0.5  # rank-2 cap


def test_no_pair_when_write_precedes_query() -> None:
    queries = [_q("2026-05-29T11:00:00", "q", ["Knowledge Base/Sources/Articles/s.md"])]
    writes = [_w("2026-05-29T10:00:00", "n", ["Knowledge Base/Sources/Articles/s"])]
    assert drp.derive_pairs(queries, writes, window_seconds=2 * 3600) == []


def test_no_pair_outside_window() -> None:
    queries = [_q("2026-05-29T06:00:00", "q", ["Knowledge Base/Sources/Articles/s.md"])]
    writes = [_w("2026-05-29T10:00:00", "n", ["Knowledge Base/Sources/Articles/s"])]
    assert drp.derive_pairs(queries, writes, window_seconds=2 * 3600) == []  # 4h > 2h


def test_no_pair_when_cited_not_in_results() -> None:
    queries = [_q("2026-05-29T10:00:00", "q", ["Knowledge Base/Notes/Insights/other.md"])]
    writes = [_w("2026-05-29T10:01:00", "n", ["Knowledge Base/Sources/Articles/s"])]
    assert drp.derive_pairs(queries, writes, window_seconds=2 * 3600) == []


def test_rank1_beats_rank2_confidence() -> None:
    queries = [_q(
        "2026-05-29T10:00:00", "q",
        ["Knowledge Base/Sources/Articles/top.md",
         "Knowledge Base/Sources/Articles/second.md"],
    )]
    writes = [_w(
        "2026-05-29T10:01:00", "n",
        ["Knowledge Base/Sources/Articles/top",
         "Knowledge Base/Sources/Articles/second"],
    )]
    pairs = {p["cited_path"]: p for p in drp.derive_pairs(queries, writes, 2 * 3600)}
    assert pairs["sources/articles/top"]["confidence"] > pairs["sources/articles/second"]["confidence"]


def test_propose_golden_skips_existing() -> None:
    pairs = [
        {"query": "new query", "cited_path": "notes/insights/x"},
        {"query": "old query", "cited_path": "notes/insights/y"},
    ]
    proposed = drp._propose_golden(pairs, existing={"old query"})
    assert "new query" in proposed
    assert "old query" not in proposed
    assert proposed["new query"] == ["Knowledge Base/notes/insights/x"]
