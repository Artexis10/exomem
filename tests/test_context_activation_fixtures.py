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
    CASE_IDS,
    FIXTURES,
    TWIN_IDS,
    FixtureError,
    assert_manifest_consistent,
    build_corpus,
    cases,
    find_verbatim_leaks,
    fixture_by_id,
    fixture_set_digest,
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
    assert first.corpus_hash == second.corpus_hash
    assert first.key_to_path == second.key_to_path


def test_build_corpus_different_seed_changes_the_hash(tmp_path) -> None:
    first = build_corpus(tmp_path / "first", seed=5, distractor_count=5)
    second = build_corpus(tmp_path / "second", seed=6, distractor_count=5)
    assert first.corpus_hash != second.corpus_hash


def test_build_corpus_default_distractor_count_is_two_hundred(tmp_path) -> None:
    manifest = build_corpus(tmp_path)
    assert manifest.distractor_count == 200
    distractor_pages = list((tmp_path / "Evidence").glob("distractor-*.md"))
    assert len(distractor_pages) == 200
