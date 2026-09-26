"""Red-first tests for the deterministic context-activation scorer (task 2.1).

Per-case x anchor-kind recall/precision with poison; twin false activation
split by status; abstention; supersession marking; token and latency bounds;
duals present; no aggregate field; mechanism-removal test red when the
compiler is disabled; void run when a manifest lacks any digest.
"""

from __future__ import annotations

import dataclasses
import json

import pytest
from epistemic.corpora.context_activation import (
    FIXTURES,
    build_corpus,
    fixture_by_id,
)
from membench.utility.context_activation import (
    DISABLED_PACKET,
    HEDGED_TWINS_CEILING,
    PRECISION_FLOOR,
    REQUIRED_DIGEST_FIELDS,
    STATEMENT_MAX_CHARS,
    ActivationPacket,
    Anchor,
    CurrentStateEntry,
    ManifestVoidError,
    PacketError,
    Pointer,
    Unit,
    audit_passed,
    injected_char_count,
    load_packet,
    packet_from_dict,
    report_to_dict,
    run_audit,
    score_case,
    score_padding_robustness,
    token_size_distribution,
    turn_status,
    validate_manifest,
)

MANIFEST = {
    "fixture_set_digest": "a" * 64,
    "corpus_digest": "b" * 64,
    "logical_corpus_digest": "d" * 64,
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


def test_packet_from_dict_preserves_structured_missing_from_product_packet() -> None:
    from exomem import working_set

    product_packet = working_set.build_packet(
        items=(),
        anchors=(),
        roles=(),
        current_state=(),
        ambiguity=(),
        missing=({"role": "constraints", "reason": "no_material", "detail": "ü"},),
        max_chars=4000,
        generation={"freshness_key": "k", "index_generation": 1, "roles_hash": "h"},
        status="resolved",
    )

    packet = packet_from_dict(product_packet)

    assert packet.missing == ('{"detail":"ü","reason":"no_material","role":"constraints"}',)
    assert injected_char_count(packet) == len(packet.missing[0])


def test_packet_from_dict_preserves_structured_ambiguity_from_product_packet() -> None:
    from exomem import working_set

    product_packet = working_set.abstained_packet(
        reason="ambiguous",
        max_chars=4000,
        generation={"freshness_key": "k", "index_generation": 1, "roles_hash": "h"},
        ambiguity=(
            {"ref": "north", "title": "Nørth", "kind": "hub", "neighbourhood_size": 2},
        ),
    )

    packet = packet_from_dict(product_packet)

    assert packet.ambiguity == ("north",)
    assert packet.ambiguity_text == (
        '{"kind":"hub","neighbourhood_size":2,"ref":"north","title":"Nørth"}',
    )
    assert injected_char_count(packet) == len(packet.ambiguity_text[0])
    assert packet.abstention_reason == "ambiguous"
    assert turn_status(packet) == "ambiguous"


def test_packet_from_dict_keeps_legacy_string_labels() -> None:
    packet = packet_from_dict(
        {"missing": ["constraints:no_material"], "ambiguity": ["exomem://memory/a"]}
    )

    assert packet.missing == ("constraints:no_material",)
    assert packet.ambiguity == ("exomem://memory/a",)
    assert packet.ambiguity_text == ()


def test_legacy_direct_ambiguity_uses_refs_as_budget_text() -> None:
    packet = ActivationPacket(ambiguity=("north", "south"))

    assert injected_char_count(packet) == len("north\nsouth")
    assert turn_status(packet) == "ambiguous"


@pytest.mark.parametrize("field", ["missing", "ambiguity"])
@pytest.mark.parametrize("entry", [7, ["nested"], None])
def test_packet_from_dict_refuses_unsupported_label_entries(field: str, entry: object) -> None:
    with pytest.raises(PacketError, match=rf"{field} entry must be a string or object"):
        packet_from_dict({field: [entry]})


@pytest.mark.parametrize("field", ["missing", "ambiguity"])
@pytest.mark.parametrize(
    "container",
    [{"role": "constraints", "reason": "no_material"}, "legacy-label", 7, None],
)
def test_packet_from_dict_refuses_non_array_label_containers(
    field: str, container: object
) -> None:
    with pytest.raises(PacketError, match=rf"{field} must be an array"):
        packet_from_dict({field: container})


def test_packet_from_dict_allows_absent_label_arrays() -> None:
    packet = packet_from_dict({})

    assert packet.missing == ()
    assert packet.ambiguity == ()
    assert packet.ambiguity_text == ()


@pytest.mark.parametrize("candidate", [{}, {"ref": ""}, {"ref": "   "}, {"ref": 7}])
def test_packet_from_dict_refuses_ambiguity_objects_without_a_valid_ref(
    candidate: dict[str, object],
) -> None:
    with pytest.raises(PacketError, match="ambiguity object ref must be a non-empty string"):
        packet_from_dict({"ambiguity": [candidate]})


@pytest.mark.parametrize(
    ("case_id", "candidates"),
    [
        (
            "T4",
            (
                {"ref": "t4_shared_first_name_entity_a", "title": "Alex Monroe", "kind": "entity"},
                {"ref": "t4_shared_first_name_entity_b", "title": "Alex Park", "kind": "entity"},
            ),
        ),
        (
            "C7",
            (
                {"ref": "c7_hub_feature", "title": "AI search feature", "kind": "hub"},
                {"ref": "c7_hub_market", "title": "AI search market", "kind": "hub"},
                {"ref": "c7_hub_search_ux", "title": "Search UX", "kind": "hub"},
            ),
        ),
    ],
)
def test_product_ambiguity_candidates_score_by_ref_and_budget_all_text(
    case_id: str, candidates: tuple[dict[str, object], ...]
) -> None:
    from exomem import working_set

    product_packet = working_set.abstained_packet(
        reason="ambiguous",
        max_chars=4000,
        generation={"freshness_key": "k", "index_generation": 1, "roles_hash": "h"},
        ambiguity=candidates,
    )
    packet = packet_from_dict(product_packet)

    score = score_case(packet, fixture_by_id(case_id))
    expected_text = "\n".join(
        json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for candidate in candidates
    )
    assert packet.ambiguity == tuple(str(candidate["ref"]) for candidate in candidates)
    assert injected_char_count(packet) == len(expected_text)
    assert score.observed_status == "ambiguous"
    assert score.gold_hit == score.gold_total
    assert score.poison_hit == 0
    assert score.passed, score.failure_reasons


def test_product_ambiguity_poison_ref_still_fails_twin_safety() -> None:
    from exomem import working_set

    t4 = fixture_by_id("T4")
    product_packet = working_set.abstained_packet(
        reason="ambiguous",
        max_chars=4000,
        generation={"freshness_key": "k", "index_generation": 1, "roles_hash": "h"},
        ambiguity=(
            {"ref": t4.gold[0], "title": "Alex Monroe", "kind": "entity"},
            {"ref": t4.gold[1], "title": "Alex Park", "kind": "entity"},
            {"ref": t4.poison[0], "title": "Wrong colleague", "kind": "entity"},
        ),
    )

    score = score_case(packet_from_dict(product_packet), t4)

    assert score.poison_hit == 1
    assert score.twin_false_activation is True
    assert not score.passed


def test_packet_from_dict_mixed_ambiguity_labels_preserve_both_budget_texts() -> None:
    candidate = {"ref": "north", "title": "Nørth", "kind": "hub"}
    packet = packet_from_dict({"ambiguity": ["legacy-ref", candidate]})
    candidate_text = json.dumps(
        candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    assert packet.ambiguity == ("legacy-ref", "north")
    assert packet.ambiguity_text == ("legacy-ref", candidate_text)
    assert injected_char_count(packet) == len(f"legacy-ref\n{candidate_text}")


def test_product_disabled_abstention_wins_over_ambiguity_candidates() -> None:
    from exomem import working_set

    product_packet = working_set.abstained_packet(
        reason="disabled",
        max_chars=4000,
        generation={"freshness_key": "k", "index_generation": 1, "roles_hash": "h"},
        ambiguity=({"ref": "north", "title": "North", "kind": "hub"},),
    )

    assert turn_status(packet_from_dict(product_packet)) == "unresolved"


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


# -- task 2.3: a superseded ancestor marked (not omitted) is not poison ---


def test_c8_ancestor_marked_superseded_with_named_successor_is_not_poison() -> None:
    c8 = fixture_by_id("C8")
    ancestor = c8.poison[0]
    anchors = (
        Anchor(ref="c8_active_head", title="head", kind="note", status="resolved"),
        Anchor(ref=ancestor, title="ancestor", kind="note", status="resolved"),
    )
    units = (
        Unit(ref="c8_active_head", role="current_state", text="The current onboarding approach is version 3."),
        Unit(
            ref=ancestor,
            role="current_state",
            text="An earlier onboarding approach, since retired.",
            lifecycle="superseded",
            provenance={"superseded_by": "c8_active_head"},
        ),
    )
    score = score_case(ActivationPacket(anchors=anchors, units=units), c8)
    assert score.poison_hit == 0
    assert score.passed, score.failure_reasons


def test_c8_ancestor_marked_superseded_without_named_successor_is_still_poison() -> None:
    c8 = fixture_by_id("C8")
    ancestor = c8.poison[0]
    anchors = (
        Anchor(ref="c8_active_head", title="head", kind="note", status="resolved"),
        Anchor(ref=ancestor, title="ancestor", kind="note", status="resolved"),
    )
    units = (
        Unit(ref="c8_active_head", role="current_state", text="The current onboarding approach."),
        # lifecycle says superseded but names no successor -- not distinguishable
        # from an ordinary stale hit, so it is not credited.
        Unit(
            ref=ancestor,
            role="current_state",
            text="An earlier onboarding approach.",
            lifecycle="superseded",
        ),
    )
    score = score_case(ActivationPacket(anchors=anchors, units=units), c8)
    assert score.poison_hit == 1
    assert not score.passed


def test_c8_ancestor_presented_as_active_is_poison() -> None:
    c8 = fixture_by_id("C8")
    ancestor = c8.poison[0]
    anchors = (
        Anchor(ref="c8_active_head", title="head", kind="note", status="resolved"),
        Anchor(ref=ancestor, title="ancestor", kind="note", status="resolved"),
    )
    units = (
        Unit(ref="c8_active_head", role="current_state", text="The current onboarding approach."),
        Unit(ref=ancestor, role="current_state", text="An earlier onboarding approach."),  # lifecycle default: active
    )
    score = score_case(ActivationPacket(anchors=anchors, units=units), c8)
    assert score.poison_hit == 1
    assert not score.passed


def test_must_include_and_must_exclude_are_checked_against_injected_text() -> None:
    c8 = fixture_by_id("C8")
    good = ActivationPacket(
        anchors=(Anchor(ref="c8_active_head", title="head", kind="note", status="resolved"),),
        units=(Unit(ref="c8_active_head", role="current_state", text="The current onboarding approach is version 3."),),
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


@pytest.mark.parametrize("twin_id", ["T1", "T2", "T6"])
def test_unresolved_twin_hedging_with_partial_status_is_tolerated(twin_id: str) -> None:
    # B3: a twin expected to resolve nothing may instead report `partial`
    # status (a hedge) without being punished for the status mismatch
    # itself. A `partial`-status anchor is the achievable hedge shape here:
    # `ambiguous` status requires *naming* concrete candidate refs
    # (`packet.ambiguity`), and for a twin with no legitimate gold of its
    # own (T1/T2/T6), naming any candidate at all is itself a false
    # activation (see test_ambiguous_hedge_is_unachievable_with_no_
    # legitimate_candidates below) -- so the tolerated hedge for these three
    # is `partial`, not `ambiguous`. The run-level ceiling on how many twins
    # may hedge at all is exercised separately below.
    twin = fixture_by_id(twin_id)
    assert twin.expected_status == "unresolved"
    packet = ActivationPacket(anchors=(Anchor(ref="something_else_entirely", title="x", kind="note", status="partial"),))
    score = score_case(packet, twin)
    assert score.observed_status == "partial"
    assert score.hedged is True
    assert score.twin_false_activation is False
    assert score.passed, score.failure_reasons


def test_ambiguous_hedge_is_unachievable_with_no_legitimate_candidates() -> None:
    # Documents the boundary above: naming any ambiguity candidate for a
    # twin with an empty own-gold is a false activation by construction
    # (the candidate is necessarily outside the empty gold set), so
    # `ambiguous` is not a "free" hedge the way `partial` is.
    t1 = fixture_by_id("T1")
    packet = ActivationPacket(ambiguity=("something_else_entirely",))
    score = score_case(packet, t1)
    assert score.observed_status == "ambiguous"
    assert score.twin_false_activation is True
    assert not score.passed


def test_t4_ambiguous_between_its_own_two_gold_candidates_is_winnable() -> None:
    # B2: T4 now carries a narrow gold of its own (the two same-first-name
    # candidates); a packet that is genuinely ambiguous between exactly
    # those two, with no role lane run (units/pointers empty), passes.
    t4 = fixture_by_id("T4")
    packet = ActivationPacket(ambiguity=t4.gold)
    score = score_case(packet, t4)
    assert score.observed_status == "ambiguous"
    assert score.status_match is True
    assert score.twin_false_activation is False
    assert score.passed, score.failure_reasons


def test_ambiguous_status_with_units_or_pointers_present_fails_no_role_lane_runs() -> None:
    # B2: "no role lane runs" is a general ambiguous-status contract
    # property, not twin-specific -- C7 (a case) is exactly as bound by it
    # as T4 (a twin).
    c7 = fixture_by_id("C7")
    packet = ActivationPacket(
        ambiguity=("c7_hub_feature", "c7_hub_market"),
        units=(Unit(ref="c7_hub_feature", role="hub_context", text="The feature hub."),),
    )
    score = score_case(packet, c7)
    assert not score.passed
    assert any("no role lane runs" in reason for reason in score.failure_reasons)


def test_twin_false_activation_via_units_only_with_no_matching_anchor() -> None:
    # B1: the reviewer's reproduction -- a twin injects a paired case's gold
    # purely through `units` (no anchor at all) -- must still be caught. The
    # pre-correction-round `_resolved_refs`-only definition missed this
    # entirely because there was no resolved anchor to find.
    t1 = fixture_by_id("T1")
    c1 = fixture_by_id("C1")
    packet = ActivationPacket(units=(Unit(ref=c1.gold[0], role="note", text="leaked via a unit only"),))
    score = score_case(packet, t1)
    assert score.twin_false_activation is True
    assert not score.passed


def test_c8_ancestor_surfaced_via_pointer_only_is_still_poison() -> None:
    # B1: a poison ref surfaced only through `pointers` (never as an anchor
    # or unit) must still count as a false activation / poison hit -- the
    # broadened mentioned-set covers this channel too.
    c8 = fixture_by_id("C8")
    ancestor = c8.poison[0]
    packet = ActivationPacket(
        anchors=(Anchor(ref="c8_active_head", title="head", kind="note", status="resolved"),),
        units=(Unit(ref="c8_active_head", role="current_state", text="The current onboarding approach is version 3."),),
        pointers=(Pointer(ref=ancestor, title="ancestor", why="see also"),),
    )
    score = score_case(packet, c8)
    assert score.poison_hit == 1
    assert not score.passed


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
    assert manifest.logical_corpus_digest == MANIFEST["logical_corpus_digest"]


def test_legacy_three_digest_manifest_is_void() -> None:
    legacy = dict(MANIFEST)
    legacy.pop("logical_corpus_digest")

    with pytest.raises(ManifestVoidError, match="logical_corpus_digest"):
        validate_manifest(legacy)


# -- mechanism removal: the audit exits red with no packets supplied -----


def test_mechanism_removal_every_positive_case_fails_with_no_packets_supplied() -> None:
    # M2: exercise the documented kill-switch shape explicitly (every fixture
    # gets DISABLED_PACKET), not "no packet at all" -- an absent packet now
    # means "blocked" (the run never attempted this case), a different,
    # separately-tested condition (see test_run_audit_marks_a_missing_packet_
    # as_blocked_and_voids_the_verdict below).
    manifest = validate_manifest(MANIFEST)
    packets = {fixture.case_id: DISABLED_PACKET for fixture in FIXTURES}
    report = run_audit(packets, manifest=manifest)
    assert audit_passed(report) is False
    assert report.blocked_count == 0
    positive_cases = [f for f in FIXTURES if f.expected_status != "unresolved"]
    failing_case_ids = {score.case_id for score in report.per_case if not score.passed}
    assert {f.case_id for f in positive_cases} <= failing_case_ids


def test_run_audit_marks_a_missing_packet_as_blocked_and_voids_the_verdict() -> None:
    # M2: a case_id absent from `packets` is "blocked", never silently
    # substituted with DISABLED_PACKET -- "nobody ran this case" must stay
    # distinguishable from "the compiler was deliberately disabled".
    manifest = validate_manifest(MANIFEST)
    packets = {"C1": _good_c1_packet()}
    report = run_audit(packets, manifest=manifest)
    scores_by_id = {score.case_id: score for score in report.per_case}
    assert scores_by_id["C1"].blocked is False
    assert scores_by_id["C2"].blocked is True
    assert scores_by_id["C2"].passed is False
    assert report.blocked_count == len(FIXTURES) - 1
    assert report.verdict == "no_verdict"
    assert audit_passed(report) is False


def test_mechanism_removal_disabled_packet_is_the_documented_kill_switch_shape() -> None:
    assert DISABLED_PACKET.abstained is True
    assert DISABLED_PACKET.abstention_reason == "disabled"


# -- report shape: no aggregate field, every metric carries its dual -----

# M7: a structural ALLOWLIST of the exact keys report_to_dict is contracted to
# emit, replacing the prior denylist (`_FORBIDDEN_AGGREGATE_KEYS`) -- a
# denylist only catches a forbidden key spelled the way the denylist expects;
# an allowlist catches literally anything undocumented, aggregate-shaped or
# not.
_TOP_LEVEL_ALLOWED_KEYS = {
    "manifest",
    "verdict",
    "fixtures_run",
    "fixtures_total",
    "blocked",
    "hedged_twins",
    "packet_size_tokens",
    "working_set_latency_ms",
    "end_to_end_latency_ms",
    "c9_padding_robustness",
    "per_case",
}
_PER_CASE_ALLOWED_KEYS = {
    "case_id",
    "is_twin",
    "expected_status",
    "observed_status",
    "status_match",
    "gold",
    "poison",
    "twin_false_activation",
    "hedged",
    "precision",
    "must_include_missing",
    "must_exclude_present",
    "packet_chars",
    "packet_tokens",
    "latency_ms",
    "working_set_ms",
    "by_anchor_kind",
    "blocked",
    "passed",
    "failure_reasons",
}
_DUAL_KEYS = {"hit", "total"}


def test_report_top_level_keys_match_the_allowlist_exactly() -> None:
    manifest = validate_manifest(MANIFEST)
    report = run_audit({fixture.case_id: DISABLED_PACKET for fixture in FIXTURES}, manifest=manifest)
    payload = report_to_dict(report)
    assert set(payload) == _TOP_LEVEL_ALLOWED_KEYS


def test_report_per_case_keys_match_the_allowlist_exactly() -> None:
    manifest = validate_manifest(MANIFEST)
    packets = {"C1": _good_c1_packet()}
    report = run_audit(packets, manifest=manifest)
    payload = report_to_dict(report)
    assert payload["manifest"]["logical_corpus_digest"] == MANIFEST["logical_corpus_digest"]
    for case in payload["per_case"]:
        assert set(case) == _PER_CASE_ALLOWED_KEYS
        assert set(case["gold"]) == _DUAL_KEYS
        assert set(case["poison"]) == _DUAL_KEYS
        for kind_row in case["by_anchor_kind"]:
            assert set(kind_row["gold"]) == _DUAL_KEYS
            assert set(kind_row["poison"]) == _DUAL_KEYS


def test_report_is_no_verdict_when_a_case_has_no_packet_at_all() -> None:
    # M2: replaces the old (incorrect) expectation that an absent packet
    # still counted as full, reviewed coverage.
    manifest = validate_manifest(MANIFEST)
    packets = {"C1": _good_c1_packet()}
    report = run_audit(packets, manifest=manifest)
    payload = report_to_dict(report)
    assert payload["fixtures_total"] == len(FIXTURES)
    assert payload["verdict"] == "no_verdict"
    assert payload["blocked"] == {"count": len(FIXTURES) - 1, "total": len(FIXTURES)}


# -- M1: precision floor over all resolved anchors, not just poison --------


def test_precision_floor_fails_a_packet_padded_with_irrelevant_resolved_anchors() -> None:
    # The reviewer's own reproduction: dozens of resolved anchors that are
    # not on the curated poison list at all still must not buy a clean pass
    # -- a poison-only precision figure would miss this entirely.
    c1 = fixture_by_id("C1")
    anchors = tuple(Anchor(ref=key, title=key, kind="note", status="resolved") for key in c1.gold)
    anchors += tuple(
        Anchor(ref=f"irrelevant_{i}", title=f"irrelevant {i}", kind="note", status="resolved") for i in range(60)
    )
    units = (
        Unit(ref="c1_weekly_limit_insight", role="historical_pattern", text="Usage hits the weekly limit most weeks."),
        Unit(ref="c1_capacity_ceilings_pattern", role="capacity_constraint", text="A capacity ceiling pattern recurs."),
    )
    score = score_case(ActivationPacket(anchors=anchors, units=units), c1)
    assert score.precision is not None and score.precision < PRECISION_FLOOR
    assert not score.passed
    assert any("precision" in reason for reason in score.failure_reasons)


def test_precision_is_none_when_nothing_is_resolved() -> None:
    score = score_case(ActivationPacket(abstained=True), fixture_by_id("C6"))
    assert score.precision is None


def test_precision_denominator_includes_unit_and_pointer_refs_not_just_anchors() -> None:
    # BLOCKER-adjacent residue (round two): 60 junk *units* (never touching
    # an anchor at all) padding a packet with 2 correctly resolved gold
    # anchors was invisible to the old resolved-anchors-only denominator.
    c1 = fixture_by_id("C1")
    anchors = tuple(Anchor(ref=key, title=key, kind="note", status="resolved") for key in c1.gold[:2])
    units = tuple(Unit(ref=f"junk_unit_{i}", role="note", text=f"junk unit {i}") for i in range(60))
    score = score_case(ActivationPacket(anchors=anchors, units=units), c1)
    assert score.precision is not None and score.precision < PRECISION_FLOOR
    assert not score.passed
    assert any("precision" in reason for reason in score.failure_reasons)


def test_credited_superseded_ancestor_is_excluded_from_the_precision_denominator() -> None:
    # A deliberately, transparently marked supersession is correct compiler
    # behaviour, not irrelevant padding, and must not dilute precision.
    c8 = fixture_by_id("C8")
    ancestor = c8.poison[0]
    anchors = (
        Anchor(ref="c8_active_head", title="head", kind="note", status="resolved"),
        Anchor(ref=ancestor, title="ancestor", kind="note", status="resolved"),
    )
    units = (
        Unit(ref="c8_active_head", role="current_state", text="The current onboarding approach is version 3."),
        Unit(
            ref=ancestor,
            role="current_state",
            text="An earlier onboarding approach, since retired.",
            lifecycle="superseded",
            provenance={"superseded_by": "c8_active_head"},
        ),
    )
    score = score_case(ActivationPacket(anchors=anchors, units=units), c8)
    assert score.precision == 1.0


# -- B3: the hedging ceiling is a run-level bound, not a per-case one -------


def test_more_than_one_hedged_twin_fails_the_run() -> None:
    manifest = validate_manifest(MANIFEST)
    packets = dict.fromkeys([f.case_id for f in FIXTURES if f.case_id not in ("T1", "T2")], DISABLED_PACKET)
    packets["T1"] = ActivationPacket(anchors=(Anchor(ref="x", title="x", kind="note", status="partial"),))
    packets["T2"] = ActivationPacket(anchors=(Anchor(ref="y", title="y", kind="note", status="partial"),))
    report = run_audit(packets, manifest=manifest)
    payload = report_to_dict(report)
    assert payload["hedged_twins"]["count"] > HEDGED_TWINS_CEILING
    assert audit_passed(report) is False


def test_hedged_twins_dual_is_reported() -> None:
    manifest = validate_manifest(MANIFEST)
    packets = {fixture.case_id: DISABLED_PACKET for fixture in FIXTURES}
    report = run_audit(packets, manifest=manifest)
    payload = report_to_dict(report)
    assert set(payload["hedged_twins"]) == {"count", "total"}
    assert payload["hedged_twins"]["count"] == 0


# -- M1: token-size and latency thresholds are wired into audit_passed -----


def test_audit_passed_fails_when_p95_token_size_exceeds_the_ceiling() -> None:
    from membench.utility.context_activation import TOKEN_P95_CEILING

    manifest = validate_manifest(MANIFEST)
    huge_text = "x" * 20000  # comfortably over the 1,500-token p95 ceiling
    packets = {fixture.case_id: DISABLED_PACKET for fixture in FIXTURES}
    packets["C1"] = ActivationPacket(
        anchors=tuple(Anchor(ref=key, title=key, kind="note", status="resolved") for key in fixture_by_id("C1").gold),
        units=(
            Unit(ref="c1_weekly_limit_insight", role="historical_pattern", text="Usage hits the weekly limit most weeks. " + huge_text),
            Unit(ref="c1_capacity_ceilings_pattern", role="capacity_constraint", text="A capacity ceiling pattern recurs."),
        ),
    )
    report = run_audit(packets, manifest=manifest)
    distribution = token_size_distribution(report.per_case)
    assert distribution["p95"] is not None and distribution["p95"] > TOKEN_P95_CEILING
    assert audit_passed(report) is False


def test_audit_passed_fails_when_end_to_end_latency_exceeds_the_measured_baseline() -> None:
    from epistemic.corpora.context_activation import MEASURED_LATENCY_MS

    manifest = validate_manifest(MANIFEST)
    good = _good_c1_packet()
    slow = dataclasses.replace(good, latency_ms=MEASURED_LATENCY_MS["p95_ms"] + 1)
    packets = {fixture.case_id: DISABLED_PACKET for fixture in FIXTURES}
    packets["C1"] = slow
    report = run_audit(packets, manifest=manifest)
    assert audit_passed(report) is False


# -- N1 (round two): C9-vs-C2 padding-robustness comparison, tree-bound ----


def _grill_packet(*, distractor_count: int | None) -> ActivationPacket:
    c9 = fixture_by_id("C9")  # C9 shares C2's own gold/poison
    anchors = tuple(Anchor(ref=key, title=key, kind="note", status="resolved") for key in c9.gold)
    units = (
        Unit(ref="c2_grill_equipment_page", role="equipment_profile", text="A two-zone gas grill."),
        Unit(ref="c2_cooking_method_insight", role="method_insight", text="Indirect heat works best, cooking method."),
    )
    return ActivationPacket(anchors=anchors, units=units, distractor_count=distractor_count)


def test_c9_padding_robustness_passes_when_padding_changes_nothing() -> None:
    padded_score = score_case(_grill_packet(distractor_count=200), fixture_by_id("C9"))
    base_score = score_case(_grill_packet(distractor_count=0), fixture_by_id("C2"))
    result = score_padding_robustness(padded_score, base_score)
    assert result.passed, result.reasons
    assert result.recall_padded == result.recall_base == 1.0
    assert result.precision_padded == 1.0


def test_c9_padding_robustness_fails_when_padding_drops_recall() -> None:
    c9 = fixture_by_id("C9")
    padded_score = score_case(
        ActivationPacket(
            anchors=(Anchor(ref=c9.gold[0], title="x", kind="note", status="resolved"),), distractor_count=200
        ),
        c9,
    )
    base_score = score_case(_grill_packet(distractor_count=0), fixture_by_id("C2"))
    result = score_padding_robustness(padded_score, base_score)
    assert not result.passed
    assert any("recall" in reason for reason in result.reasons)


def test_c9_padding_robustness_fails_below_the_precision_floor() -> None:
    c9 = fixture_by_id("C9")
    anchors = tuple(Anchor(ref=key, title=key, kind="note", status="resolved") for key in c9.gold)
    anchors += tuple(
        Anchor(ref=f"distractor_{i}", title=f"distractor {i}", kind="note", status="resolved") for i in range(10)
    )
    padded_score = score_case(ActivationPacket(anchors=anchors, distractor_count=200), c9)
    base_anchors = tuple(Anchor(ref=key, title=key, kind="note", status="resolved") for key in c9.gold)
    base_score = score_case(ActivationPacket(anchors=base_anchors, distractor_count=0), fixture_by_id("C2"))
    result = score_padding_robustness(padded_score, base_score)
    assert not result.passed
    assert any("precision" in reason for reason in result.reasons)


def test_padding_comparison_refuses_packets_that_share_one_tree() -> None:
    # Spec scenario "Padding comparison refuses packets from one tree": two
    # IDENTICAL packets recording the same distractor_count must fail --
    # before this fix, identical precision/recall made this pass vacuously,
    # proving nothing about padding at all.
    same_packet = _grill_packet(distractor_count=200)
    padded_score = score_case(same_packet, fixture_by_id("C9"))
    base_score = score_case(same_packet, fixture_by_id("C2"))
    result = score_padding_robustness(padded_score, base_score)
    assert not result.passed
    assert any("same corpus tree" in reason for reason in result.reasons)


def test_padding_comparison_refuses_none_vs_none_naming_the_missing_tree() -> None:
    # MAJOR-2 (micro round): the old `is not None and ...` guard let both
    # sides recording no tree at all pass vacuously -- a missing tree is the
    # violation "every fixture and packet SHALL record the corpus tree"
    # exists to catch, not a third acceptable state.
    padded_score = score_case(_grill_packet(distractor_count=None), fixture_by_id("C9"))
    base_score = score_case(_grill_packet(distractor_count=None), fixture_by_id("C2"))
    result = score_padding_robustness(padded_score, base_score)
    assert not result.passed
    assert any("None" in reason for reason in result.reasons)


def test_padding_comparison_refuses_two_hundred_vs_none() -> None:
    padded_score = score_case(_grill_packet(distractor_count=200), fixture_by_id("C9"))
    base_score = score_case(_grill_packet(distractor_count=None), fixture_by_id("C2"))
    result = score_padding_robustness(padded_score, base_score)
    assert not result.passed
    assert any("base packet must record" in reason for reason in result.reasons)


def test_report_includes_c9_padding_robustness_when_both_scores_are_present() -> None:
    manifest = validate_manifest(MANIFEST)
    packets = {fixture.case_id: DISABLED_PACKET for fixture in FIXTURES}
    packets["C9"] = _grill_packet(distractor_count=200)
    packets["C2"] = _grill_packet(distractor_count=0)
    report = run_audit(packets, manifest=manifest)
    payload = report_to_dict(report)
    assert payload["c9_padding_robustness"] is not None
    assert payload["c9_padding_robustness"]["passed"] is True


def test_report_c9_padding_robustness_is_none_when_either_score_is_absent() -> None:
    manifest = validate_manifest(MANIFEST)
    report = run_audit({}, manifest=manifest, fixtures=tuple(f for f in FIXTURES if f.case_id != "C2"))
    payload = report_to_dict(report)
    assert payload["c9_padding_robustness"] is None


# -- M4/M5: Pointer and CurrentStateEntry.statement round-trip -------------


def test_packet_from_dict_round_trips_pointers_and_current_state_statement() -> None:
    data = {
        "pointers": [{"ref": "p1", "title": "P1", "why": "see also"}],
        "current_state": [{"anchor": "a1", "source": "records", "as_of": "2026-09-10", "statement": "status: unavailable"}],
    }
    packet = packet_from_dict(data)
    assert packet.pointers[0] == Pointer(ref="p1", title="P1", why="see also")
    assert packet.current_state[0].statement == "status: unavailable"


def test_statement_max_chars_mirrors_the_compiler_constant() -> None:
    # M5 field-name correction: `exomem.working_set_state.STATEMENT_MAX_CHARS`
    # is 200 (that package is not a dependency of this benchmark worktree,
    # so the value is pinned here rather than imported).
    assert STATEMENT_MAX_CHARS == 200


def test_current_state_statement_text_feeds_char_and_token_counts() -> None:
    # M4/M5: the *statement* text (never the `source` label, which only
    # names where it came from) is what actually gets injected and counted.
    packet = ActivationPacket(
        current_state=(CurrentStateEntry(anchor="a", source="records", as_of="2026-09-10", statement="status: unavailable"),)
    )
    assert injected_char_count(packet) == len("status: unavailable")
    score = score_case(packet, fixture_by_id("C5"))
    assert score.packet_chars == len("status: unavailable")
    assert score.packet_tokens > 0


def test_current_state_statement_over_200_chars_fails_as_a_packet_contract_violation() -> None:
    # Spec: "a current-state statement of at most 200 characters, a longer
    # one failing the case as a packet-contract violation" -- never silently
    # truncated in packet_from_dict, which would hide the violation.
    data = {
        "anchors": [{"ref": "c5_resource_profile", "title": "bench", "kind": "resource", "status": "resolved"}],
        "current_state": [{"anchor": "c5_records_latest_unavailable", "source": "records", "statement": "x" * 201}],
    }
    packet = packet_from_dict(data)
    assert packet.current_state[0].statement == "x" * 201  # not truncated
    score = score_case(packet, fixture_by_id("C5"))
    assert not score.passed
    assert any("packet-contract violation" in reason and "200" in reason for reason in score.failure_reasons)


def test_current_state_statement_at_exactly_200_chars_does_not_violate() -> None:
    packet = ActivationPacket(
        current_state=(CurrentStateEntry(anchor="a", source="records", statement="x" * 200),)
    )
    score = score_case(packet, fixture_by_id("C5"))
    assert not any("packet-contract violation" in reason for reason in score.failure_reasons)


def test_pointer_ref_is_covered_by_mentioned_and_precision_channels() -> None:
    c5 = fixture_by_id("C5")
    packet = ActivationPacket(
        anchors=(Anchor(ref="c5_resource_profile", title="bench", kind="resource", status="resolved"),),
        pointers=(Pointer(ref="c5_records_latest_unavailable", title="records", why="latest status"),),
        current_state=(
            CurrentStateEntry(anchor="c5_records_latest_unavailable", source="records", as_of="2026-09-10", statement="status: unavailable"),
        ),
    )
    score = score_case(packet, c5)
    assert score.gold_hit == score.gold_total == 2


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


def test_legacy_identity_only_oracle_scoring_is_unchanged() -> None:
    score = score_case(_good_c1_packet(), fixture_by_id("C1"))
    assert score.gold_hit == score.gold_total == 3
    assert score.poison_hit == 0
    assert score.precision == 1.0


@pytest.mark.parametrize(
    "mechanism",
    ("product", "product-op-activate-context", "actual-activate-context"),
)
def test_product_mechanisms_require_a_reference_binding_even_for_an_empty_subset(
    mechanism: str,
) -> None:
    manifest = validate_manifest({**MANIFEST, "mechanism": mechanism})

    with pytest.raises(ManifestVoidError, match="reference_binding"):
        run_audit({}, manifest=manifest, fixtures=())


@pytest.mark.parametrize("mechanism", ("oracle_packet", "unknown"))
def test_explicit_identity_only_mechanisms_remain_binding_optional(mechanism: str) -> None:
    manifest = validate_manifest({**MANIFEST, "mechanism": mechanism})

    report = run_audit({}, manifest=manifest, fixtures=())

    assert report.verdict == "no_verdict"


# -- multilingual rows (step 4, T9) ----------------------------------------
#
# The multilingual set is scored by its own module from a PAIR of packets per
# turn (semantic on, semantic off); nothing above changes. Every check reads
# the packets the product served, so none of these can credit what a packet
# never did.

_ML_PATHS = {
    "gold": "Knowledge Base/Products/Gold.md",
    "poison": "Knowledge Base/Products/Poison.md",
    "other": "Knowledge Base/Products/Other.md",
}


def _ml_case(case_id: str):
    from epistemic.corpora.context_activation_multilingual import case_by_id

    return case_by_id(case_id)


def _ml_key_to_path(case) -> dict[str, str]:
    mapping = {key: _ML_PATHS["gold"] for key in case.gold}
    mapping.update({key: _ML_PATHS["poison"] for key in (*case.poison, *case.never_resolved)})
    return mapping


def _ml_packet(*anchors, recent=(), abstained=None, generation=None, **extra) -> dict:
    items = [{"path": path, "status": status, "evidence": list(evidence)} for path, status, evidence in anchors]
    resolved = any(item["status"] == "resolved" for item in items)
    packet = {
        "anchors": items,
        "recent_context": [{"path": path} for path in recent],
        "abstained": (not resolved) if abstained is None else abstained,
        "generation": generation or {"semantic_evidence": "ready"},
        "ambiguity": [],
        "units": [],
        "pointers": [],
        "current_state": [],
    }
    packet.update(extra)
    return packet


def test_multilingual_named_case_passes_on_band_plus_word_and_partial_off() -> None:
    from membench.utility.context_activation_multilingual import score_multilingual_case

    case = _ml_case("M1-de")
    gold = _ML_PATHS["gold"]
    row = score_multilingual_case(
        case,
        _ml_packet((gold, "resolved", ("rare_term", "vector_band"))),
        _ml_packet((gold, "partial", ("rare_term",)), generation={"semantic_evidence": "disabled"}),
        key_to_path=_ml_key_to_path(case),
    )
    assert row.passed, row.failure_reasons
    assert (row.observed_status_on, row.observed_status_off) == ("resolved", "partial")
    assert row.gold_resolved is True


def test_multilingual_status_counts_an_abstained_partial_as_partial() -> None:
    from membench.utility.context_activation_multilingual import case_status

    assert case_status(_ml_packet((_ML_PATHS["gold"], "partial", ("rare_term",)), abstained=True)) == "partial"
    assert case_status(_ml_packet(abstained=True)) == "unresolved"
    assert turn_status(packet_from_dict({"anchors": [], "abstained": True})) == "unresolved"


def test_multilingual_band_alone_resolution_fails_the_case() -> None:
    from membench.utility.context_activation_multilingual import score_multilingual_case

    case = _ml_case("M1-de")
    gold = _ML_PATHS["gold"]
    row = score_multilingual_case(
        case,
        _ml_packet((gold, "resolved", ("vector_band",))),
        _ml_packet((gold, "partial", ("rare_term",))),
        key_to_path=_ml_key_to_path(case),
    )
    assert not row.passed
    assert any("vector_band alone" in reason for reason in row.failure_reasons)


def test_multilingual_twin_resolving_anything_is_false_activation() -> None:
    from membench.utility.context_activation_multilingual import score_multilingual_case

    twin = _ml_case("N1-de")
    other = _ML_PATHS["other"]
    row = score_multilingual_case(
        twin,
        _ml_packet((other, "resolved", ("rare_term", "lexical_overlap"))),
        _ml_packet(),
        key_to_path=_ml_key_to_path(twin),
    )
    assert not row.passed
    assert any("twin false activation (on)" in reason for reason in row.failure_reasons)


def test_multilingual_menu_rank_comes_only_from_the_served_menu() -> None:
    """A promotion the packet never made cannot be credited: the gold absent
    from both menus is no rank on either arm and a gain of zero."""
    from membench.utility.context_activation_multilingual import (
        multilingual_report,
        score_multilingual_case,
    )

    case = _ml_case("M2-de")
    recent = (_ML_PATHS["other"],)
    row = score_multilingual_case(
        case,
        _ml_packet(recent=recent),
        _ml_packet(recent=recent),
        key_to_path=_ml_key_to_path(case),
    )
    assert (row.gold_menu_rank_on, row.gold_menu_rank_off) == (None, None)
    assert row.passed, row.failure_reasons
    assert multilingual_report([row], encoder={})["summary"]["menu_gain"] == 0

    promoted = score_multilingual_case(
        case,
        _ml_packet(recent=(_ML_PATHS["gold"], *recent)),
        _ml_packet(recent=recent),
        key_to_path=_ml_key_to_path(case),
    )
    assert (promoted.gold_menu_rank_on, promoted.gold_menu_rank_off) == (1, None)


def test_multilingual_poison_promoted_or_banded_is_caught() -> None:
    from membench.utility.context_activation_multilingual import (
        multilingual_report,
        score_multilingual_case,
    )

    case = _ml_case("M4-ru")
    poison = _ML_PATHS["poison"]
    recent = (_ML_PATHS["other"],)
    row = score_multilingual_case(
        case,
        _ml_packet((poison, "partial", ("vector_band",)), recent=(poison, *recent)),
        _ml_packet(recent=recent),
        key_to_path=_ml_key_to_path(case),
    )
    assert row.poison_banded == (poison,)
    assert row.poison_promoted == (poison,)
    assert not row.passed
    assert multilingual_report([row], encoder={})["summary"]["false_band_rate"] == 1.0


def test_multilingual_content_free_menu_must_match_across_arms() -> None:
    from membench.utility.context_activation_multilingual import score_multilingual_case

    case = _ml_case("M3-de")
    row = score_multilingual_case(
        case,
        _ml_packet(recent=(_ML_PATHS["gold"], _ML_PATHS["other"])),
        _ml_packet(recent=(_ML_PATHS["other"], _ML_PATHS["gold"])),
        key_to_path={},
    )
    assert not row.passed
    assert any("content-free" in reason for reason in row.failure_reasons)


def test_multilingual_degraded_packet_must_equal_the_off_packet() -> None:
    from membench.utility.context_activation_multilingual import (
        multilingual_report,
        score_multilingual_case,
    )

    case = _ml_case("M3-ru")
    off = _ml_packet(recent=(_ML_PATHS["other"],), generation={"semantic_evidence": "disabled"})
    cold = _ml_packet(recent=(_ML_PATHS["other"],), generation={"semantic_evidence": "unavailable"})
    drifted = _ml_packet((_ML_PATHS["gold"], "partial", ("rare_term",)), recent=(_ML_PATHS["other"],))
    same = score_multilingual_case(case, off, off, key_to_path={}, degraded=[cold])
    assert same.degrade_identical and same.passed
    different = score_multilingual_case(case, off, off, key_to_path={}, degraded=[drifted])
    assert not different.degrade_identical and not different.passed
    assert multilingual_report([same, different], encoder={})["summary"]["degrade_identical"] is False


def test_multilingual_carry_fragment_that_carries_fails() -> None:
    from membench.utility.context_activation_multilingual import score_multilingual_case

    case = _ml_case("M7-de")
    carried = _ml_packet(
        (_ML_PATHS["poison"], "retrieval_carried", ("retrieval",)),
        abstained=False,
        generation={"semantic_evidence": "ready", "carried_by": "retrieval"},
    )
    row = score_multilingual_case(case, carried, carried, key_to_path=_ml_key_to_path(case))
    assert not row.passed
    assert any("single word carried" in reason for reason in row.failure_reasons)
    assert any("poison carried" in reason for reason in row.failure_reasons)


def test_multilingual_residual_row_is_reported_never_failed() -> None:
    from membench.utility.context_activation_multilingual import score_multilingual_case

    case = _ml_case("M8-de")
    resolved = _ml_packet((_ML_PATHS["gold"], "resolved", ("rare_term", "lexical_overlap")))
    row = score_multilingual_case(case, resolved, _ml_packet(), key_to_path=_ml_key_to_path(case))
    assert row.scored is False
    assert row.passed is True
    assert row.failure_reasons, "the residual still reports what it saw"


def test_multilingual_report_takes_semantic_ms_from_the_served_timings() -> None:
    from membench.utility.context_activation_multilingual import (
        multilingual_report,
        score_multilingual_case,
    )

    case = _ml_case("M3-ja")
    timed = _ml_packet(timings={"stages": {"working_set.semantic": {"ms": 12.5}}})
    row = score_multilingual_case(case, timed, _ml_packet(), key_to_path={}, encode_ms=7.0)
    report = multilingual_report([row], encoder={"model": "m"})
    assert report["summary"]["semantic_ms"] == {"p50": 12.5, "p95": 12.5, "n": 1}
    assert report["summary"]["encode_ms"]["p95"] == 7.0
    assert report["encoder"] == {"model": "m"}
    assert set(report) == {"encoder", "arms", "per_case", "summary"}


def test_multilingual_latency_rows_are_ceil_rank_percentiles_over_every_case() -> None:
    from membench.utility.context_activation_multilingual import (
        multilingual_report,
        score_multilingual_case,
    )

    case = _ml_case("M3-ja")
    rows = [
        score_multilingual_case(
            case,
            _ml_packet(timings={"stages": {"working_set.semantic": {"ms": float(ms)}}}),
            _ml_packet(),
            key_to_path={},
        )
        for ms in (40, 10, 30, 20, 100, 50, 60, 70, 80, 90)
    ]
    semantic = multilingual_report(rows, encoder={})["summary"]["semantic_ms"]
    assert semantic == {"p50": 50.0, "p95": 100.0, "n": 10}
