"""Bounded-LRU behavior for the parsed-page frontmatter cache."""

from __future__ import annotations

import os
from pathlib import Path

from exomem import find_corpus


def _page(path: Path, title: str) -> None:
    path.write_text(f"---\ntype: research-note\n---\n# {title}\n", encoding="utf-8")


def test_cache_respects_bound_and_evicts_least_recently_used(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("EXOMEM_PAGE_CACHE_SIZE", "2")
    cache = find_corpus.FrontmatterCache()
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    third = tmp_path / "third.md"
    for page in (first, second, third):
        _page(page, page.stem)

    cache.get(first, tmp_path)
    cache.get(second, tmp_path)
    cache.get(first, tmp_path)
    cache.get(third, tmp_path)

    assert len(cache.entries) == 2
    assert list(cache.entries) == [first, third]


def test_cache_hit_within_bound_does_not_parse_again(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_PAGE_CACHE_SIZE", "2")
    page = tmp_path / "page.md"
    _page(page, "First title")
    cache = find_corpus.FrontmatterCache()
    parse_calls = 0
    original_parse_page = find_corpus.parse_page

    def count_parses(path: Path, mtime: float, vault_root: Path):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse_page(path, mtime, vault_root)

    monkeypatch.setattr(find_corpus, "parse_page", count_parses)

    first = cache.get(page, tmp_path)
    second = cache.get(page, tmp_path)

    assert first is second
    assert parse_calls == 1


def test_mtime_change_invalidates_entry_within_bound(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_PAGE_CACHE_SIZE", "2")
    page = tmp_path / "page.md"
    _page(page, "Original title")
    cache = find_corpus.FrontmatterCache()
    parse_calls = 0
    original_parse_page = find_corpus.parse_page

    def count_parses(path: Path, mtime: float, vault_root: Path):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse_page(path, mtime, vault_root)

    monkeypatch.setattr(find_corpus, "parse_page", count_parses)

    first = cache.get(page, tmp_path)
    old_stat = page.stat()
    _page(page, "Updated title")
    os.utime(page, (old_stat.st_atime, old_stat.st_mtime + 1))
    updated = cache.get(page, tmp_path)

    assert first is not updated
    assert updated is not None
    assert updated.title == "Updated title"
    assert parse_calls == 2
