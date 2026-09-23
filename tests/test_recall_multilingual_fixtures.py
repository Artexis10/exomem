"""Manifest checks for the multilingual recall fixtures.

`recall_multilingual` (sixty pages beside the English golden fixture) and
`recall_japanese_vault` (about eighty Japanese pages built through the
product writers) are digest-pinned so a content edit is a visible, reviewed
change, and every golden row must name a page that exists.
"""

from __future__ import annotations

import re
from collections import Counter

from epistemic.corpora import recall_japanese_vault, recall_multilingual

from exomem import bm25

_MULTILINGUAL_DIGEST = "2c5a7c868d3bb1e7af6b3f00ba03b6d3a7deaa437691a18260128737eddab8a6"
_JAPANESE_DIGEST = "f6e2aec6e3241b4947beb748aa8734b9c2da01437bfe3a25e4187e1afcdb6a08"
_ASCII_LETTER = re.compile(r"[A-Za-z]")


def test_multilingual_pages_are_pinned_and_fifteen_per_language() -> None:
    assert recall_multilingual.corpus_digest() == _MULTILINGUAL_DIGEST
    counts = Counter(page.language for page in recall_multilingual.PAGES)
    assert counts == {language: 15 for language in recall_multilingual.LANGUAGES}
    assert len(recall_multilingual.PAGES_BY_KEY) == len(recall_multilingual.PAGES)


def test_multilingual_rows_name_existing_pages_and_every_kind_per_language() -> None:
    rows = recall_multilingual.load_queries()
    kinds = {(row["language"], row["kind"]) for row in rows}
    for language in recall_multilingual.LANGUAGES:
        for kind in ("same_language", "cross_language", "language_bias"):
            assert (language, kind) in kinds
    for language in ("de", "et"):
        assert (language, "without_diacritics") in kinds
    for language in ("ru", "et"):
        assert (language, "morphology") in kinds
    for row in rows:
        assert bm25.tokenize(row["query"], query=True), row["query"]
        if row["kind"] == "language_bias":
            assert row.get("poison"), row["query"]


def test_diacritic_free_rows_really_are_typed_without_diacritics() -> None:
    for row in recall_multilingual.load_queries():
        if row["kind"] == "without_diacritics" and row["language"] in {"de", "et"}:
            assert row["query"].isascii(), row["query"]


def test_japanese_vault_is_pinned_japanese_and_about_eighty_pages() -> None:
    assert recall_japanese_vault.corpus_digest() == _JAPANESE_DIGEST
    notes = (*recall_japanese_vault.TARGET_NOTES, *recall_japanese_vault.background_notes())
    pages = len(notes) + len(recall_japanese_vault.TARGET_ENTITIES) + 2
    assert pages == 81
    for note in notes:
        assert not _ASCII_LETTER.search(note.title + note.observation), note.key
    for entity in recall_japanese_vault.TARGET_ENTITIES:
        assert not _ASCII_LETTER.search(entity.name + entity.summary), entity.key


def test_japanese_queries_name_targets_and_yield_tokens() -> None:
    targets = {note.key for note in recall_japanese_vault.TARGET_NOTES} | {
        entity.key for entity in recall_japanese_vault.TARGET_ENTITIES
    }
    queries = recall_japanese_vault.QUERIES
    assert len(queries) == 23
    assert {query.gold for query in queries} <= targets
    for query in queries:
        assert not _ASCII_LETTER.search(query.query), query.query
        assert bm25.tokenize(query.query, query=True), query.query


def test_japanese_particle_queries_are_all_hiragana() -> None:
    hiragana = range(0x3040, 0x30A0)
    for query in recall_japanese_vault.PARTICLE_QUERIES:
        assert all(ord(character) in hiragana for character in query), query
