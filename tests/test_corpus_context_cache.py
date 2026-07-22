"""Correctness of the corpus-context cache (semantic_contract).

The cache may only ever serve a context that is indistinguishable from a
fresh build. Object identity is the detector below: a cache hit returns the
same object, a rebuild returns a new one.
"""

from __future__ import annotations

import dataclasses
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import (
    activation_manifest,
    freshness,
    relation_registry,
    semantic_contract,
    semantic_language_registry,
    semantic_writes,
)
from exomem import find as find_module
from exomem.vault import WikilinkResolver

_PAGE_REL = "Knowledge Base/Notes/Insights/one.md"


def _page(*, title: str = "Page", body: str = "Body.\n") -> str:
    return f"---\ntitle: {title}\ntype: insight\nstatus: active\nproject: atlas\n---\n\n{body}"


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch: pytest.MonkeyPatch):
    # The suite-wide conftest defaults the cache OFF; this suite exists to
    # exercise it, so opt back in and start cold.
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    semantic_contract.reset_corpus_context_cache()
    freshness.clear()
    yield
    semantic_contract.reset_corpus_context_cache()
    freshness.clear()


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    notes = tmp_path / "Knowledge Base" / "Notes" / "Insights"
    notes.mkdir(parents=True)
    (notes / "one.md").write_text(_page(title="One"), encoding="utf-8")
    (notes / "two.md").write_text(_page(title="Two"), encoding="utf-8")
    return tmp_path


def test_unchanged_corpus_is_reused(vault: Path) -> None:
    first = semantic_contract.build_corpus_context(vault)
    second = semantic_contract.build_corpus_context(vault)
    assert second is first
    assert set(second.pages) == {
        "Knowledge Base/Notes/Insights/one.md",
        "Knowledge Base/Notes/Insights/two.md",
    }


def test_content_change_rebuilds(vault: Path) -> None:
    first = semantic_contract.build_corpus_context(vault)
    (vault / _PAGE_REL).write_text(
        _page(title="One", body="Entirely new body text.\n"), encoding="utf-8"
    )
    second = semantic_contract.build_corpus_context(vault)
    assert second is not first
    assert second.pages[_PAGE_REL].source_hash != first.pages[_PAGE_REL].source_hash


def test_markdown_change_reconciles_without_full_rebuild(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = semantic_contract.build_corpus_context(vault)
    (vault / _PAGE_REL).write_text(
        _page(title="One", body="Incrementally refreshed body.\n"),
        encoding="utf-8",
    )

    def fail_full_rebuild(*args, **kwargs):
        raise AssertionError("a Markdown delta must not rebuild the whole corpus")

    monkeypatch.setattr(semantic_contract, "_build_corpus_context_uncached", fail_full_rebuild)
    second = semantic_contract.build_corpus_context(vault)

    assert second is not first
    assert second.pages[_PAGE_REL].source_hash != first.pages[_PAGE_REL].source_hash
    assert second.pages[_PAGE_REL].title == "One"


def test_markdown_delete_reconciles_without_full_rebuild(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    semantic_contract.build_corpus_context(vault)
    removed = "Knowledge Base/Notes/Insights/two.md"
    (vault / removed).unlink()

    def fail_full_rebuild(*args, **kwargs):
        raise AssertionError("a Markdown deletion must not rebuild the whole corpus")

    monkeypatch.setattr(semantic_contract, "_build_corpus_context_uncached", fail_full_rebuild)
    second = semantic_contract.build_corpus_context(vault)

    assert removed not in second.pages
    assert all(entry.path != removed for entry in second.identity_census.entries)


def test_incremental_reconcile_matches_full_rebuild(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    semantic_contract.build_corpus_context(vault)
    (vault / _PAGE_REL).write_text(
        _page(title="Incremental", body="Changed with the same governed rules.\n"),
        encoding="utf-8",
    )
    incremental = semantic_contract.build_corpus_context(vault)

    semantic_contract.reset_corpus_context_cache()
    monkeypatch.setenv("EXOMEM_DISABLE_CORPUS_CACHE", "1")
    rebuilt = semantic_contract.build_corpus_context(vault)

    assert incremental.as_dict() == rebuilt.as_dict()


def test_live_event_patch_makes_hot_reads_census_free(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = semantic_contract.build_corpus_context(vault)
    pages = tuple(vault.rglob("*.md"))
    freshness.seed(
        vault,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in pages),
    )
    page = vault / _PAGE_REL
    page.write_text(_page(title="Event patched"), encoding="utf-8")
    freshness.on_files_changed(vault, changed=(page,))
    semantic_contract.on_corpus_files_changed(vault, changed=(page,))

    def fail_census(*args, **kwargs):
        raise AssertionError("a live, event-patched cache must not stat-walk the vault")

    monkeypatch.setattr(semantic_contract, "_corpus_census", fail_census)
    second = semantic_contract.build_corpus_context(vault)

    assert second is not first
    assert second.pages[_PAGE_REL].title == "Event patched"


def test_writer_preflight_self_heals_exact_page_before_delayed_event(vault: Path) -> None:
    semantic_contract.build_corpus_context(vault)
    freshness.seed(
        vault,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault.rglob("*.md")),
    )
    semantic_contract.build_corpus_context(vault)

    # Model the disk-visible window before a watcher/out-of-band publisher has
    # advanced the freshness token. Preflight already owns these exact guarded
    # bytes, so it must repair that one page locally instead of rejecting a
    # valid current state against the lagging cache.
    page = vault / _PAGE_REL
    page.write_text(_page(title="Visible before event", body="Current bytes.\n"), encoding="utf-8")
    preflight = semantic_writes.preflight_existing(
        vault,
        path=_PAGE_REL,
        after_source=_page(title="Visible before event", body="Next bytes.\n"),
        operation="observe",
    )

    assert preflight.before_corpus.pages[_PAGE_REL] == preflight.before
    assert not any(
        finding.code == "SEMANTIC_CORPUS_STATE_MISMATCH"
        for finding in preflight.contract_result.findings
    )


def test_atomic_event_publish_keeps_concurrent_page_changes(vault: Path) -> None:
    semantic_contract.build_corpus_context(vault)
    freshness.seed(
        vault,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault.rglob("*.md")),
    )
    first = vault / _PAGE_REL
    second = vault / "Knowledge Base/Notes/Insights/two.md"
    first.write_text(_page(title="First concurrent"), encoding="utf-8")
    second.write_text(_page(title="Second concurrent"), encoding="utf-8")
    start = threading.Barrier(2)

    def publish(path: Path) -> None:
        start.wait(timeout=5)
        semantic_contract.publish_corpus_files_changed(vault, changed=(path,))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish, path) for path in (first, second)]
        for future in futures:
            future.result(timeout=10)

    current = semantic_contract.build_corpus_context(vault)
    assert current.pages[_PAGE_REL].title == "First concurrent"
    assert current.pages["Knowledge Base/Notes/Insights/two.md"].title == ("Second concurrent")


def test_cold_build_absorbs_markdown_churn_instead_of_discarding_cache(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_build = semantic_contract._build_corpus_context_uncached
    builds = 0

    def edit_after_first_build(*args, **kwargs):
        nonlocal builds
        builds += 1
        context = real_build(*args, **kwargs)
        if builds == 1:
            (vault / _PAGE_REL).write_text(_page(title="Changed during build"), encoding="utf-8")
        return context

    monkeypatch.setattr(semantic_contract, "_build_corpus_context_uncached", edit_after_first_build)
    first = semantic_contract.build_corpus_context(vault)
    second = semantic_contract.build_corpus_context(vault)

    assert builds == 1
    assert first.pages[_PAGE_REL].title == "Changed during build"
    assert second is first


def test_concurrent_cold_builds_share_one_uncached_result(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_build = semantic_contract._build_corpus_context_uncached
    release_build = threading.Event()
    duplicate_entered = threading.Event()
    calls_lock = threading.Lock()
    calls = 0

    def slow_build(*args, **kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
            if calls > 1:
                duplicate_entered.set()
        assert release_build.wait(timeout=5)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(semantic_contract, "_build_corpus_context_uncached", slow_build)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(semantic_contract.build_corpus_context, vault) for _ in range(2)]
        duplicated = duplicate_entered.wait(timeout=0.5)
        (vault / _PAGE_REL).write_text(_page(title="Current during flight"), encoding="utf-8")
        release_build.set()
        results = [future.result(timeout=10) for future in futures]

    assert duplicated is False
    assert calls == 1
    assert results[0] is results[1]
    assert results[0].pages[_PAGE_REL].title == "Current during flight"


def test_cold_builds_with_different_registry_inputs_serialize_then_refresh(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_build = semantic_contract._build_corpus_context_uncached
    real_census = semantic_contract._corpus_census
    first_build_entered = threading.Event()
    current_census_seen = threading.Event()
    release_first_build = threading.Event()
    calls_lock = threading.Lock()
    calls = 0
    active = 0
    max_active = 0

    def tracked_build(*args, **kwargs):
        nonlocal active, calls, max_active
        with calls_lock:
            calls += 1
            active += 1
            max_active = max(max_active, active)
            if calls == 1:
                first_build_entered.set()
        if calls == 1:
            assert release_first_build.wait(timeout=5)
        try:
            return real_build(*args, **kwargs)
        finally:
            with calls_lock:
                active -= 1

    def observed_census(root: Path):
        census = real_census(root)
        if census is not None and any(
            entry[0] == "Knowledge Base/_Schema/relation-registry.yaml" and entry[1] == "cfg"
            for entry in census
        ):
            current_census_seen.set()
        return census

    monkeypatch.setattr(semantic_contract, "_build_corpus_context_uncached", tracked_build)
    monkeypatch.setattr(semantic_contract, "_corpus_census", observed_census)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(semantic_contract.build_corpus_context, vault)
        assert first_build_entered.wait(timeout=5)
        registry_path = vault / "Knowledge Base" / "_Schema" / "relation-registry.yaml"
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(
            "schema_version: 1\nextensions:\n  science.replicates:\n"
            "    parent: supports\n    description: Independent reproduction\n",
            encoding="utf-8",
        )
        current_hash = relation_registry.load_registry(vault).extension_hash
        second_future = pool.submit(semantic_contract.build_corpus_context, vault)
        assert current_census_seen.wait(timeout=5)
        release_first_build.set()
        first = first_future.result(timeout=10)
        second = second_future.result(timeout=10)

    assert calls == 2
    assert max_active == 1
    assert first.registry.extension_hash != current_hash
    assert second.registry.extension_hash == current_hash


def test_cold_cache_publication_serializes_with_file_events(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    semantic_contract.build_corpus_context(vault)
    freshness.seed(
        vault,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault.rglob("*.md")),
    )
    (vault / "Knowledge Base" / "_access.yaml").write_text("readonly: []\n", encoding="utf-8")
    page = vault / _PAGE_REL
    real_census = semantic_contract._corpus_census
    publish_now = threading.Event()
    page_written = threading.Event()
    publish_done = threading.Event()
    census_calls = 0

    def census_with_racing_event(root: Path):
        nonlocal census_calls
        census_calls += 1
        snapshot = real_census(root)
        if census_calls == 2:
            publish_now.set()
            assert page_written.wait(timeout=5)
            # On the unsafe implementation publication completes here and is
            # then overwritten. The fixed boundary deliberately keeps it
            # waiting until the cold context is captioned and installed.
            publish_done.wait(timeout=0.25)
        return snapshot

    def publish_edit() -> None:
        assert publish_now.wait(timeout=5)
        page.write_text(_page(title="Event after cold census"), encoding="utf-8")
        page_written.set()
        semantic_contract.publish_corpus_files_changed(vault, changed=(page,))
        publish_done.set()

    monkeypatch.setattr(semantic_contract, "_corpus_census", census_with_racing_event)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(publish_edit)
        semantic_contract.build_corpus_context(vault)
        future.result(timeout=10)

    current = semantic_contract.build_corpus_context(vault)
    assert current.pages[_PAGE_REL].title == "Event after cold census"


def test_event_delta_rejects_reparse_markdown_like_full_identity_census(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    semantic_contract.build_corpus_context(vault)
    freshness.seed(
        vault,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault.rglob("*.md")),
    )
    semantic_contract.build_corpus_context(vault)
    page = vault / _PAGE_REL
    real_lstat = Path.lstat
    fake_info = SimpleNamespace(st_mode=real_lstat(page).st_mode)

    def lstat_with_reparse(path: Path):
        return fake_info if path == page else real_lstat(path)

    monkeypatch.setattr(Path, "lstat", lstat_with_reparse)
    monkeypatch.setattr(
        semantic_contract.vault,
        "_is_reparse",
        lambda info: info is fake_info,
    )

    with pytest.raises(activation_manifest.ActivationManifestError) as raised:
        semantic_contract.publish_corpus_files_changed(vault, changed=(page,))

    assert raised.value.code == "IDENTITY_CENSUS_UNSAFE_ENTRY"
    cache_key = semantic_contract._corpus_cache_key(vault)
    assert cache_key not in semantic_contract._CORPUS_CONTEXT_CACHE

    # A later valid event cannot skip over the rejected delta and re-caption
    # the old context with a newer freshness token.
    other = vault / "Knowledge Base/Notes/Insights/two.md"
    other.write_text(_page(title="Later valid event"), encoding="utf-8")
    semantic_contract.publish_corpus_files_changed(vault, changed=(other,))
    assert cache_key not in semantic_contract._CORPUS_CONTEXT_CACHE

    def cold_oracle_required(*args, **kwargs):
        raise AssertionError("next request must use the full safety oracle")

    monkeypatch.setattr(
        semantic_contract,
        "_build_corpus_context_uncached",
        cold_oracle_required,
    )
    with pytest.raises(AssertionError, match="full safety oracle"):
        semantic_contract.build_corpus_context(vault)


def test_event_delete_rejects_missing_leaf_below_reparse_ancestor(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    semantic_contract.build_corpus_context(vault)
    freshness.seed(
        vault,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault.rglob("*.md")),
    )
    semantic_contract.build_corpus_context(vault)
    page = vault / _PAGE_REL
    page.unlink()
    unsafe_ancestor = page.parent
    real_lstat = Path.lstat
    fake_info = SimpleNamespace(st_mode=real_lstat(unsafe_ancestor).st_mode)

    def lstat_with_reparse(path: Path):
        return fake_info if path == unsafe_ancestor else real_lstat(path)

    monkeypatch.setattr(Path, "lstat", lstat_with_reparse)
    monkeypatch.setattr(
        semantic_contract.vault,
        "_is_reparse",
        lambda info: info is fake_info,
    )

    with pytest.raises(activation_manifest.ActivationManifestError) as raised:
        semantic_contract.publish_corpus_files_changed(vault, deleted=(_PAGE_REL,))

    assert raised.value.code == "IDENTITY_CENSUS_UNSAFE_ENTRY"


def test_candidate_with_stable_topology_rederives_only_its_own_facts(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = semantic_contract.build_corpus_context(vault)
    candidate = semantic_contract.build_page_state(
        vault,
        _PAGE_REL,
        _page(title="One", body="A changed semantic payload.\n"),
        relation_registry=relation_registry.load_registry(vault),
        language_registry=semantic_language_registry.load_registry(vault),
    )
    expected_pages = dict(before.pages)
    expected_pages[_PAGE_REL] = candidate
    expected = semantic_contract._context_from_state_map(
        vault,
        expected_pages,
        before.registry,
        before.identity_census.with_page(candidate),
    )
    real_derive = semantic_contract._derive_relation_facts

    def derive_candidate_only(root, states, resolver, registry, **kwargs):
        assert tuple(states) == (_PAGE_REL,)
        return real_derive(root, states, resolver, registry, **kwargs)

    monkeypatch.setattr(semantic_contract, "_derive_relation_facts", derive_candidate_only)

    actual = before.with_candidate(candidate)

    assert actual.as_dict() == expected.as_dict()


def test_corpus_entries_prime_writer_resolver_without_full_vault_build(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = semantic_contract.build_corpus_context(vault)
    freshness.seed(
        vault,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault.rglob("*.md")),
    )
    find_module.clear_cache()
    freshness.seed(
        vault,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault.rglob("*.md")),
    )

    def fail_full_build(self):
        raise AssertionError("writer resolver must be primed from corpus entries")

    monkeypatch.setattr(WikilinkResolver, "_build", fail_full_build)
    find_module.prime_resolver_from_entries(vault, context.resolver_entries)
    snapshot = find_module.writer_resolver_snapshot(vault)

    assert snapshot.full_paths == context.resolver_full_paths


def test_corpus_entries_do_not_prime_resolver_after_freshness_changes(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = semantic_contract.build_corpus_context(vault)
    freshness.seed(
        vault,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault.rglob("*.md")),
    )
    expected = freshness.triple(vault, "vault")
    assert expected is not None
    page = vault / "Knowledge Base/Notes/Insights/two.md"
    page.write_text(_page(title="Changed after preflight"), encoding="utf-8")
    freshness.on_files_changed(vault, changed=(page,))

    def fail_stale_prime(*args, **kwargs):
        raise AssertionError("stale preflight entries must not seed the resolver")

    monkeypatch.setattr(WikilinkResolver, "from_entries", fail_stale_prime)
    result = find_module.prime_resolver_from_entries(
        vault,
        context.resolver_entries,
        expected_freshness=expected,
    )

    assert result is None


def test_mtime_preserving_sync_edit_rebuilds(vault: Path) -> None:
    """The Syncthing trap: new content materialized with an OLDER mtime.

    A max-mtime freshness key would serve the stale corpus here. The census
    compares (path, size, mtime_ns) per file, so the synced file's changed
    mtime — even though it is older than every other timestamp in the vault —
    invalidates the entry.
    """
    page = vault / _PAGE_REL
    first = semantic_contract.build_corpus_context(vault)
    original = page.stat()
    original_bytes = page.read_bytes()
    replacement = original_bytes.replace(b"One", b"Uno")
    assert replacement != original_bytes
    assert len(replacement) == original.st_size
    page.write_bytes(replacement)
    hour_ns = 3_600_000_000_000
    os.utime(page, ns=(original.st_mtime_ns - hour_ns, original.st_mtime_ns - hour_ns))
    assert page.stat().st_size == original.st_size
    second = semantic_contract.build_corpus_context(vault)
    assert second is not first
    assert second.pages[_PAGE_REL].title == "Uno"


def test_added_page_rebuilds(vault: Path) -> None:
    first = semantic_contract.build_corpus_context(vault)
    (vault / "Knowledge Base" / "Notes" / "Insights" / "three.md").write_text(
        _page(title="Three"), encoding="utf-8"
    )
    second = semantic_contract.build_corpus_context(vault)
    assert second is not first
    assert "Knowledge Base/Notes/Insights/three.md" in second.pages


def test_removed_page_rebuilds(vault: Path) -> None:
    first = semantic_contract.build_corpus_context(vault)
    (vault / "Knowledge Base" / "Notes" / "Insights" / "two.md").unlink()
    second = semantic_contract.build_corpus_context(vault)
    assert second is not first
    assert "Knowledge Base/Notes/Insights/two.md" not in second.pages


def test_access_config_change_rebuilds(vault: Path) -> None:
    first = semantic_contract.build_corpus_context(vault)
    (vault / "Knowledge Base" / "_access.yaml").write_text("readonly:\n- Notes\n", encoding="utf-8")
    second = semantic_contract.build_corpus_context(vault)
    assert second is not first


def test_census_covers_non_markdown_inputs(vault: Path) -> None:
    census = semantic_contract._corpus_census(vault)
    assert census is not None
    markers = {entry[0] for entry in census if entry[1] in {"cfg", "absent"}}
    assert "Knowledge Base/_access.yaml" in markers
    assert "Knowledge Base/_Schema/relation-registry.yaml" in markers
    assert "Knowledge Base/_Schema/semantic-language-registry.yaml" in markers


def test_candidate_build_bypasses_and_does_not_pollute_cache(vault: Path) -> None:
    first = semantic_contract.build_corpus_context(vault)
    candidate = semantic_contract.build_page_state(
        vault,
        "Knowledge Base/Notes/Insights/draft.md",
        _page(title="Draft"),
        relation_registry=relation_registry.core_registry(),
        language_registry=semantic_language_registry.core_registry(),
    )
    with_candidate = semantic_contract.build_corpus_context(vault, candidate=candidate)
    assert with_candidate is not first
    assert "Knowledge Base/Notes/Insights/draft.md" in with_candidate.pages
    again = semantic_contract.build_corpus_context(vault)
    assert again is first
    assert "Knowledge Base/Notes/Insights/draft.md" not in again.pages


def test_disk_equal_registries_share_the_cache(vault: Path) -> None:
    first = semantic_contract.build_corpus_context(vault)
    registry = relation_registry.load_registry(vault)
    language = semantic_language_registry.load_registry(vault)
    second = semantic_contract.build_corpus_context(
        vault, registry=registry, language_registry=language
    )
    assert second is first


def test_synthetic_registry_bypasses_cache(vault: Path) -> None:
    first = semantic_contract.build_corpus_context(vault)
    core = relation_registry.load_registry(vault)
    synthetic = dataclasses.replace(core, extension_hash="0" * 64)
    second = semantic_contract.build_corpus_context(vault, registry=synthetic)
    assert second is not first


def test_live_event_cache_rejects_synthetic_language_registry(vault: Path) -> None:
    first = semantic_contract.build_corpus_context(vault)
    freshness.seed(
        vault,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault.rglob("*.md")),
    )
    assert semantic_contract.build_corpus_context(vault) is first
    disk_language = semantic_language_registry.load_registry(vault)
    synthetic = dataclasses.replace(disk_language, content_hash="0" * 64)

    second = semantic_contract.build_corpus_context(
        vault,
        language_registry=synthetic,
    )

    assert second is not first


def test_kill_switch_disables_cache(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_CORPUS_CACHE", "1")
    first = semantic_contract.build_corpus_context(vault)
    second = semantic_contract.build_corpus_context(vault)
    assert second is not first
