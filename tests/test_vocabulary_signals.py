import json

import pytest

from exomem import vocabulary_signals as signals


def origin(ref, independence, context="Two entities cooperate on a delivery."):
    return {
        "ref": ref,
        "version": "content-v1",
        "independence_key": independence,
        "context": context,
    }


def pair(origins, **overrides):
    return signals.consider_generic_pair(
        source_ref="entity:a",
        target_ref="entity:b",
        origins=origins,
        **(
            {"resolved": True, "relation": "relates_to", "projection_status": "current"} | overrides
        ),
    )


def test_two_independent_origins_expose_a_generic_pair_without_selecting_meaning():
    result = pair(
        [
            origin("source:1", "origin:1", "The first company supplies equipment to the second."),
            origin(
                "source:2",
                "origin:2",
                "An independent account describes the same supply arrangement.",
            ),
        ]
    )
    assert result["eligibility"] == "eligible"
    assert result["reason"] == "independent_origin_recurrence"
    assert result["independent_origins"] == 2
    assert result["selected_relation"] is None
    assert result["proposed_relation"] is None
    assert {entry["ref"] for entry in result["evidence"]} == {"source:1", "source:2"}
    assert result["review_route"] == {"tool": "connect_memory", "operation": "resolve-relation"}


def test_copy_equivalent_sources_and_a_supplier_keyword_are_not_recurrence():
    result = pair(
        [
            origin("source:1", "same-origin", "supplier"),
            origin("source:1-copy", "same-origin", "supplier"),
        ]
    )
    assert result["eligibility"] == "not_eligible"
    assert result["independent_origins"] == 1
    assert pair([origin("source:1", "origin:1", "supplier")])["eligibility"] == "not_eligible"


def test_explicit_agent_question_does_not_require_recurrence():
    result = pair([], question="Does this evidence support a durable connection?")
    assert result["eligibility"] == "eligible"
    assert result["reason"] == "explicit_meaning_question"
    assert result["question"] == "Does this evidence support a durable connection?"


@pytest.mark.parametrize(
    "overrides",
    [
        {"projection_status": "warming"},
        {"projection_status": "unavailable"},
        {"resolved": False},
    ],
)
def test_unavailable_projection_or_identity_is_not_zero(overrides):
    result = pair([origin("source:1", "origin:1"), origin("source:2", "origin:2")], **overrides)
    assert result["eligibility"] == "unavailable"
    assert result["independent_origins"] is None


def test_missing_independence_cannot_be_invented_from_distinct_paths():
    result = pair([origin("source:1", None), origin("source:2", None)])
    assert result["eligibility"] == "unavailable"
    assert result["reason"] == "origin_independence_unavailable"
    assert result["independent_origins"] is None


def test_two_known_origins_remain_eligible_with_an_unknown_extra():
    result = pair(
        [origin("source:1", "origin:1"), origin("source:2", "origin:2"), origin("legacy", None)]
    )
    assert result["eligibility"] == "eligible"
    assert result["independent_origins"] == 2
    assert result["unresolved_origins"] == 1


def test_a_specific_registered_relation_does_not_automatically_demand_a_new_one():
    result = pair(
        [origin("source:1", "origin:1"), origin("source:2", "origin:2")], relation="part_of"
    )
    assert result["eligibility"] == "not_eligible"


def test_evidence_is_bounded_and_omission_has_continuation():
    origins = [origin(f"source:{index}", f"origin:{index}", "é" * 5000) for index in range(8)]
    result = pair(origins)
    assert len(result["evidence"]) == 4
    assert all(len(value["context"]) <= 800 for value in result["evidence"])
    assert result["continuation"]
    later = pair(origins, continuation=result["continuation"])
    assert len(later["evidence"]) == 4
    assert later["continuation"] is None
    assert not ({x["ref"] for x in result["evidence"]} & {x["ref"] for x in later["evidence"]})


def test_compact_advice_is_one_kib_in_utf8_and_contains_no_raw_source_text():
    result = signals.compact_advisory(
        ref="exomem://review/vocabulary/" + "a" * 24,
        family="relation-type/v1",
        fingerprint="b" * 64,
    )
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 1024
    assert "context" not in result
    assert result["decisions"] == ["reuse", "propose-new", "generic", "no-edge", "defer"]
