from __future__ import annotations


def _gold():
    from protocol.models import CaseGold

    return CaseGold(
        case_id="case-1",
        answer="the violet cedar lantern opens at dawn",
        answer_session_ids=["answer_3b7c9"],
        question_type="knowledge-update",
        question="Which lantern opens at dawn?",
    )


def test_ingest_scanner_detects_every_strict_leakage_class() -> None:
    from protocol.leakage import scan_ingest

    findings = scan_ingest(
        ["answer_3b7c9"],
        {"gold_label": "the violet cedar lantern opens at dawn", "category": "knowledge-update"},
        _gold(),
    )
    assert {finding.detector for finding in findings} >= {
        "gold-text",
        "gold-shingle",
        "label-token",
        "raw-upstream-id",
        "structural-key",
    }
    assert all(finding.severity == "case-invalidating" for finding in findings)


def test_search_allows_question_text_but_flags_gold_advisorily() -> None:
    from protocol.leakage import scan_search

    clean = scan_search({"query": "Which lantern opens at dawn?"}, _gold())
    assert not clean
    findings = scan_search({"query": "the violet cedar lantern opens at dawn"}, _gold())
    assert findings and all(finding.severity == "advisory" for finding in findings)


def test_ingest_content_allows_realistic_answer_word_and_gold_text() -> None:
    """Dataset message text may say "answer" and contain the correct answer."""

    from protocol.leakage import scan_ingest

    findings = scan_ingest(
        content_fields=["Can you answer that for me?"],
        harness_fields={"title": "LongMemEval case 1 session 1", "tags": ["longmemeval"]},
        gold=_gold(),
    )
    assert not [finding for finding in findings if finding.severity == "case-invalidating"]
