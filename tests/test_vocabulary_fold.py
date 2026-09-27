import pytest

from exomem.vocabulary_fold import EXCEPTIONS, fold_term


@pytest.mark.parametrize(
    ("variants", "key"),
    [
        (["dogfood", "dogfooding", "Dogfooding", "dogfooded"], "dogfood"),
        (["failure", "failures", "Failures"], "failure"),
        (["x_y", "x-y", "X Y", "x  y", "x_ y"], "x-y"),
        (["policy", "policies"], "policy"),
        (["match", "matches"], "match"),
        (["cache", "caches"], "cache"),
        (["index", "indexes"], "index"),
        (["plan", "plans", "planning", "planned"], "plan"),
        (["embed", "embedding", "embedded", "embeddings"], "embed"),
        (["test", "tests", "testing", "tested"], "test"),
        (["failure_mode", "failure-modes", "Failure Modes"], "failure-mode"),
    ],
)
def test_inflection_and_separator_variants_share_one_key(variants, key):
    assert {fold_term(variant) for variant in variants} == {key}


@pytest.mark.parametrize(
    "word",
    ["news", "series", "analysis", "bus", "gas", "status", "process", "windows", "https"],
)
def test_meaning_bearing_forms_are_left_alone(word):
    assert word in EXCEPTIONS or len(word) < 4
    assert fold_term(word) == word


@pytest.mark.parametrize(
    ("word", "folded"),
    [
        ("ops", "ops"),  # under four letters
        ("cats", "cat"),  # four letters is enough for a plural
        ("class", "class"),
        ("campus", "campus"),
        ("basis", "basis"),
        ("need", "need"),
        ("speed", "speed"),
        ("string", "string"),
        ("adding", "adding"),  # stem under four letters
        ("building", "building"),
        ("buildings", "building"),
        ("evening", "evening"),
        ("processes", "process"),
        ("statuses", "status"),
        ("v2", "v2"),
        ("python3", "python3"),
    ],
)
def test_the_fold_is_conservative(word, folded):
    assert fold_term(word) == folded


def test_only_the_final_segment_is_inflected():
    assert fold_term("tests-results") == "tests-result"
    assert fold_term("news-feeds") == "news-feed"


def test_fold_is_idempotent_on_its_own_output():
    for word in ["dogfooding", "failures", "policies", "x_y", "embeddings", "matches"]:
        once = fold_term(word)
        assert fold_term(once) == once


def test_empty_and_separator_only_input():
    assert fold_term("") == ""
    assert fold_term("  _ ") == ""


@pytest.mark.parametrize(
    "word",
    [
        "always", "perhaps", "statuses", "focuses", "caches", "sizes", "meetings",
        "building", "planning", "summaries", "overviews", "drafts", "stopped",
        "regressions", "dogfooding", "Context  Compiler", "snake_case term",
        "machine-learning-models", "https", "a", "", "  ",
    ],
)
def test_the_fold_is_idempotent(word: str) -> None:
    """A stored key folds to itself, so comparing it again never drifts."""
    once = fold_term(word)
    assert fold_term(once) == once


def test_adverbs_ending_in_s_are_not_plurals() -> None:
    assert fold_term("always") == "always"
    assert fold_term("perhaps") == "perhaps"
