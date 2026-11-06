"""Bounded-LRU behavior for the parsed-page frontmatter cache."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

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


def test_container_absence_survives_body_churn_but_not_path_or_scope_invalidation(vault, monkeypatch):
    """A body LRU must not discard the compact fact that the next ordinary-page read depends on."""
    from test_governance_egress import _external

    from exomem import document_containers, reserved_paths
    from exomem import vault as vault_module
    from exomem.governance import egress, principal

    monkeypatch.setenv("EXOMEM_PAGE_CACHE_SIZE", "1")
    monkeypatch.setattr(vault_module, "GENERATION_RACY_WINDOW_NS", 0)
    cache = find_corpus.FrontmatterCache()
    monkeypatch.setattr(find_corpus, "CACHE", cache)
    paths = [vault / f"ordinary-{index}.md" for index in range(3)]
    for path in paths:
        _page(path, path.stem)
        assert document_containers.parent_binding_for_path(vault, path.name) is None
        cache.get(path, vault)
    assert len(cache.entries) == 1 and cache.cache_status()["classification_facts"] == 3
    if reserved_paths.inspect_generic_version(vault, paths[0].name) is None:
        pytest.skip("requires a descriptor-qualified native ext filesystem")
    with monkeypatch.context() as patch:
        read = reserved_paths.read_generic_bytes
        def unexpected(root, path, *args, **kwargs):
            if path == paths[0].name:
                pytest.fail("an aged fact must not retain or reread the body")
            return read(root, path, *args, **kwargs)
        patch.setattr(reserved_paths, "read_generic_bytes", unexpected)
        with principal.request_scope(_external()):
            basis = egress.current_source_release_basis(vault)
            with egress.source_release_proof(vault, basis) as capture:
                assert document_containers.parent_binding_for_path(vault, paths[0].name) is None
            assert capture.finish() is not None
    assert cache.stale_paths(vault, [paths[0].name], {paths[0].name: "0" * 64}) == (paths[0].name,)
    assert cache.invalidate_paths(vault, [paths[0].name]) == 1
    assert cache.cache_status()["classification_facts"] == 2
    cache.invalidate_scope(vault, "vault")
    assert cache.cache_status()["classification_facts"] == 0


def test_container_fact_admission_shares_the_byte_cap_and_evicts_bodies_first(tmp_path, monkeypatch):
    """Compact metadata must be finite, including map growth, while a small body cap cannot thrash its facts."""
    from test_released_corpus_memory import _independent_bytes

    from exomem import document_containers

    monkeypatch.setenv("EXOMEM_PAGE_CACHE_BYTES", "6000")
    cache = find_corpus.FrontmatterCache()
    monkeypatch.setattr(find_corpus, "CACHE", cache)
    body = tmp_path / "body.md"
    body.write_text("Readable body. " * 60)
    assert cache.get(body, tmp_path) is not None and cache.entries
    for index in range(10):
        path = tmp_path / f"ordinary-{index}.md"
        _page(path, path.stem)
        assert document_containers.parent_binding_for_path(tmp_path, path.name) is None
        status = cache.cache_status()
        assert status["classification_bytes"] >= _independent_bytes([cache._container_absence])
        assert status["classification_bytes"] + cache._retained_bytes <= 6000
    assert not cache.entries and 1 < cache.cache_status()["classification_facts"] < 10
    monkeypatch.setenv("EXOMEM_PAGE_CACHE_BYTES", "0")
    assert not cache.container_absence_enabled()
    assert not cache.entries and cache.cache_status()["classification_facts"] == 0
    monkeypatch.delenv("EXOMEM_PAGE_CACHE_BYTES")
    monkeypatch.setattr(find_corpus, "_page_cache_bytes", lambda: None)
    monkeypatch.setattr(find_corpus, "_DEFAULT_PAGE_CACHE_BYTES", 3000)
    for index in range(10):
        assert document_containers.parent_binding_for_path(tmp_path, f"ordinary-{index}.md") is None
        assert cache.cache_status()["classification_bytes"] <= 3000
    assert 0 < cache.cache_status()["classification_facts"] < 10
    cache.release()
    assert cache.cache_status()["classification_bytes"] == 0


@pytest.mark.parametrize("trusted", [False, True])
def test_container_fact_cannot_hide_same_size_claim_edits_with_restored_mtime(tmp_path, monkeypatch, trusted):
    """Both qualified metadata and full-digest fallback must notice changed claims when mtime stays equal."""
    from exomem import document_containers, held_fs, reserved_paths
    from exomem import vault as vault_module

    monkeypatch.setattr(vault_module, "GENERATION_RACY_WINDOW_NS", 0)
    cache = find_corpus.FrontmatterCache()
    monkeypatch.setattr(find_corpus, "CACHE", cache)
    path = tmp_path / "ordinary.md"
    path.write_text("---\ncontainer_ignore: null\n---\nUnchanged body.\n")
    with held_fs.acquire(tmp_path).require() as filesystem, filesystem.parent(".").require() as parent:
        with filesystem.file(parent, path.name).require() as file:
            file_type = type(file)
    if not trusted:
        monkeypatch.setattr(file_type, "version_witness_supported", lambda self: False)
        assert reserved_paths.inspect_generic_version(tmp_path, path.name) is None
    elif reserved_paths.inspect_generic_version(tmp_path, path.name) is None:
        pytest.skip("requires a descriptor-qualified native ext filesystem")
    assert document_containers.parent_binding_for_path(tmp_path, path.name) is None
    with monkeypatch.context() as patch:
        patch.setattr(document_containers, "parse_frontmatter", lambda *args, **kwargs: pytest.fail("matching full bytes already prove the negative fact"))
        assert document_containers.parent_binding_for_path(tmp_path, path.name) is None
    before = path.stat()
    path.write_bytes(path.read_bytes().replace(b"container_ignore", b"container_member"))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size
    with pytest.raises(ValueError, match="provenance"):
        document_containers.parent_binding_for_path(tmp_path, path.name)
    assert cache.cache_status()["classification_facts"] == 0


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux held read admission")
def test_container_metadata_fact_cannot_bypass_a_current_read_open_refusal(tmp_path, monkeypatch):
    """A sandbox can admit O_PATH metadata while refusing the ordinary read open required by canonical classification."""
    from exomem import _held_fs_posix, document_containers, reserved_paths
    from exomem import vault as vault_module

    monkeypatch.setattr(vault_module, "GENERATION_RACY_WINDOW_NS", 0)
    monkeypatch.setattr(find_corpus, "CACHE", find_corpus.FrontmatterCache())
    path = tmp_path / "ordinary.md"
    _page(path, "Ordinary")
    assert document_containers.parent_binding_for_path(tmp_path, path.name) is None
    if reserved_paths.inspect_generic_version(tmp_path, path.name) is None:
        pytest.skip("requires a descriptor-qualified native ext filesystem")
    opened = _held_fs_posix._open_relative
    def refused(parent, name, flags, *args):
        if name == path.name and not flags & os.O_PATH:
            raise PermissionError("current read opens are refused")
        return opened(parent, name, flags, *args)
    monkeypatch.setattr(_held_fs_posix, "_open_relative", refused)
    assert reserved_paths.inspect_generic_version(tmp_path, path.name) is None
    with pytest.raises(reserved_paths.ReservedPathLeafError):
        document_containers.parent_binding_for_path(tmp_path, path.name)


def test_container_metadata_hit_cannot_follow_an_exchanged_parent(tmp_path, monkeypatch):
    """The identical file behind a new parent symlink must not inherit the old generic admission."""
    from exomem import document_containers, reserved_paths
    from exomem import vault as vault_module

    monkeypatch.setattr(vault_module, "GENERATION_RACY_WINDOW_NS", 0)
    monkeypatch.setattr(find_corpus, "CACHE", find_corpus.FrontmatterCache())
    parent = tmp_path / "notes"
    parent.mkdir()
    path = parent / "ordinary.md"
    _page(path, "Ordinary")
    assert document_containers.parent_binding_for_path(tmp_path, "notes/ordinary.md") is None
    parent.rename(tmp_path / "moved")
    parent.symlink_to(tmp_path / "moved", target_is_directory=True)
    assert reserved_paths.inspect_generic_version(tmp_path, "notes/ordinary.md") is None
    with pytest.raises(reserved_paths.ReservedPathLeafError):
        document_containers.parent_binding_for_path(tmp_path, "notes/ordinary.md")


def test_generic_read_witness_cannot_describe_a_write_after_the_consumed_image(tmp_path, monkeypatch):
    """A separate before/after pathname probe would certify the wrong full image across this read boundary."""
    from exomem import held_fs, reserved_paths
    from exomem import vault as vault_module

    monkeypatch.setattr(vault_module, "GENERATION_RACY_WINDOW_NS", 0)
    path = tmp_path / "ordinary.md"
    path.write_bytes(b"first image")
    with held_fs.acquire(tmp_path).require() as filesystem:
        filesystem_type = type(filesystem)
    read = filesystem_type.read
    def changed(self, file, **kwargs):
        result = read(self, file, **kwargs)
        path.write_bytes(b"other image")
        return result
    monkeypatch.setattr(filesystem_type, "read", changed)
    snapshot = reserved_paths.read_generic_bytes(tmp_path, path.name, version_witness=True)
    assert snapshot.data == b"first image"
    assert snapshot.version_witness.version is None


def test_unavailable_metadata_close_keeps_the_canonical_bytes_without_a_version(tmp_path, monkeypatch):
    """A sandbox may allow the canonical reader while refusing the optional O_PATH closing probe."""
    from exomem import document_containers, held_fs, reserved_paths, runtime_resources
    from exomem import vault as vault_module

    monkeypatch.setattr(vault_module, "GENERATION_RACY_WINDOW_NS", 0)
    monkeypatch.setattr(find_corpus, "CACHE", find_corpus.FrontmatterCache())
    path = tmp_path / "ordinary.md"
    _page(path, "Ordinary")
    if reserved_paths.inspect_generic_version(tmp_path, path.name) is None:
        pytest.skip("requires a descriptor-qualified native ext filesystem")
    with held_fs.acquire(tmp_path).require() as filesystem:
        filesystem_type = type(filesystem)
    parent = filesystem_type.parent
    def unavailable(self, relative, **kwargs):
        if kwargs.get("access") == "identity":
            return held_fs.HeldResult(error=held_fs.HeldFsError("CAPABILITY_UNAVAILABLE", "metadata closing is unavailable"))
        return parent(self, relative, **kwargs)
    monkeypatch.setattr(filesystem_type, "parent", unavailable)
    snapshot = reserved_paths.read_generic_bytes(tmp_path, path.name, version_witness=True)
    assert snapshot.data == path.read_bytes() and snapshot.version_witness.version is None
    assert document_containers.parent_binding_for_path(tmp_path, path.name) is None
    for failure in (runtime_resources.PreparationBudgetExceeded, TimeoutError):
        def exhausted(self, relative, *, failure=failure, **kwargs):
            if kwargs.get("access") == "identity":
                raise failure("the request ended during metadata validation")
            return parent(self, relative, **kwargs)
        monkeypatch.setattr(filesystem_type, "parent", exhausted)
        with pytest.raises(failure):
            reserved_paths.inspect_generic_version(tmp_path, path.name)
        with pytest.raises(failure):
            reserved_paths.read_generic_bytes(tmp_path, path.name, version_witness=True)
