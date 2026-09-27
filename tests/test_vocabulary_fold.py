import pytest

from exomem.vocabulary_fold import EXCEPTIONS, fold_term

#: Pairs of different words that a fold must keep apart. Each merge would
#: silently join two concepts in retrieval, claims and tag maintenance.
MUST_NOT_MERGE = [
    ("training", "trains"),
    ("marketing", "markets"),
    ("accounting", "accounts"),
    ("monitoring", "monitors"),
    ("recording", "records"),
    ("modeling", "models"),
    ("hosting", "hosts"),
    ("reasoning", "reasons"),
    ("engineering", "engineers"),
    ("parking", "parks"),
    ("writing", "writ"),
    ("opening", "open"),
    ("embedded", "embeddings"),
    ("building", "build"),
    ("meeting", "meet"),
    ("dogfooding", "dogfood"),
    ("planned", "plan"),
    ("news", "new"),
]

#: Spellings of one term that must share a key.
MUST_MERGE = [
    (["failure", "failures", "Failures"], "failure"),
    (["policy", "policies"], "policy"),
    (["match", "matches", "Matches"], "match"),
    (["cache", "caches"], "cache"),
    (["index", "indexes"], "index"),
    (["status", "statuses"], "status"),
    (["x_y", "x-y", "X Y", "x  y", "x_ y", "x--y", "-x-y-"], "x-y"),
    (["failure_mode", "failure-modes", "Failure Modes"], "failure-mode"),
    (["building", "buildings"], "building"),
    (["ﬁle", "file", "ＦＩＬＥ"], "file"),
]


@pytest.mark.parametrize(("left", "right"), MUST_NOT_MERGE)
def test_different_words_never_share_a_key(left, right):
    assert fold_term(left) != fold_term(right)


@pytest.mark.parametrize(("variants", "key"), MUST_MERGE)
def test_plural_and_separator_variants_share_one_key(variants, key):
    assert {fold_term(variant) for variant in variants} == {key}


@pytest.mark.parametrize(
    "word",
    ["dogfooding", "planning", "tested", "stopped", "embedded", "caching", "running"],
)
def test_progressive_and_past_forms_are_never_folded(word):
    assert fold_term(word) == word


@pytest.mark.parametrize(
    "word",
    ["news", "series", "analysis", "bus", "gas", "status", "process", "windows", "https",
     "always", "perhaps"],
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
        ("classes", "class"),
        ("campus", "campus"),
        ("basis", "basis"),
        ("coaches", "coach"),
        ("niches", "niche"),
        ("sizes", "size"),
        ("processes", "process"),
        ("statuses", "status"),
        ("v2", "v2"),
        ("python3", "python3"),
        ("builds", "build"),
    ],
)
def test_the_fold_is_conservative(word, folded):
    assert fold_term(word) == folded


def test_only_the_final_segment_is_inflected():
    assert fold_term("tests-results") == "tests-result"
    assert fold_term("news-feeds") == "news-feed"


def test_empty_and_separator_only_input():
    assert fold_term("") == ""
    assert fold_term("  _ ") == ""
    assert fold_term("---") == ""


@pytest.mark.parametrize(
    "word",
    [
        "always", "perhaps", "statuses", "focuses", "caches", "sizes", "meetings",
        "building", "planning", "summaries", "overviews", "drafts", "stopped",
        "regressions", "dogfooding", "Context  Compiler", "snake_case term",
        "machine-learning-models", "https", "a", "", "  ", "classes", "coaches",
        "entries", "dressings", "Straße", "ǰobs", "ℌello", "ﬁles",
        "x_-_y", "policies", "indexes", "failures",
        *[word for pair in MUST_NOT_MERGE for word in pair],
        *[word for variants, _ in MUST_MERGE for word in variants],
    ],
)
def test_the_fold_is_idempotent(word: str) -> None:
    """A stored key folds to itself, so comparing it again never drifts."""
    once = fold_term(word)
    assert fold_term(once) == once
