"""Red-first tests for the deterministic context-activation scorer (task 2.1).

Per-case x anchor-kind recall/precision with poison; twin false activation
split by status; abstention; supersession marking; token and latency bounds;
duals present; no aggregate field; mechanism-removal test red when the
compiler is disabled; void run when a manifest lacks any digest.
"""

from __future__ import annotations

import json

import pytest
from epistemic.corpora.context_activation import (
    FIXTURES,
    build_corpus,
    fixture_by_id,
)
from membench.utility.context_activation import (
    DISABLED_PACKET,
    REQUIRED_DIGEST_FIELDS,
    ActivationPacket,
    Anchor,
    CurrentStateEntry,
    ManifestVoidError,
    PacketError,
    Unit,
    audit_passed,
    load_packet,
    packet_from_dict,
    report_to_dict,
    run_audit,
    score_case,
    token_size_distribution,
    turn_status,
    validate_manifest,
)

MANIFEST = {
    "fixture_set_digest": "a" * 64,
    "corpus_digest": "b" * 64,
    "threshold_digest": "c" * 64,
    "mechanism": "oracle_packet",
}


def _good_c1_packet() -> ActivationPacket:
    c1 = fixture_by_id("C1")
    anchors = tuple(Anchor(ref=key, title=key, kind="note", status="resolved") for key in c1.gold)
    units = (
        Unit(ref="c1_weekly_limit_insight", role="historical_pattern", text="Usage hits the weekly limit most weeks."),
        Unit(ref="c1_capacity_ceilings_pattern", role="capacity_constraint", text="A capacity ceiling pattern recurs."),
    )
    return ActivationPacket(anchors=anchors, units=units, budget_used_chars=120, latency_ms=42.0)


def _abstained_packet() -> ActivationPacket:
    return ActivationPacket(abstained=True, abstention_reason="unresolved")


# -- packet loading -----------------------------------------------------


def test_packet_from_dict_round_trips_anchors_units_and_budget() -> None:
    data = {
        "anchors": [{"ref": "r1", "title": "R1", "kind": "note", "status": "resolved", "evidence": ["exact_alias"]}],
        "units": [{"ref": "r1", "role": "note", "text": "hello", "lifecycle": "active"}],
        "current_state": [{"anchor": "r1", "source": "records", "as_of": "2026-09-01"}],
        "budget": {"limit_chars": 4000, "used_chars": 5},
        "abstained": False,
    }
    packet = packet_from_dict(data)
    assert packet.anchors[0].ref == "r1"
    assert packet.anchors[0].evidence == ("exact_alias",)
    assert packet.units[0].text == "hello"
    assert packet.current_state[0].source == "records"
    assert packet.budget_used_chars == 5


def test_load_packet_refuses_a_malformed_file(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(PacketError):
        load_packet(path)


def test_load_packet_reads_a_well_formed_file(tmp_path) -> None:
    path = tmp_path / "packet.json"
    path.write_text(json.dumps({"anchors": [], "abstained": True}), encoding="utf-8")
    packet = load_packet(path)
    assert packet.abstained is True


# -- turn_status ---------------------------------------------------------


def test_turn_status_abstained_is_unresolved() -> None:
    assert turn_status(ActivationPacket(abstained=True)) == "unresolved"


def test_turn_status_ambiguity_wins_over_resolved_anchors() -> None:
    packet = ActivationPacket(
        anchors=(Anchor(ref="a", title="A", kind="hub", status="resolved"),),
        ambiguity=("a", "b"),
    )
    assert turn_status(packet) == "ambiguous"


def test_turn_status_resolved_beats_partial() -> None:
    packet = ActivationPacket(
        anchors=(
            Anchor(ref="a", title="A", kind="note", status="partial"),
            Anchor(ref="b", title="B", kind="note", status="resolved"),
        )
    )
    assert turn_status(packet) == "resolved"


def test_turn_status_no_anchors_is_unresolved() -> None:
    assert turn_status(ActivationPacket()) == "unresolved"


# -- score_case: gold recall / poison precision / anchor kind -----------


def test_good_c1_packet_passes_and_reports_recall_and_kind_breakdown() -> None:
    c1 = fixture_by_id("C1")
    score = score_case(_good_c1_packet(), c1)
    assert score.passed, score.failure_reasons
    assert score.gold_hit == score.gold_total == 3
    assert score.poison_hit == 0
    kinds = {tally.kind for tally in score.by_anchor_kind}
    assert kinds == {"records_collection", "note"}
    for tally in score.by_anchor_kind:
        # every anchor-kind row is a numerator/denominator dual
        assert tally.gold_hit <= tally.gold_total
        assert tally.poison_hit <= tally.poison_total


def test_low_gold_recall_fails_the_case() -> None:
    c1 = fixture_by_id("C1")
    only_one = ActivationPacket(anchors=(Anchor(ref=c1.gold[0], title="x", kind="note", status="resolved"),))
    score = score_case(only_one, c1)
    assert not score.passed
    assert any("recall" in reason for reason in score.failure_reasons)


def test_poison_hit_fails_the_case_even_with_full_gold_recall() -> None:
    c1 = fixture_by_id("C1")
    anchors = tuple(Anchor(ref=key, title=key, kind="note", status="resolved") for key in c1.gold)
    anchors += (Anchor(ref=c1.poison[0], title="poison", kind="note", status="resolved"),)
    score = score_case(ActivationPacket(anchors=anchors), c1)
    assert not score.passed
    assert score.poison_hit == 1
    assert any("poison" in reason for reason in score.failure_reasons)


def test_must_include_and_must_exclude_are_checked_against_injected_text() -> None:
    c8 = fixture_by_id("C8")
    good = ActivationPacket(
        anchors=(Anchor(ref="c8_active_head", title="head", kind="note", status="resolved"),),
        units=(Unit(ref="c8_active_head", role="current_state", text="The current onboarding approach."),),
    )
    good_score = score_case(good, c8)
    assert good_score.passed, good_score.failure_reasons

    bad = ActivationPacket(
        anchors=(Anchor(ref="c8_active_head", title="head", kind="note", status="resolved"),),
        units=(
            Unit(
                ref="c8_active_head",
                role="current_state",
                text="This presents the retired approach presented as current.",
            ),
        ),
    )
    bad_score = score_case(bad, c8)
    assert not bad_score.passed
    assert "retired approach presented as current" in bad_score.must_exclude_present


# -- twin false activation -------------------------------------------------


def test_twin_abstention_passes() -> None:
    t1 = fixture_by_id("T1")
    score = score_case(_abstained_packet(), t1)
    assert score.passed, score.failure_reasons
    assert score.twin_false_activation is False


def test_twin_resolving_the_paired_cases_gold_is_false_activation() -> None:
    t1 = fixture_by_id("T1")
    c1 = fixture_by_id("C1")
    packet = ActivationPacket(anchors=(Anchor(ref=c1.gold[0], title="x", kind="note", status="resolved"),))
    score = score_case(packet, t1)
    assert score.twin_false_activation is True
    assert not score.passed


def test_twin_partial_or_ambiguous_status_is_reported_not_punished() -> None:
    t4 = fixture_by_id("T4")  # expected_status="ambiguous"
    packet = ActivationPacket(ambiguity=("t4_shared_first_name_entity_a", "t4_shared_first_name_entity_b"))
    score = score_case(packet, t4)
    assert score.observed_status == "ambiguous"
    assert score.status_match is True
    assert score.twin_false_activation is False
    assert score.passed


def test_narrow_legitimate_twin_resolving_its_own_gold_is_not_false_activation() -> None:
    t7 = fixture_by_id("T7")  # gold=("c7_hub_feature",); expected_status="resolved"
    packet = ActivationPacket(anchors=(Anchor(ref="c7_hub_feature", title="feature", kind="hub", status="resolved"),))
    score = score_case(packet, t7)
    assert score.twin_false_activation is False
    assert score.passed, score.failure_reasons


# -- abstention correctness for the no-memory case (C6) -------------------


def test_c6_no_memory_case_passes_when_it_injects_nothing() -> None:
    c6 = fixture_by_id("C6")
    score = score_case(ActivationPacket(abstained=True), c6)
    assert score.passed, score.failure_reasons


def test_c6_no_memory_case_fails_if_anything_is_injected() -> None:
    c6 = fixture_by_id("C6")
    leaky = ActivationPacket(
        anchors=(Anchor(ref="something", title="x", kind="note", status="resolved"),),
        budget_used_chars=40,
    )
    score = score_case(leaky, c6)
    assert not score.passed
    assert any("no-memory" in reason for reason in score.failure_reasons)


# -- packet token/latency accounting --------------------------------------


def test_packet_token_count_is_over_injected_text_only() -> None:
    packet = ActivationPacket(
        units=(Unit(ref="a", role="note", text="a short unit of text"),),
        current_state=(CurrentStateEntry(anchor="a", source="a short current-state note"),),
    )
    score = score_case(packet, fixture_by_id("C6"))
    assert score.packet_tokens > 0


def test_empty_packet_has_zero_tokens() -> None:
    score = score_case(ActivationPacket(), fixture_by_id("C6"))
    assert score.packet_tokens == 0


def test_token_size_distribution_flags_the_hard_cap() -> None:
    class _Stub:
        def __init__(self, case_id: str, tokens: int) -> None:
            self.case_id = case_id
            self.packet_tokens = tokens

    scores = [_Stub("C1", 500), _Stub("C2", 900), _Stub("C3", 2500)]
    distribution = token_size_distribution(scores)
    assert distribution["over_hard_cap"] == ["C3"]
    assert distribution["p50"] is not None and distribution["p95"] is not None


# -- manifest validation ----------------------------------------------------


def test_manifest_missing_any_digest_is_void() -> None:
    for missing_field in REQUIRED_DIGEST_FIELDS:
        incomplete = {k: v for k, v in MANIFEST.items() if k != missing_field}
        with pytest.raises(ManifestVoidError):
            validate_manifest(incomplete)


def test_complete_manifest_validates() -> None:
    manifest = validate_manifest(MANIFEST)
    assert manifest.fixture_set_digest == MANIFEST["fixture_set_digest"]


# -- mechanism removal: the audit exits red with no packets supplied -----


def test_mechanism_removal_every_positive_case_fails_with_no_packets_supplied() -> None:
    manifest = validate_manifest(MANIFEST)
    report = run_audit({}, manifest=manifest)
    assert audit_passed(report) is False
    positive_cases = [f for f in FIXTURES if f.expected_status != "unresolved"]
    failing_case_ids = {score.case_id for score in report.per_case if not score.passed}
    assert {f.case_id for f in positive_cases} <= failing_case_ids


def test_mechanism_removal_disabled_packet_is_the_documented_kill_switch_shape() -> None:
    assert DISABLED_PACKET.abstained is True
    assert DISABLED_PACKET.abstention_reason == "disabled"


# -- report shape: no aggregate field, every metric carries its dual -----


_FORBIDDEN_AGGREGATE_KEYS = ("aggregate", "mean_", "overall_score", "weighted")


def _walk_keys(value: object) -> list[str]:
    keys: list[str] = []
    if isinstance(value, dict):
        for key, sub in value.items():
            keys.append(str(key))
            keys.extend(_walk_keys(sub))
    elif isinstance(value, list):
        for item in value:
            keys.extend(_walk_keys(item))
    return keys


def test_report_carries_no_aggregate_field_and_full_fixture_coverage() -> None:
    manifest = validate_manifest(MANIFEST)
    packets = {"C1": _good_c1_packet()}
    report = run_audit(packets, manifest=manifest)
    payload = report_to_dict(report)
    all_keys = _walk_keys(payload)
    for forbidden in _FORBIDDEN_AGGREGATE_KEYS:
        assert not any(forbidden in key.lower() for key in all_keys), forbidden
    assert payload["fixtures_total"] == len(FIXTURES)
    assert payload["verdict"] == "reviewed"  # every fixture was scored (against DISABLED_PACKET where absent)
    for case in payload["per_case"]:
        assert set(case["gold"]) == {"hit", "total"}
        assert set(case["poison"]) == {"hit", "total"}
        for kind_row in case["by_anchor_kind"]:
            assert set(kind_row["gold"]) == {"hit", "total"}
            assert set(kind_row["poison"]) == {"hit", "total"}


def test_report_is_no_verdict_when_the_fixture_set_is_partial() -> None:
    manifest = validate_manifest(MANIFEST)
    report = run_audit({}, manifest=manifest, fixtures=FIXTURES[:-1])
    payload = report_to_dict(report)
    assert payload["verdict"] == "no_verdict"


# -- corpus-backed end-to-end key_to_ref path ------------------------------


def test_score_case_resolves_through_a_corpus_key_to_path_mapping(tmp_path) -> None:
    corpus_manifest = build_corpus(tmp_path, distractor_count=5)
    c2 = fixture_by_id("C2")
    anchors = tuple(
        Anchor(ref=corpus_manifest.key_to_path[key], title=key, kind="note", status="resolved") for key in c2.gold
    )
    score = score_case(ActivationPacket(anchors=anchors), c2, key_to_ref=corpus_manifest.key_to_path)
    assert score.gold_hit == score.gold_total
