"""Manifest checks for the language-bias twin fixture (`recall_multilingual_twins`).

The poison pages are digest-pinned so a content edit is a visible, reviewed
change. With the first twin of each language in `queries_multilingual.yaml`,
every language has three twins, each naming an English golden-fixture gold, and
every added poison shares a word with its query.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from epistemic.corpora import recall_multilingual, recall_multilingual_twins

from exomem import bm25

_TWINS_DIGEST = "21affdd45f610f19e984aa661c3d9125157f2d5e1c3a9ca71a64095f53ff8eff"
_FIXTURE_VAULT = Path(__file__).resolve().parent / "fixtures"


def _all_twins() -> list[dict]:
    first = [row for row in recall_multilingual.load_queries() if row["kind"] == "language_bias"]
    return first + recall_multilingual_twins.load_queries()


def test_twin_poison_pages_are_pinned_and_two_per_language() -> None:
    assert recall_multilingual_twins.corpus_digest() == _TWINS_DIGEST
    counts = Counter(page.language for page in recall_multilingual_twins.PAGES)
    assert counts == {language: 2 for language in recall_multilingual.LANGUAGES}
    assert not set(recall_multilingual_twins.PAGES_BY_KEY) & set(recall_multilingual.PAGES_BY_KEY)


def test_every_language_has_three_twins_with_an_english_gold() -> None:
    twins = _all_twins()
    assert Counter(row["language"] for row in twins) == {
        language: 3 for language in recall_multilingual.LANGUAGES
    }
    for row in twins:
        (gold,) = row["gold"]
        assert gold.startswith(recall_multilingual.FIXTURE_PREFIX), row
        assert (_FIXTURE_VAULT / gold.removeprefix(recall_multilingual.FIXTURE_PREFIX)).is_file()


def test_every_added_poison_shares_a_word_with_its_query() -> None:
    for row in recall_multilingual_twins.load_queries():
        (poison,) = row["poison"]
        page = recall_multilingual_twins.PAGES_BY_KEY[poison]
        assert page.language == row["language"]
        query_stems = set(bm25.tokenize(row["query"], query=True))
        assert query_stems & set(bm25.tokenize(f"{page.title} {page.body}")), row["query"]
