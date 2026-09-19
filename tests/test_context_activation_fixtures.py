"""Red-first tests for the context-activation fixture manifest and corpus.

Task 1.1 (openspec/changes/add-context-activation-benchmark/tasks.md): the
fixture manifest lists nine cases and nine twins with gold, poison, roles,
must-include, must-exclude, expected status and a reminder turn; digests are
stable; editing a gold list changes the digest; no fixture turn appears
verbatim in any corpus page.
"""

from __future__ import annotations

import dataclasses

import pytest
from epistemic.corpora.context_activation import (
    BASE_DISTRACTOR_COUNT,
    CASE_IDS,
    FIXTURES,
    MEASURED_LATENCY_MS,
    TWIN_IDS,
    FixtureError,
    _corpus_hash,
    _logical_corpus_hash,
    assert_manifest_consistent,
    build_corpus,
    cases,
    find_fact_leaks_outside_gold_poison_pages,
    find_normalized_leaks,
    find_verbatim_leaks,
    fixture_by_id,
    fixture_set_digest,
    latest_record,
    twins,
)

REQUIRED_FIELDS = (
    "case_id",
    "pairs_with",
    "turn",
    "reminder_turn",
    "gold",
    "poison",
    "roles",
    "must_include",
    "must_exclude",
    "expected_status",
)


def test_manifest_lists_nine_cases_and_nine_twins() -> None:
    assert len(cases()) == 9
    assert len(twins()) == 9
    assert {f.case_id for f in cases()} == set(CASE_IDS)
    assert {f.case_id for f in twins()} == set(TWIN_IDS)


def test_manifest_is_internally_consistent() -> None:
    assert_manifest_consistent()  # must not raise


def test_every_twin_pairs_with_a_real_case() -> None:
    for twin in twins():
        assert twin.pairs_with in CASE_IDS
        fixture_by_id(twin.pairs_with)  # must not raise


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.case_id)
def test_every_fixture_carries_every_required_field(fixture: object) -> None:
    field_names = {f.name for f in dataclasses.fields(fixture)}  # type: ignore[arg-type]
    assert set(REQUIRED_FIELDS) <= field_names
    assert fixture.turn.strip()  # type: ignore[attr-defined]
    assert fixture.reminder_turn.strip()  # type: ignore[attr-defined]
    assert fixture.expected_status in ("resolved", "partial", "ambiguous", "unresolved")  # type: ignore[attr-defined]
    for name in ("gold", "poison", "roles", "must_include", "must_exclude"):
        assert isinstance(getattr(fixture, name), tuple)


def test_case_fields_are_meaningfully_exercised_not_vacuous() -> None:
    # Every field is non-trivially used somewhere in the case set, not just
    # structurally present as an always-empty tuple.
    assert any(f.gold for f in cases())
    assert any(f.poison for f in cases())
    assert any(f.roles for f in cases())
    assert any(f.must_include for f in cases())
    assert any(f.must_exclude for f in FIXTURES)


def test_digest_is_stable_across_calls() -> None:
    assert fixture_set_digest() == fixture_set_digest()


def test_editing_a_gold_list_changes_the_digest() -> None:
    original = fixture_set_digest()
    c1 = fixture_by_id("C1")
    edited = dataclasses.replace(c1, gold=(*c1.gold, "an_extra_gold_key"))
    edited_set = tuple(edited if f.case_id == "C1" else f for f in FIXTURES)
    assert fixture_set_digest(edited_set) != original


def test_unknown_case_id_refused() -> None:
    with pytest.raises(FixtureError):
        fixture_by_id("C99")


def test_partial_fixture_set_refused() -> None:
    with pytest.raises(FixtureError):
        assert_manifest_consistent(FIXTURES[:-1])  # drop one twin


def test_duplicate_case_id_refused() -> None:
    with pytest.raises(FixtureError):
        assert_manifest_consistent((*FIXTURES, FIXTURES[0]))


def test_corpus_has_no_verbatim_fixture_turns(tmp_path) -> None:
    build_corpus(tmp_path, distractor_count=5)
    assert find_verbatim_leaks(tmp_path) == ()


def test_build_corpus_maps_every_gold_and_poison_key_to_a_real_page(tmp_path) -> None:
    manifest = build_corpus(tmp_path, distractor_count=5)
    referenced_keys = {key for fixture in FIXTURES for key in (*fixture.gold, *fixture.poison)}
    assert referenced_keys <= set(manifest.key_to_path)
    for rel in manifest.key_to_path.values():
        assert (tmp_path / rel).is_file()


def test_build_corpus_is_deterministic_for_the_same_seed(tmp_path) -> None:
    first = build_corpus(tmp_path / "first", seed=5, distractor_count=5)
    second = build_corpus(tmp_path / "second", seed=5, distractor_count=5)
    assert first.logical_hash == second.logical_hash
    assert first.key_to_path == second.key_to_path


def test_logical_corpus_identity_and_state_declarations_are_deterministic_across_writer_builds(
    tmp_path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first = build_corpus(first_root, seed=5, distractor_count=0)
    second = build_corpus(second_root, seed=5, distractor_count=0)

    assert first.logical_hash == second.logical_hash
    assert first.state_sources == second.state_sources


def test_build_corpus_different_seed_changes_the_hash(tmp_path) -> None:
    first = build_corpus(tmp_path / "first", seed=5, distractor_count=5)
    second = build_corpus(tmp_path / "second", seed=6, distractor_count=5)
    assert first.logical_hash != second.logical_hash


def test_logical_hash_changes_when_key_mapping_changes_but_corpus_bytes_do_not(tmp_path) -> None:
    manifest = build_corpus(tmp_path, seed=5, distractor_count=0)
    remapped = dict(manifest.key_to_path)
    remapped["c2_grill_equipment_page"], remapped["t2_camera_gear_note"] = (
        remapped["t2_camera_gear_note"],
        remapped["c2_grill_equipment_page"],
    )

    changed = _logical_corpus_hash(
        tmp_path,
        seed=manifest.seed,
        distractor_count=manifest.distractor_count,
        key_to_path=remapped,
    )

    assert changed != manifest.logical_hash
    assert _corpus_hash(tmp_path) == manifest.corpus_hash


def test_logical_hash_changes_when_state_source_declaration_changes(tmp_path) -> None:
    manifest = build_corpus(tmp_path, seed=5, distractor_count=0)
    declarations = (
        dataclasses.replace(manifest.state_sources[0], state_field="status"),
        *manifest.state_sources[1:],
    )

    changed = _logical_corpus_hash(
        tmp_path,
        seed=manifest.seed,
        distractor_count=manifest.distractor_count,
        key_to_path=manifest.key_to_path,
        state_sources=declarations,
    )

    assert changed != manifest.logical_hash
    assert _corpus_hash(tmp_path) == manifest.corpus_hash


def test_build_corpus_default_distractor_count_is_two_hundred(tmp_path) -> None:
    manifest = build_corpus(tmp_path)
    assert manifest.distractor_count == 200
    distractor_pages = list(
        (tmp_path / "Knowledge Base" / "Evidence" / "context-activation").glob(
            "distractor-*.md"
        )
    )
    assert len(distractor_pages) == 200


def test_no_verbatim_or_normalized_leak_with_the_default_distractor_count(tmp_path) -> None:
    # M7: run the leak check at the real default (200), not the speed-only 5
    # most other tests use, and check a normalised (casefold, punctuation
    # stripped) near-verbatim match too, not just exact substring.
    build_corpus(tmp_path)
    assert find_verbatim_leaks(tmp_path) == ()
    assert find_normalized_leaks(tmp_path) == ()


def test_normalized_leak_check_catches_punctuation_and_case_variants(tmp_path) -> None:
    (tmp_path / "sneaky.md").write_text(
        "---\ntitle: Sneaky\n---\n\nI KEEP HITTING MY AI USAGE LIMITS, again, this week!!\n",
        encoding="utf-8",
    )
    leaks = find_normalized_leaks(tmp_path, turns=("I keep hitting my AI usage limits again this week.",))
    assert leaks != ()


# -- B4: every case authors a reminder-test must_include fact --------------


def test_every_case_has_a_non_empty_must_include() -> None:
    for case in cases():
        assert case.must_include, f"{case.case_id} must author at least one must_include fact"


def test_must_include_facts_are_not_verbatim_turn_words() -> None:
    # A fact is a piece of knowledge the reminder would supply, not a restated
    # fragment of the turn itself -- otherwise "reflected" would be trivially
    # true for any response that merely echoes the question.
    for case in cases():
        for fact in case.must_include:
            assert fact.lower() not in case.turn.lower(), (
                f"{case.case_id}: must_include fact {fact!r} appears verbatim in the turn"
            )


def test_c6_must_include_is_the_numeric_answer() -> None:
    c6 = fixture_by_id("C6")
    assert "9.5" in c6.must_include


def test_c7_must_include_names_the_two_competing_senses() -> None:
    c7 = fixture_by_id("C7")
    assert len(c7.must_include) >= 2


# -- B6: A5's oracle text is hand-authored per case, not derived -----------


def test_oracle_text_is_non_empty_for_every_case_except_c6() -> None:
    for case in cases():
        if case.case_id == "C6":
            assert case.oracle_text == ""
        else:
            assert case.oracle_text.strip(), f"{case.case_id} must author oracle_text"


def test_oracle_text_is_empty_for_every_twin() -> None:
    for twin in twins():
        assert twin.oracle_text == ""


# -- B2: T4 is winnable -- it carries a narrow gold of its own -------------


def test_t4_carries_a_narrow_gold_of_the_two_ambiguous_candidates() -> None:
    t4 = fixture_by_id("T4")
    assert t4.expected_status == "ambiguous"
    assert len(t4.gold) == 2
    c4 = fixture_by_id("C4")
    assert set(t4.gold).isdisjoint(c4.gold)  # never C4's own content


# -- minor: MEASURED_LATENCY_MS is honestly typed (numbers only) -----------


def test_measured_latency_ms_carries_only_numeric_fields() -> None:
    assert MEASURED_LATENCY_MS is not None
    for key, value in MEASURED_LATENCY_MS.items():
        assert isinstance(value, (int, float)), f"{key} is not numeric: {value!r}"


# -- N1 (round two): C9 = C2's turn padded, T9 = T2's turn padded, an
# ordinary negative twin again; padding robustness compares C9 to C2 --------


def test_c9_is_c2s_own_turn_and_gold_pinned_to_the_padded_tree() -> None:
    c2, c9 = fixture_by_id("C2"), fixture_by_id("C9")
    assert c9.turn == c2.turn
    assert c9.gold == c2.gold
    assert c9.poison == c2.poison
    assert c9.expected_status == "resolved"
    assert c9.distractor_count == 200


def test_t9_is_t2s_own_turn_a_real_negative_twin_pinned_to_the_padded_tree() -> None:
    t2, t9 = fixture_by_id("T2"), fixture_by_id("T9")
    assert t9.turn == t2.turn
    assert t9.gold == () == t2.gold
    assert t9.poison == t2.poison
    assert t9.expected_status == "unresolved"
    assert t9.distractor_count == 200


def test_nine_negative_twins_have_empty_gold_or_are_narrow_gold_by_id() -> None:
    from epistemic.corpora.context_activation import NARROW_GOLD_TWIN_IDS

    for twin in twins():
        if twin.case_id in NARROW_GOLD_TWIN_IDS:
            assert twin.gold
        else:
            assert twin.gold == ()


def test_every_twins_gold_is_disjoint_from_its_paired_cases_gold_except_t7() -> None:
    # T7's narrow gold is legitimately one of C7's own three ambiguous
    # candidates (resolving to it is the point, not a false activation);
    # every other twin's gold must never overlap its case's own gold.
    for twin in twins():
        case = fixture_by_id(twin.pairs_with)
        if twin.case_id == "T7":
            assert set(twin.gold) <= set(case.gold)
        else:
            assert set(twin.gold).isdisjoint(case.gold)


def test_base_distractor_count_is_zero() -> None:
    assert BASE_DISTRACTOR_COUNT == 0


def test_every_fixture_records_a_distractor_count() -> None:
    # MAJOR-2 (micro round): "every fixture and every packet SHALL record
    # the corpus tree it belongs to" -- all sixteen non-C9/T9 fixtures
    # default to the base (unpadded) tree; C9/T9 pin the padded one.
    for fixture in FIXTURES:
        assert fixture.distractor_count is not None
        if fixture.case_id in ("C9", "T9"):
            assert fixture.distractor_count == 200
        else:
            assert fixture.distractor_count == BASE_DISTRACTOR_COUNT


def test_a_fixture_with_no_distractor_count_fails_manifest_consistency() -> None:
    c1 = fixture_by_id("C1")
    broken = dataclasses.replace(c1, distractor_count=None)
    broken_set = tuple(broken if f.case_id == "C1" else f for f in FIXTURES)
    with pytest.raises(FixtureError):
        assert_manifest_consistent(broken_set)


def test_build_corpus_supports_a_base_unpadded_tree(tmp_path) -> None:
    manifest = build_corpus(tmp_path, distractor_count=BASE_DISTRACTOR_COUNT)
    assert manifest.distractor_count == 0
    assert not (tmp_path / "Knowledge Base" / "Evidence" / "context-activation").exists()


# -- N2: distractor bodies are composed from a domain word bank, never a
# fixture's own must_include/gold-phrase terms -----------------------------


def test_distractor_bodies_are_generic_not_pure_random_numbers(tmp_path) -> None:
    build_corpus(tmp_path, distractor_count=5)
    text = (
        tmp_path
        / "Knowledge Base"
        / "Evidence"
        / "context-activation"
        / "distractor-0000.md"
    ).read_text(encoding="utf-8")
    domain_words = ("smoke", "brine", "sear", "temperature", "wood", "doneness", "marinade", "thermometer")
    assert any(word in text for word in domain_words)


def test_find_fact_leaks_outside_gold_poison_pages_is_clean_on_the_full_corpus(tmp_path) -> None:
    manifest = build_corpus(tmp_path)
    assert find_fact_leaks_outside_gold_poison_pages(tmp_path, manifest.key_to_path) == ()


# -- C5: the records collection is structural, latest by observed_on -------


def test_c5_records_collection_queries_canonical_items(tmp_path) -> None:
    from exomem.commands import op_record_memory

    manifest = build_corpus(tmp_path, distractor_count=0)
    rel = manifest.key_to_path["c5_records_latest_unavailable"]
    queried = op_record_memory(
        tmp_path,
        action="query",
        collection=rel,
        columns=["observed_on", "status"],
        sort_by="observed_on",
        limit=10,
    )
    assert len(queried["rows"]) == 2
    assert {item["status"] for item in queried["rows"]} == {"available", "unavailable"}


def test_c5_latest_record_by_observed_on_is_unavailable_never_by_line_order() -> None:
    # The unavailable entry is *not* first in file order -- latest_record
    # must sort by observed_on, not trust which line happens to come first.
    items = [{"observed_on": "2026-08-02", "status": "available"}, {"observed_on": "2026-09-10", "status": "unavailable"}]
    assert latest_record(items)["status"] == "unavailable"
    assert latest_record(list(reversed(items)))["status"] == "unavailable"


def test_c5_resource_page_links_to_the_records_collection(tmp_path) -> None:
    manifest = build_corpus(tmp_path, distractor_count=0)
    rel = manifest.key_to_path["c5_resource_profile"]
    text = (tmp_path / rel).read_text(encoding="utf-8")
    assert "Knowledge Base/Records/Workshop Bench/_collection" in text


# -- C7: three hubs have distinct, pairwise-disjoint wikilink neighbourhoods -


def _wikilinks(text: str) -> set[str]:
    import re

    return set(re.findall(r"\[\[([^\]]+)\]\]", text))


def test_c7_hubs_have_non_empty_pairwise_disjoint_neighbourhoods(tmp_path) -> None:
    manifest = build_corpus(tmp_path, distractor_count=0)
    hub_keys = ("c7_hub_feature", "c7_hub_market", "c7_hub_search_ux")
    neighbourhoods = []
    for key in hub_keys:
        rel = manifest.key_to_path[key]
        text = (tmp_path / rel).read_text(encoding="utf-8")
        links = _wikilinks(text)
        assert links, f"{key} has no wikilink neighbourhood"
        neighbourhoods.append(links)
    for i in range(len(neighbourhoods)):
        for j in range(i + 1, len(neighbourhoods)):
            assert neighbourhoods[i].isdisjoint(neighbourhoods[j]), (
                f"{hub_keys[i]} and {hub_keys[j]} share a neighbour page"
            )
