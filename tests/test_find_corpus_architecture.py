"""Architecture checks for the find corpus-access split."""

from __future__ import annotations

from exomem import find, find_corpus


def test_find_reexports_corpus_cache_and_helpers() -> None:
    assert find.FrontmatterCache is find_corpus.FrontmatterCache
    assert find._CACHE is find_corpus.CACHE
    assert find._walk_md is find_corpus.walk_md
    assert find._parse_page is find_corpus.parse_page
    assert find._passes_filters is find_corpus.passes_filters
