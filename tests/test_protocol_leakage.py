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
        authored_literals={"title": "LongMemEval case {case} session {session}", "tags": ["longmemeval"]},
        harness_fields={"title": "LongMemEval case 1 session 1", "tags": ["longmemeval"]},
        gold=_gold(),
    )
    assert not [finding for finding in findings if finding.severity == "case-invalidating"]


def test_ingest_gold_detectors_ignore_numeric_session_interpolation() -> None:
    """A numeric gold must not collide with a harness session ordinal."""

    from protocol.leakage import scan_ingest
    from protocol.models import CaseGold

    gold = CaseGold(case_id="case", answer="3", answer_session_ids=[], question_type="knowledge-update", question="Which number?")
    findings = scan_ingest(
        [],
        {"prefix": "Session ordinal: {session_ordinal}"},
        {"prefix": "Session ordinal: 3"},
        gold,
    )
    assert not {finding.detector for finding in findings} & {"gold-text", "gold-shingle"}


def test_ingest_gold_detectors_ignore_timestamp_interpolation() -> None:
    """A year-valued gold must not collide with a rendered timestamp."""

    from protocol.leakage import scan_ingest
    from protocol.models import CaseGold

    gold = CaseGold(case_id="case", answer="2023", answer_session_ids=[], question_type="knowledge-update", question="Which year?")
    findings = scan_ingest(
        [],
        {"prefix": "Session timestamp: {timestamp}"},
        {"prefix": "Session timestamp: 2023-05-20T00:00:00Z"},
        gold,
    )
    assert not {finding.detector for finding in findings} & {"gold-text", "gold-shingle"}


def test_ingest_gold_detector_still_scans_authored_title_literals() -> None:
    from protocol.leakage import scan_ingest
    from protocol.models import CaseGold

    gold = CaseGold(case_id="case", answer="violet cedar", answer_session_ids=[], question_type="knowledge-update", question="Which title?")
    findings = scan_ingest([], {"title": "The violet cedar"}, {"title": "The violet cedar"}, gold)
    assert "gold-text" in {finding.detector for finding in findings}


def test_ingest_raw_upstream_detector_scans_interpolated_harness_payload() -> None:
    from protocol.leakage import scan_ingest

    findings = scan_ingest(
        [],
        {"title": "LongMemEval case {case} session {session}"},
        {"title": "LongMemEval case 1 session answer_3b7c9"},
        _gold(),
    )
    assert "raw-upstream-id" in {finding.detector for finding in findings}
