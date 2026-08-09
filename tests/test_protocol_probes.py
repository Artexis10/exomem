from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    ("hits", "expected"),
    [
        (["current"], "superseded"),
        (["current", "stale"], "both_returned"),
        (["stale"], "stale_only"),
        ([], "unresolvable"),
    ],
)
def test_update_outcomes(hits: list[str], expected: str) -> None:
    from protocol.probes import classify_update_outcome

    assert classify_update_outcome(hits) == expected


def test_probe_specs_include_inconclusive_by_design() -> None:
    from protocol.probes import known_answer_probe_specs

    specs = known_answer_probe_specs()
    assert {spec.kind for spec in specs} == {"lexical-rare-token", "semantic-zero-overlap", "update-current-state"}
    assert "inconclusive-by-design" in specs[0].allowed_outcomes


def test_update_outcome_classifies_mapping_shaped_hits() -> None:
    from protocol.probes import classify_update_outcome

    assert classify_update_outcome([{"state": "current"}, {"revision": "stale"}]) == "both_returned"


def test_probe_result_accepts_every_closed_outcome() -> None:
    from protocol.models import ProbeResult

    outcomes = ("pass", "fail", "inconclusive-by-design", "superseded", "both_returned", "stale_only", "unresolvable")
    results = [ProbeResult(case_id="case", probe_kind="update-current-state", outcome=outcome) for outcome in outcomes]
    assert [result.outcome for result in results] == list(outcomes)
