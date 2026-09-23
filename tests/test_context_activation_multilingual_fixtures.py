"""The multilingual sibling fixture set for context activation (step 4, T1).

`context-activation-multilingual-v1` is a SIBLING of the digest-pinned
English set: its own cases, its own corpus, its own digest. These tests pin
the manifest (every design row, a same-language negative twin for every
positive, pool placement of the menu cases) and the built corpus (anchors
enough for the band's population floor, explicit non-burst recency, native
script pages, no turn leaked into a page, the privacy gate). They import only
the corpus and the product's own index: no model, no scorer.
"""

from __future__ import annotations

import dataclasses
import unicodedata
from pathlib import Path

import pytest
from epistemic.corpora import context_activation as english_set
from epistemic.corpora.context_activation_multilingual import (
    CASE_IDS,
    CASES,
    CORPUS_ID,
    ENTITY_NAMES,
    FIXTURE_SET_ID,
    KEY_LANGUAGES,
    LEAK_CHECKED_TURNS,
    POOL_ORDER,
    POSITIVE_KINDS,
    RECENT_MENU_SIZE,
    RECENT_POOL_SIZE,
    FixtureError,
    MultilingualCase,
    assert_manifest_consistent,
    build_corpus,
    case_by_id,
    fixture_set_digest,
)

from exomem import working_set, working_set_index
from exomem.public_artifact_privacy import assert_public_artifacts_clean

pytestmark = pytest.mark.timeout(600)

#: The English set's digest at the revision this sibling was authored against.
#: The multilingual set must never be folded into it or edit it.
ENGLISH_SET_DIGEST = "a49d85f49b18c2ce8f0349933ed01ceb4fb5ca176dca066700c9c2f93605426f"

#: The design table's rows (STEP4 §9.1), by id.
DESIGN_ROWS = {
    "M1-de", "M1-ru", "N1-de", "N1-ru",
    "M2-en", "M2-de", "M2-ru", "M2-ja", "N2-en", "N2-de", "N2-ru", "N2-ja",
    "M3-de", "M3-ru", "M3-ja", "M3-et",
    "M4-ru",
    "M5-ja", "N5-ja",
    "M6-de",
    "M7-de", "M7-et",
    "M8-de",
}


def _script_of(text: str) -> set[str]:
    scripts: set[str] = set()
    for character in text:
        if not character.isalpha():
            continue
        name = unicodedata.name(character, "")
        if name.startswith("CYRILLIC"):
            scripts.add("cyrillic")
        elif name.startswith(("CJK UNIFIED", "HIRAGANA", "KATAKANA")):
            scripts.add("cjk")
        elif name.startswith("LATIN"):
            scripts.add("latin")
    return scripts


# --------------------------------------------------------------------------- #
# The manifest (pure)
# --------------------------------------------------------------------------- #


def test_the_set_is_a_sibling_and_leaves_the_english_set_byte_identical() -> None:
    assert FIXTURE_SET_ID == "context-activation-multilingual-v1"
    assert FIXTURE_SET_ID != english_set.FIXTURE_SET_ID
    assert CORPUS_ID != english_set.CORPUS_ID
    assert set(CASE_IDS).isdisjoint(english_set.CASE_IDS + english_set.TWIN_IDS)
    assert english_set.fixture_set_digest() == ENGLISH_SET_DIGEST
    assert len(english_set.FIXTURES) == 18


def test_every_design_row_is_present_exactly_once() -> None:
    assert set(CASE_IDS) == DESIGN_ROWS
    assert len(CASE_IDS) == len(set(CASE_IDS)) == len(CASES)


def test_manifest_is_internally_consistent() -> None:
    assert_manifest_consistent()


def test_turns_cover_english_german_russian_and_japanese() -> None:
    assert {case.language for case in CASES} >= {"en", "de", "ru", "ja"}


def test_every_positive_has_a_same_language_negative_twin() -> None:
    positives = [case for case in CASES if case.kind in POSITIVE_KINDS]
    assert positives
    for positive in positives:
        twins = [case for case in CASES if case.pairs_with == positive.case_id]
        assert twins, f"{positive.case_id} has no negative twin"
        for twin in twins:
            assert twin.language == positive.language
            assert set(twin.gold).isdisjoint(positive.gold)


def test_language_bias_case_carries_its_same_language_poison_built_in() -> None:
    case = case_by_id("M4-ru")
    assert case.poison
    assert any(KEY_LANGUAGES[key] == "ru" for key in case.poison)
    assert all(KEY_LANGUAGES[key] == "en" for key in case.gold)


def test_turn_scripts_match_their_language() -> None:
    for case in CASES:
        scripts = _script_of(case.turn + case.prior_turn)
        if case.language == "ru":
            assert "cyrillic" in scripts, case.case_id
        elif case.language == "ja":
            assert "cjk" in scripts, case.case_id
        else:
            assert "latin" in scripts and "cyrillic" not in scripts, case.case_id
    german = " ".join(case.turn for case in CASES if case.language == "de")
    assert any(character in german for character in "äöüß")


def test_digest_is_stable_and_every_field_moves_it() -> None:
    original = fixture_set_digest()
    assert fixture_set_digest() == original
    case = case_by_id("M6-de")
    for change in (
        {"gold": ("an_extra_key",)},
        {"poison": ("an_extra_key",)},
        {"never_resolved": ()},
        {"expected_status_off": "partial"},
        {"menu": "gold_first"},
        {"prior_turn": "Etwas anderes."},
        {"language": "et"},
    ):
        edited = dataclasses.replace(case, **change)
        edited_set = tuple(edited if item.case_id == case.case_id else item for item in CASES)
        assert fixture_set_digest(edited_set) != original, change


def test_manifest_refuses_a_missing_or_foreign_language_twin() -> None:
    without_twin = tuple(case for case in CASES if case.case_id != "N5-ja")
    with pytest.raises(FixtureError):
        assert_manifest_consistent(without_twin)
    foreign = tuple(
        dataclasses.replace(case, language="de") if case.case_id == "N2-ja" else case
        for case in CASES
    )
    with pytest.raises(FixtureError):
        assert_manifest_consistent(foreign)
    with pytest.raises(FixtureError):
        assert_manifest_consistent((*CASES, CASES[0]))


def test_a_case_refuses_an_unknown_kind_status_or_menu() -> None:
    case = case_by_id("M1-de")
    with pytest.raises(FixtureError):
        dataclasses.replace(case, kind="something_else")
    with pytest.raises(FixtureError):
        dataclasses.replace(case, expected_status_off="carried")
    with pytest.raises(FixtureError):
        dataclasses.replace(case, menu="first")
    with pytest.raises(FixtureError):
        dataclasses.replace(case, turn="  ")


def test_named_rare_turns_name_one_anchor_and_twins_name_another() -> None:
    for case_id, twin_id in (("M1-de", "N1-de"), ("M1-ru", "N1-ru")):
        case, twin = case_by_id(case_id), case_by_id(twin_id)
        assert case.rare_name and case.rare_name in case.turn
        assert twin.rare_name and twin.rare_name in twin.turn
        assert case.rare_name not in twin.turn
        assert twin.rare_name != case.rare_name
        assert set(case.gold) <= set(twin.never_resolved)
        assert case.expected_status_on == "resolved"
        assert case.expected_status_off == "partial"
        assert twin.expected_status_on == twin.expected_status_off == "partial"


def test_cjk_twin_holds_the_name_only_inside_a_longer_compound() -> None:
    case, twin = case_by_id("M5-ja"), case_by_id("N5-ja")
    name = case.rare_name
    assert len(name) >= 2 and all(unicodedata.name(ch).startswith("CJK UNIFIED") for ch in name)
    assert name in case.turn
    start = twin.turn.index(name)
    following = twin.turn[start + len(name)]
    assert unicodedata.name(following).startswith("CJK UNIFIED"), "the twin must extend the name into a compound"
    assert set(case.gold) <= set(twin.never_resolved)
    assert (case.expected_status_on, case.expected_status_off) == ("resolved", "partial")
    assert (twin.expected_status_on, twin.expected_status_off) == ("partial", "partial")


def test_menu_cases_sit_in_the_pool_below_the_recency_menu() -> None:
    assert len(POOL_ORDER) == RECENT_POOL_SIZE == 24
    assert RECENT_MENU_SIZE == working_set.RECENT_CONTEXT_MAX_ENTRIES
    menu_cases = [case for case in CASES if case.menu == "gold_first"]
    assert {case.case_id for case in menu_cases} == {"M2-en", "M2-de", "M2-ru", "M2-ja", "M4-ru"}
    for case in menu_cases:
        for key in (*case.gold, *case.poison):
            rank = POOL_ORDER.index(key)
            assert RECENT_MENU_SIZE <= rank < RECENT_POOL_SIZE, (case.case_id, key, rank)
        assert case.expected_status_on == case.expected_status_off == "unresolved"
    # Neither order is built in: some golds are newer than their poison, some older.
    newer = [
        POOL_ORDER.index(case.gold[0]) < min(POOL_ORDER.index(key) for key in case.poison)
        for case in menu_cases
    ]
    assert any(newer) and not all(newer)


def test_menu_twins_poison_their_cases_gold() -> None:
    for case in CASES:
        if case.kind != "cross_language_menu":
            continue
        twin = next(item for item in CASES if item.pairs_with == case.case_id)
        assert twin.gold == ()
        assert set(case.gold) <= set(twin.poison)
        assert twin.expected_status_on == twin.expected_status_off == "unresolved"


def test_top_of_the_pool_is_unrelated_to_every_case() -> None:
    referenced = {
        key for case in CASES for key in (*case.gold, *case.poison, *case.never_resolved)
    }
    assert referenced.isdisjoint(POOL_ORDER[:RECENT_MENU_SIZE])


def test_content_free_turns_expect_the_recency_menu_and_nothing_else() -> None:
    content_free = [case for case in CASES if case.kind == "content_free"]
    assert {case.language for case in content_free} >= {"de", "ru", "ja"}
    for case in content_free:
        assert case.gold == case.poison == case.never_resolved == ()
        assert case.menu == "recency"
        assert case.expected_status_on == case.expected_status_off == "unresolved"


def test_continuity_switch_starts_from_a_turn_that_names_its_anchor() -> None:
    case = case_by_id("M6-de")
    assert case.prior_turn and case.rare_name in case.prior_turn
    assert case.rare_name not in case.turn
    assert case.never_resolved and case.gold == ()


def test_residual_row_is_reported_not_scored() -> None:
    residual = [case for case in CASES if not case.scored]
    assert [case.case_id for case in residual] == ["M8-de"]
    assert all(case.scored for case in CASES if case.case_id != "M8-de")


def test_every_referenced_key_has_a_declared_language() -> None:
    referenced = {
        key for case in CASES for key in (*case.gold, *case.poison, *case.never_resolved)
    }
    assert referenced <= set(KEY_LANGUAGES)
    assert set(POOL_ORDER) <= set(KEY_LANGUAGES)


# --------------------------------------------------------------------------- #
# The built corpus
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("multilingual-corpus")
    return root, build_corpus(root)


def test_build_maps_every_page_key_to_a_real_page(corpus) -> None:
    root, manifest = corpus
    assert manifest.corpus_id == CORPUS_ID
    assert manifest.fixture_set_digest == fixture_set_digest()
    assert set(manifest.key_to_path) == set(KEY_LANGUAGES)
    for relative in manifest.key_to_path.values():
        assert relative.startswith("Knowledge Base/")
        assert (root / relative).is_file(), relative


def test_the_activation_index_holds_enough_anchors_for_the_band_floor(corpus) -> None:
    root, manifest = corpus
    index = working_set_index.WorkingSetIndex(root)
    index.rebuild()
    anchors = {anchor.path: anchor for anchor in index.anchors()}

    assert len(anchors) >= 50, len(anchors)
    for case_id in ("M1-de", "M1-ru", "M5-ja"):
        for key in case_by_id(case_id).gold:
            assert manifest.key_to_path[key] in anchors, (case_id, key)
    for key in case_by_id("M6-de").never_resolved:
        assert manifest.key_to_path[key] in anchors
    for case in CASES:
        if case.menu == "gold_first":
            for key in case.gold:
                assert manifest.key_to_path[key] not in anchors, (case.case_id, key)
    assert manifest.key_to_path[case_by_id("M4-ru").poison[0]] in anchors
    anchor_languages = {
        KEY_LANGUAGES[key] for key, path in manifest.key_to_path.items() if path in anchors
    }
    assert anchor_languages >= {"en", "de", "ru", "ja"}
    assert sum(1 for key, path in manifest.key_to_path.items() if path in anchors and KEY_LANGUAGES[key] == "en") > len(anchors) // 2


def test_recency_is_explicit_ordered_and_never_a_burst(corpus) -> None:
    root, manifest = corpus
    assert set(manifest.recency) == set(manifest.key_to_path.values())
    ordered = sorted(manifest.recency.items(), key=lambda item: -item[1])
    path_to_key = {path: key for key, path in manifest.key_to_path.items()}
    assert tuple(path_to_key[path] for path, _ in ordered[:RECENT_POOL_SIZE]) == POOL_ORDER
    times = [mtime for _, mtime in ordered]
    gaps = [newer - older for newer, older in zip(times, times[1:], strict=False)]
    assert min(gaps) > working_set.HOT_PROFILE_BURST_NS
    for relative, mtime in manifest.recency.items():
        assert (root / relative).stat().st_mtime_ns == mtime, relative
    others = [
        path.stat().st_mtime_ns
        for path in (root / "Knowledge Base").rglob("*.md")
        if path.relative_to(root).as_posix() not in manifest.recency
    ]
    assert others and max(others) < min(times), "unlisted files must be older than every fixture page"


def test_every_non_english_language_has_native_script_pages(corpus) -> None:
    root, manifest = corpus
    expected = {"de": "latin", "ru": "cyrillic", "ja": "cjk"}
    for language, script in expected.items():
        keys = [key for key, value in KEY_LANGUAGES.items() if value == language]
        assert len(keys) >= 3, language
        for key in keys:
            text = (root / manifest.key_to_path[key]).read_text(encoding="utf-8")
            assert script in _script_of(text), key
    german_text = " ".join(
        (root / manifest.key_to_path[key]).read_text(encoding="utf-8")
        for key, value in KEY_LANGUAGES.items()
        if value == "de"
    )
    assert any(character in german_text for character in "äöüß")


def test_no_fixture_turn_leaks_into_the_corpus(corpus) -> None:
    root, _manifest = corpus
    assert english_set.find_verbatim_leaks(root, turns=LEAK_CHECKED_TURNS) == ()
    assert english_set.find_normalized_leaks(root, turns=LEAK_CHECKED_TURNS) == ()
    exempt = {case.turn for case in CASES} - set(LEAK_CHECKED_TURNS)
    assert exempt == {case.turn for case in CASES if case.kind == "carry_fragment"}


def test_a_carry_fragment_word_is_on_exactly_one_page_its_poison(corpus) -> None:
    root, manifest = corpus
    pages = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8").casefold()
        for path in (root / "Knowledge Base").rglob("*.md")
    }
    for case in CASES:
        if case.kind != "carry_fragment":
            continue
        word = case.rare_name.casefold()
        assert not word.isascii(), "the word must be one the ASCII catalogue splits"
        holders = [relative for relative, text in pages.items() if word in text]
        assert holders == [manifest.key_to_path[case.poison[0]]], (case.case_id, holders)
    residual = case_by_id("M8-de")
    phrase = residual.rare_name.casefold()
    holders = [relative for relative, text in pages.items() if phrase in text]
    assert len(holders) == 1, holders


def test_entities_carry_only_declared_invented_names(corpus) -> None:
    root, manifest = corpus
    index = working_set_index.WorkingSetIndex(root)
    index.rebuild()
    entity_titles = {anchor.title for anchor in index.anchors() if anchor.kind == "entity"}
    assert entity_titles == set(ENTITY_NAMES)


def test_privacy_gate_passes_for_the_module_and_every_built_page(corpus) -> None:
    root, _manifest = corpus
    import epistemic.corpora.context_activation_multilingual as module

    pages = sorted((root / "Knowledge Base").rglob("*.md"))
    assert pages
    assert_public_artifacts_clean([Path(module.__file__), *pages])


def test_build_is_deterministic(corpus, tmp_path: Path) -> None:
    _root, manifest = corpus
    again = build_corpus(tmp_path / "again")
    assert again.key_to_path == manifest.key_to_path
    assert again.recency == manifest.recency
    assert again.fixture_set_digest == manifest.fixture_set_digest


def test_multilingual_case_is_a_frozen_value() -> None:
    case = case_by_id("M2-de")
    assert isinstance(case, MultilingualCase)
    with pytest.raises(dataclasses.FrozenInstanceError):
        case.turn = "x"  # type: ignore[misc]
