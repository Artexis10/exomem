"""Keep the diagnostic truth bound and prevent future-question leakage."""

from __future__ import annotations

import hashlib
import json

from epistemic.memory_loop import destination_cohesion as fixture


def test_independent_truth_is_bound_to_the_same_source_cases() -> None:
    source = json.loads(fixture.CASES_PATH.read_text(encoding="utf-8"))
    truth = json.loads(fixture.TRUTH_PATH.read_text(encoding="utf-8"))

    assert truth["source_cases_sha256"] == hashlib.sha256(fixture.CASES_PATH.read_bytes()).hexdigest()
    source_ids = [case["case_id"] for case in source["cases"]]
    truth_ids = [case["case_id"] for case in truth["cases"]]
    assert len(set(source_ids)) == len(source_ids)
    assert sorted(source_ids) == sorted(truth_ids)


def test_capture_input_withholds_the_later_question_and_truth() -> None:
    source = json.loads(fixture.CASES_PATH.read_text(encoding="utf-8"))
    for case in source["cases"]:
        view = fixture.actor_view(case["case_id"])
        assert view == {"episode_id": case["case_id"], "turns": [case["original_turn"]]}
