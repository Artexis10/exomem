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

    def count_parses(path: Path, mtime: float, vault_root: Path, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse_page(path, mtime, vault_root, **kwargs)

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

    def count_parses(path: Path, mtime: float, vault_root: Path, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse_page(path, mtime, vault_root, **kwargs)

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


def test_size_change_with_preserved_mtime_invalidates_entry(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    _page(page, "Original title")
    cache = find_corpus.FrontmatterCache()

    first = cache.get(page, tmp_path)
    old_stat = page.stat()
    _page(page, "A much longer updated title")
    os.utime(page, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    assert page.stat().st_mtime_ns == old_stat.st_mtime_ns
    updated = cache.get(page, tmp_path)

    assert first is not updated
    assert updated is not None
    assert updated.title == "A much longer updated title"


def test_same_size_content_change_with_unchanged_stat_invalidates_entry(
    tmp_path: Path, monkeypatch
) -> None:
    page = tmp_path / "page.md"
    _page(page, "Original title")
    cache = find_corpus.FrontmatterCache()

    first = cache.get(page, tmp_path)
    old_stat = page.stat()
    old_size = len(page.read_bytes())
    _page(page, "Modified title")
    assert len(page.read_bytes()) == old_size

    original_stat = Path.stat

    def frozen_stat(path: Path, *args, **kwargs):
        if path == page:
            return old_stat
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", frozen_stat)

    updated = cache.get(page, tmp_path)

    assert first is not updated
    assert updated is not None
    assert updated.title == "Modified title"


def test_byte_budget_evicts_lru_without_retaining_oversized_pages(tmp_path, monkeypatch):
    # Count-only caching retains arbitrarily large bodies and normalized copies.
    monkeypatch.setenv("EXOMEM_PAGE_CACHE_SIZE", "10")
    monkeypatch.setenv("EXOMEM_PAGE_CACHE_BYTES", "40000")
    cache = find_corpus.FrontmatterCache()
    paths = [tmp_path / (name + ".md") for name in ("first", "second", "third")]
    for path in paths:
        path.write_text("---\ntype: research-note\n---\n# Note\n" + "UPPER " * 800)
    for path in (paths[0], paths[1], paths[0], paths[2]):
        page = cache.get(path, tmp_path)
        assert page.body_norm == page.body.strip().lower()
    assert list(cache.entries) == [paths[0], paths[2]]
    oversized = tmp_path / "oversized.md"
    oversized.write_text("---\ntitle: Large metadata\nextra: " + "x" * 60000 + "\n---\nshort")
    assert cache.get(oversized, tmp_path).title == "Large metadata"
    assert list(cache.entries) == [paths[0], paths[2]]


def test_byte_charge_is_retired_on_replacement_and_invalidation(tmp_path, monkeypatch):
    # A stale charge must not evict an unrelated page after a rewrite or removal.
    monkeypatch.setenv("EXOMEM_PAGE_CACHE_BYTES", "18000")
    cache = find_corpus.FrontmatterCache()
    first, second = tmp_path / "first.md", tmp_path / "second.md"
    first.write_text("LARGE " * 700)
    cache.get(first, tmp_path)
    first.write_text("small")
    cache.get(first, tmp_path)
    second.write_text("NEXT " * 700)
    cache.get(second, tmp_path)
    assert set(cache.entries) == {first, second}
    cache.invalidate_paths(tmp_path, [second.name])
    third = tmp_path / "third.md"
    third.write_text("LAST " * 700)
    cache.get(third, tmp_path)
    assert set(cache.entries) == {first, third}
    first.write_text("CAP " * 15000)
    assert cache.get(first, tmp_path).body == "CAP " * 15000
    assert set(cache.entries) == {third}


def test_budgeted_page_does_not_retain_lazy_word_sets(tmp_path, monkeypatch):
    import weakref

    monkeypatch.setenv("EXOMEM_PAGE_CACHE_BYTES", "60000")
    path = tmp_path / "words.md"
    path.write_text(" ".join("token" + str(n) for n in range(2000)))
    page = find_corpus.FrontmatterCache().get(path, tmp_path)
    words = page.stem_set
    retained = weakref.ref(words)
    assert "token123" in words
    del words
    assert retained() is None


def test_simultaneous_misses_do_not_leave_ghost_byte_charges(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setenv("EXOMEM_PAGE_CACHE_BYTES", "15000")
    path = tmp_path / "shared.md"
    path.write_text("WORK " * 500)
    cache = find_corpus.FrontmatterCache()
    original = find_corpus.parse_page
    barrier = threading.Barrier(2)
    count_lock = threading.Lock()
    calls = 0

    def simultaneous_parse(*args, **kwargs):
        nonlocal calls
        page = original(*args, **kwargs)
        with count_lock:
            calls += 1
            concurrent = calls <= 2
        if concurrent:
            barrier.wait(timeout=5)
        return page

    monkeypatch.setattr(find_corpus, "parse_page", simultaneous_parse)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pages = list(pool.map(lambda _: cache.get(path, tmp_path), range(2)))
    assert all(page.body == "WORK " * 500 for page in pages)
    assert cache.get(path, tmp_path) is cache.get(path, tmp_path)
