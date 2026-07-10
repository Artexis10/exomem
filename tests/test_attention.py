"""Unit tests for the `attention` review surface — the pure `_rank` composer.

Torch-free: every test builds synthetic `AuditFinding` objects and feeds them to
`attention._rank` directly (no vault, no embeddings, no audit pass). The end-to-end
path (audit -> _rank) over the fixture vault is covered by
`test_consolidated_tools.py::test_attention_tool_composes_review_surface` (MCP),
`test_rest_registry.py::test_attention_route_and_openapi_params` (REST), and
`test_cli_core_ops.py::test_attention_runs` (CLI).
"""

from __future__ import annotations

import pytest

from exomem import attention as attention_module
from exomem.audit import AuditFinding

C = "corpus_contradictions"
S = "stale_review"
U = "unprocessed_source"
R = "relation_debt"


def _f(category: str, path: str, *, severity: str = "info",
       paths: list[str] | None = None, meta: dict | None = None) -> AuditFinding:
    return AuditFinding(
        category=category,
        severity=severity,
        path=path,
        detail=f"{category} finding for {path}",
        proposed_fix="orig fix text",
        paths=paths,
        meta=meta,
    )


def _paths(report) -> list[str]:
    return [it.path for it in report.items]


def test_rank_major_interleave_equal_weights():
    """Equal weights → rank-major, category-minor interleave (c-before-s-before-u)."""
    findings = [
        _f(C, "c1"), _f(C, "c2"), _f(C, "c3"),
        _f(S, "s1"), _f(S, "s2"), _f(S, "s3"),
        _f(U, "u1"), _f(U, "u2"), _f(U, "u3"),
    ]
    report = attention_module._rank(findings)
    assert _paths(report) == ["c1", "s1", "u1", "c2", "s2", "u2", "c3", "s3", "u3"]
    assert report.summary == {C: 3, S: 3, U: 3}
    assert report.total == 9
    assert report.truncated == 0
    assert report.note is None


def test_multi_signal_additivity_and_dedup():
    """A note flagged by two queues dedups to one item, sums votes, and rises."""
    findings = [
        _f(C, "c1"), _f(C, "N"),     # N at contradiction rank 2
        _f(S, "N"), _f(S, "s2"),     # N at stale rank 1
    ]
    report = attention_module._rank(findings)

    # N first (0.0325 > c1 0.0164 > s2 0.0161)
    assert _paths(report)[0] == "N"
    n = report.items[0]
    assert n.categories == [C, S]                 # ordered by category preference
    assert len(n.reasons) == 2
    k = attention_module._RRF_K
    expected = round(1.0 / (k + 2) + 1.0 / (k + 1), 6)   # contradiction r2 + stale r1
    assert n.score == expected
    assert n.severity == "info"
    # ranks above both single-flagged items
    assert _paths(report).index("N") < _paths(report).index("c1")
    assert _paths(report).index("N") < _paths(report).index("s2")
    # dedup: N appears exactly once
    assert _paths(report).count("N") == 1


def test_contradiction_anchor_and_pair_preserved():
    """A contradiction pair surfaces under its anchor; the partner lives in the reason."""
    findings = [
        _f(C, "A", paths=["A", "B"], meta={"cosine": 0.83, "priority": 1.2, "same_family": False}),
    ]
    report = attention_module._rank(findings)
    assert _paths(report) == ["A"]                       # B is not its own item
    reason = report.items[0].reasons[0]
    assert reason["category"] == C
    assert reason["related_paths"] == ["A", "B"]
    assert reason["meta"]["cosine"] == 0.83


def test_contradiction_partner_independent_only_when_flagged():
    """B becomes an item only if independently flagged by another queue."""
    findings = [
        _f(C, "A", paths=["A", "B"]),
        _f(S, "B"),
    ]
    report = attention_module._rank(findings)
    assert set(_paths(report)) == {"A", "B"}


def test_truncation_caps_and_counts():
    findings = [_f(S, f"s{i}") for i in range(1, 6)]     # 5 items
    report = attention_module._rank(findings, limit=2)
    assert report.shown == 2
    assert report.total == 5
    assert report.truncated == 3
    assert len(report.items) == 2
    assert report.note is not None and "3 more" in report.note


def test_no_note_when_within_limit():
    findings = [_f(S, f"s{i}") for i in range(1, 4)]
    report = attention_module._rank(findings, limit=25)
    assert report.truncated == 0
    assert report.note is None


def test_upstream_contradiction_summary_is_folded_not_surfaced():
    """The contradiction queue's trailing summary finding is folded into upstream_truncated."""
    findings = [
        _f(C, "A", paths=["A", "B"]),
        _f(C, "Knowledge Base/", meta={"truncated": 7, "shown": 40, "total": 47}),
        _f(S, "s1"),
    ]
    report = attention_module._rank(findings)
    assert "Knowledge Base/" not in _paths(report)        # not a review item
    assert report.upstream_truncated == 7
    assert report.summary[C] == 1                          # the real pair only
    assert report.note is not None and "7" in report.note
    assert "upstream" in report.note.lower() or "capped upstream" in report.note.lower()


def test_categories_filter():
    findings = [_f(C, "c1"), _f(S, "s1"), _f(U, "u1")]
    report = attention_module._rank(findings, categories={S})
    assert _paths(report) == ["s1"]
    assert set(report.summary) == {S}


def test_severity_is_max_over_reasons():
    findings = [_f(U, "p", severity="warn"), _f(S, "p", severity="info")]
    report = attention_module._rank(findings)
    assert report.items[0].severity == "warn"


def test_ranking_is_deterministic():
    findings = [
        _f(C, "c1"), _f(C, "N"),
        _f(S, "N"), _f(S, "s2"),
        _f(U, "u1"),
    ]
    a = attention_module._rank(findings).as_dict()
    b = attention_module._rank(findings).as_dict()
    assert a == b


def test_item_proposed_fix_is_review_only():
    findings = [_f(S, "s1")]
    report = attention_module._rank(findings)
    fix = report.items[0].proposed_fix.lower()
    assert "review only" in fix
    assert "auto-acted" in fix or "never auto" in fix


def test_invalid_category_raises(tmp_path):
    """attention() validates categories before touching the vault."""
    with pytest.raises(ValueError, match="unknown attention categories"):
        attention_module.attention(tmp_path, categories=["bogus"])


def test_as_dict_shape():
    findings = [_f(C, "A", paths=["A", "B"], meta={"cosine": 0.83}), _f(S, "A")]
    d = attention_module._rank(findings).as_dict()
    assert set(d) >= {"items", "summary", "shown", "total", "truncated", "upstream_truncated", "note"}
    item = d["items"][0]
    assert set(item) >= {"path", "score", "severity", "categories", "reasons", "proposed_fix"}
    assert isinstance(item["reasons"], list)


def test_empty_findings():
    report = attention_module._rank([])
    assert report.items == []
    assert report.total == 0
    assert report.shown == 0
    assert report.summary == {}
    assert report.truncated == 0
    assert report.upstream_truncated == 0
    assert report.note is None


def test_relation_debt_is_composed_after_other_rank_ties():
    report = attention_module._rank([_f(R, "relationless"), _f(U, "source")])

    assert _paths(report) == ["source", "relationless"]
    assert report.summary == {U: 1, R: 1}


def test_limit_zero_or_negative_surfaces_all():
    """`limit <= 0` is the uncapped convention (mirrors EXOMEM_CONTRADICTION_TOP_N=0)."""
    findings = [_f(S, f"s{i}") for i in range(1, 6)]
    for lim in (0, -3):
        report = attention_module._rank(findings, limit=lim)
        assert report.shown == 5
        assert report.total == 5
        assert report.truncated == 0
        assert report.note is None


def test_same_category_double_anchor_one_item_best_rank_score():
    """One note anchoring two contradiction pairs → one item, both reasons, best-rank score."""
    findings = [
        _f(C, "A", paths=["A", "B"], meta={"cosine": 0.90}),   # rank 1
        _f(C, "c2"),                                           # rank 2
        _f(C, "A", paths=["A", "D"], meta={"cosine": 0.85}),   # rank 3, same anchor
    ]
    report = attention_module._rank(findings)
    a = next(it for it in report.items if it.path == "A")
    assert len(a.reasons) == 2
    assert sorted(r["related_paths"] for r in a.reasons) == [["A", "B"], ["A", "D"]]
    # score uses A's BEST rank (1), not the sum of rank 1 + rank 3 — no domination by pair count
    k = attention_module._RRF_K
    assert a.score == round(1.0 / (k + 1), 6)
    assert report.summary[C] == 3            # all three contributing findings counted (pre-dedup)
    assert _paths(report).count("A") == 1    # deduped to one item
