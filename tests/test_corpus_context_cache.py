"""Correctness of the corpus-context cache (semantic_contract).

The cache may only ever serve a context that is indistinguishable from a
fresh build. Object identity is the detector below: a cache hit returns the
same object, a rebuild returns a new one.
"""

from __future__ import annotations

import dataclasses
import os
import threading
import time
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
from exomem import vault as vault_module
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


def test_reserved_runtime_trees_do_not_enter_identity_census_or_cache_token(
    vault: Path,
) -> None:
    receipt_root = vault / "Knowledge Base" / ".graph-commit-receipts"
    reset_root = vault / "Knowledge Base" / f".graph-reset-{'c' * 24}"
    batch_root = (
        vault
        / "Knowledge Base"
        / "Notes"
        / "Insights"
        / f".exomem-batch-{'a' * 32}"
    )
    nested_reset_root = (
        vault / "Knowledge Base" / "Notes" / f".graph-reset-{'1' * 24}"
    )
    receipt_root.mkdir()
    reset_root.mkdir()
    batch_root.mkdir()
    nested_reset_root.mkdir()
    receipt_page = receipt_root / "private.md"
    reset_page = reset_root / "private.md"
    batch_page = batch_root / "stage.md"
    nested_reset_page = nested_reset_root / "page.md"
    invalid_runtime_page = "---\nexomem_id: not-a-stable-id\n---\n"
    receipt_page.write_text(invalid_runtime_page, encoding="utf-8")
    reset_page.write_text(invalid_runtime_page, encoding="utf-8")
    batch_page.write_text(invalid_runtime_page, encoding="utf-8")
    nested_reset_page.write_text(
        _page(title="Nested reset lookalike"), encoding="utf-8"
    )

    first = semantic_contract.build_corpus_context(vault)

    assert set(first.pages) == {
        "Knowledge Base/Notes/Insights/one.md",
        "Knowledge Base/Notes/Insights/two.md",
        f"Knowledge Base/Notes/.graph-reset-{'1' * 24}/page.md",
    }
    assert {entry.path for entry in first.identity_census.entries} == set(first.pages)

    receipt_page.write_text(invalid_runtime_page + "receipt changed\n", encoding="utf-8")
    reset_page.write_text(invalid_runtime_page + "reset changed\n", encoding="utf-8")
    batch_page.write_text(invalid_runtime_page + "batch changed\n", encoding="utf-8")

    assert semantic_contract.build_corpus_context(vault) is first

    nested_reset_page.write_text(
        _page(title="Changed nested reset lookalike"), encoding="utf-8"
    )
    changed = semantic_contract.build_corpus_context(vault)
    assert changed is not first
    assert changed.pages[
        f"Knowledge Base/Notes/.graph-reset-{'1' * 24}/page.md"
    ].title == "Changed nested reset lookalike"

    kb = vault / "Knowledge Base"
    assert not semantic_contract._prune_identity_census_directory(
        kb, kb, ".GRAPH-COMMIT-RECEIPTS"
    )
    assert not semantic_contract._prune_identity_census_directory(
        kb, kb / "Notes", f".EXOMEM-BATCH-{'b' * 32}"
    )
    assert not semantic_contract._prune_identity_census_directory(
        kb, kb, f".GRAPH-RESET-{'d' * 24}"
    )
    assert not semantic_contract._prune_identity_census_directory(
        kb, kb, ".graph-reset-not-an-operation-id"
    )
    assert not semantic_contract._prune_identity_census_directory(
        kb, kb / "Notes", f".graph-reset-{'e' * 24}"
    )
    graph_rebuild = f".graph-rebuild-{'a' * 64}-{'b' * 24}.sqlite"
    assert semantic_contract._prune_identity_census_directory(kb, kb, graph_rebuild)
    assert semantic_contract._prune_identity_census_directory(
        kb, kb, f"{graph_rebuild}-journal"
    )
    assert not semantic_contract._prune_identity_census_directory(
        kb, kb, graph_rebuild.upper()
    )
    assert not semantic_contract._prune_identity_census_directory(
        kb, kb, ".graph-rebuild-user-copy.sqlite"
    )
    assert not semantic_contract._prune_identity_census_directory(
        kb, kb / "Notes", graph_rebuild
    )
    lexical_rebuild = f".lexical.sqlite.rebuild-{'a' * 32}.tmp"
    assert semantic_contract._prune_identity_census_directory(kb, kb, lexical_rebuild)
    assert semantic_contract._prune_identity_census_directory(
        kb, kb, f"{lexical_rebuild}-wal"
    )
    assert semantic_contract._prune_identity_census_directory(
        kb, kb, f"{lexical_rebuild}-shm"
    )
    assert not semantic_contract._prune_identity_census_directory(
        kb, kb, lexical_rebuild.upper()
    )
    assert not semantic_contract._prune_identity_census_directory(
        kb, kb, ".lexical.sqlite.rebuild-user-copy.tmp"
    )
    assert not semantic_contract._prune_identity_census_directory(
        kb, kb / "Notes", lexical_rebuild
    )
    assert vault_module.in_excluded_scan_dir(
        f"Knowledge Base/.graph-reset-{'f' * 24}/private.md"
    )
    assert not vault_module.in_excluded_scan_dir(
        f"Knowledge Base/Notes/.graph-reset-{'f' * 24}/private.md"
    )


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows answers DirEntry.stat() from the directory listing, so an "
    "entry deleted after the listing still stats and the race cannot occur",
)
def test_a_vanishing_sqlite_sidecar_does_not_degrade_the_census(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `-wal` that disappears mid-walk must not cost every caller its cache.

    The Knowledge Base root holds SQLite's transient sidecars beside the
    pages. They are not census input, and the identity census stopped
    statting them in #528 -- but `_corpus_census` mirrors that walk by hand
    and kept the stat ahead of the `.md` filter, so one sidecar vanishing in
    the window between the listing and the stat raised FileNotFoundError and
    degraded the whole census to `None`. Every caller then rebuilt uncached,
    on a vault where nothing a caller can see had changed (#561).

    The deletion is driven from `_prune_identity_census_directory`, which the
    walk calls per child immediately before the stat, so the race is exact
    rather than timed.
    """
    kb = vault / "Knowledge Base"
    sidecar = kb / ".embeddings.sqlite-wal"
    sidecar.write_bytes(b"transient")

    baseline = semantic_contract._corpus_census(vault)
    assert baseline is not None

    real_prune = semantic_contract._prune_identity_census_directory

    def prune_then_vanish(kb_root: Path, directory: Path, name: str) -> bool:
        if name == sidecar.name:
            sidecar.unlink(missing_ok=True)
        return real_prune(kb_root, directory, name)

    monkeypatch.setattr(
        semantic_contract, "_prune_identity_census_directory", prune_then_vanish
    )

    # Identical, not merely non-None: the sidecar was never census input, so
    # its removal may not move the key either.
    assert semantic_contract._corpus_census(vault) == baseline


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


def test_non_markdown_event_does_not_evict_or_corrupt_warm_context(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = semantic_contract.build_corpus_context(vault)
    freshness.seed(
        vault,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault.rglob("*.md")),
    )
    assert semantic_contract.build_corpus_context(vault) is first
    schema = vault / "Knowledge Base" / "_Schema"
    schema.mkdir(parents=True, exist_ok=True)
    manifest = schema / "semantic-activation.yaml"
    manifest.write_text("schema_version: 1\ncontract_version: 1\npages: []\n", encoding="utf-8")

    semantic_contract.publish_corpus_files_changed(vault, changed=(manifest,))

    def fail_census(*args, **kwargs):
        raise AssertionError("an unrelated non-Markdown event must keep the context warm")

    monkeypatch.setattr(semantic_contract, "_corpus_census", fail_census)
    assert semantic_contract.build_corpus_context(vault) is first


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
    rejected = semantic_contract.build_corpus_context(vault)
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
    # the context that missed it. Refilling the cache the rejected delta
    # emptied IS allowed -- that is populate-on-miss -- but only from scratch
    # through the full safety oracle, never by patching the old context.
    other = vault / "Knowledge Base/Notes/Insights/two.md"
    other.write_text(_page(title="Later valid event"), encoding="utf-8")
    oracle_calls: list[str] = []
    real_uncached = semantic_contract._build_corpus_context_uncached

    def counted_oracle(*args, **kwargs):
        oracle_calls.append("build")
        return real_uncached(*args, **kwargs)

    monkeypatch.setattr(semantic_contract, "_build_corpus_context_uncached", counted_oracle)
    semantic_contract.publish_corpus_files_changed(vault, changed=(other,))

    assert oracle_calls, "refilling the cache must use the full safety oracle"
    repopulated = semantic_contract._CORPUS_CONTEXT_CACHE[cache_key][1]
    assert repopulated is not rejected
    assert (
        repopulated.pages["Knowledge Base/Notes/Insights/two.md"].title == "Later valid event"
    )
    assert semantic_contract.build_corpus_context(vault) is repopulated


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


# --- #561: a vanishing sidecar temporary must not void the whole census ----


class _VanishedEntry:
    """A listing entry whose file disappeared before anything stat'ed it.

    Deleting the file for real does not stage the race portably: on Windows
    `DirEntry.stat()` answers from the data `scandir` already returned, so the
    stat succeeds on a file that is gone. Standing in for the entry exercises
    the window on every platform, and counts whether the walk stats at all --
    which is what #528's fix, and now #561's, actually changed.
    """

    def __init__(self, entry: os.DirEntry) -> None:
        self._entry = entry
        self.stat_calls = 0

    @property
    def name(self) -> str:
        return self._entry.name

    @property
    def path(self) -> str:
        return self._entry.path

    def is_symlink(self) -> bool:
        return self._entry.is_symlink()

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        return self._entry.is_dir(follow_symlinks=follow_symlinks)

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        return self._entry.is_file(follow_symlinks=follow_symlinks)

    def stat(self, *, follow_symlinks: bool = True):
        self.stat_calls += 1
        raise FileNotFoundError(2, "No such file or directory", self._entry.path)


def _vanish_on_listing(monkeypatch: pytest.MonkeyPatch, name: str) -> list[_VanishedEntry]:
    """Replace *name* in every listing with an entry that cannot be stat'ed."""
    real_scandir = os.scandir
    swapped: list[_VanishedEntry] = []

    def scandir_with_a_vanished_entry(path):
        entries = []
        for entry in real_scandir(path):
            if entry.name == name:
                stand_in = _VanishedEntry(entry)
                swapped.append(stand_in)
                entries.append(stand_in)
            else:
                entries.append(entry)
        return entries

    monkeypatch.setattr(semantic_contract.os, "scandir", scandir_with_a_vanished_entry)
    return swapped


def test_census_never_stats_a_sidecar_temporary_at_all(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `-wal` companion is not census input, so losing one must cost nothing.

    Before #561 the strict walk stat'ed every entry ahead of the `.md` filter,
    and the `FileNotFoundError` from a companion that had already been dropped
    degraded the whole census to `None` -- which forces an uncached
    whole-corpus build on the write path, for a reason unrelated to anything
    that changed.
    """
    (vault / "Knowledge Base" / ".embeddings.sqlite-wal").write_bytes(b"transient")
    swapped = _vanish_on_listing(monkeypatch, ".embeddings.sqlite-wal")

    census = semantic_contract._corpus_census(vault)

    assert swapped, "the fixture never saw the sidecar in a listing"
    assert all(entry.stat_calls == 0 for entry in swapped)
    assert census is not None
    assert not any(entry[0].endswith("-wal") for entry in census)


def test_census_refuses_rather_than_omits_a_page_that_vanishes_mid_walk(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one place this walk is deliberately stricter than the one it mirrors.

    `_build_identity_census` skips a page that vanishes mid-walk, because it
    reports a snapshot and a page that is gone is simply not in it. This walk is
    a cache key. Returning `None` costs one uncached build; quietly returning a
    census that omits the page would let a cached context the corpus no longer
    matches keep its key and be served as current (#561).
    """
    doomed = vault / "Knowledge Base" / "Notes" / "Insights" / "doomed.md"
    doomed.write_text(_page(title="Doomed"), encoding="utf-8")
    _vanish_on_listing(monkeypatch, "doomed.md")

    assert semantic_contract._corpus_census(vault) is None


# --- populate-on-miss is paid by a writer, so it has to be bounded (#539) ----


def _discarding_populate(monkeypatch: pytest.MonkeyPatch, seconds: float = 0.0) -> list[tuple]:
    """Stand in for a build that runs and then legitimately refuses to stamp.

    Every discard route inside `_build_and_admit_corpus_context` -- registry
    moved, checkpoint advanced, census moved -- reaches the caller as the same
    `False`, so one stub covers them all. What matters to the caller is only
    that the attempt did not land.
    """
    calls: list[tuple] = []

    def build(root: Path, cache_key: tuple[str, str]) -> bool:
        calls.append(cache_key)
        if seconds:
            time.sleep(seconds)
        return False

    monkeypatch.setattr(semantic_contract, "_build_and_admit_corpus_context", build)
    return calls


def test_a_second_publisher_joins_no_second_whole_vault_build(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Single-flight, the concurrent half of the bound.

    Two governed writes landing on the same cold vault used to start two
    independent full censuses and parses of it. Only one can stamp; the other
    pays the whole cost to discard.
    """
    entered = threading.Event()
    release = threading.Event()
    calls: list[tuple] = []

    def blocking_build(root: Path, cache_key: tuple[str, str]) -> bool:
        calls.append(cache_key)
        entered.set()
        assert release.wait(10)
        return False

    monkeypatch.setattr(semantic_contract, "_build_and_admit_corpus_context", blocking_build)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(semantic_contract._populate_corpus_context_after_miss, vault)
        assert entered.wait(10), "the first publisher never reached the build"

        # The second publisher finds the same cold key mid-build and must
        # return rather than start its own.
        semantic_contract._populate_corpus_context_after_miss(vault)
        assert len(calls) == 1

        release.set()
        first.result(timeout=10)

    assert len(calls) == 1


def test_a_discarded_populate_quiets_the_next_publisher(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The memo, the sequential half of the bound.

    A vault that keeps discarding -- external churn, or a projection patch that
    fails and evicts on every publish -- otherwise makes *every* governed write
    pay a full build that cannot land.
    """
    calls = _discarding_populate(monkeypatch)
    cache_key = semantic_contract._corpus_cache_key(vault)

    for _ in range(3):
        semantic_contract._populate_corpus_context_after_miss(vault)

    assert len(calls) == 1
    assert cache_key in semantic_contract._CORPUS_POPULATE_MEMO


def test_a_populate_that_raised_is_quieted_like_one_that_discarded(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raise is "did not stamp" too, and is the more expensive one to repeat.

    The caller already swallows it so the publish it belongs to still succeeds;
    swallowing it without memoizing would leave the loudest failure the only
    unbounded one.
    """
    calls: list[tuple] = []

    def failing_build(root: Path, cache_key: tuple[str, str]) -> bool:
        calls.append(cache_key)
        raise RuntimeError("projection unavailable")

    monkeypatch.setattr(semantic_contract, "_build_and_admit_corpus_context", failing_build)

    for _ in range(3):
        semantic_contract._populate_corpus_context_after_miss(vault)

    assert len(calls) == 1


def test_the_quiet_window_ends_and_the_next_publisher_tries_again(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Suppression is a delay, never a decision.

    A vault that settles has to repopulate on its own; nothing else re-arms
    populate-on-miss for it.
    """
    calls = _discarding_populate(monkeypatch)
    cache_key = semantic_contract._corpus_cache_key(vault)

    semantic_contract._populate_corpus_context_after_miss(vault)
    assert len(calls) == 1

    semantic_contract._CORPUS_POPULATE_MEMO[cache_key] = time.monotonic() - 1.0
    semantic_contract._populate_corpus_context_after_miss(vault)

    assert len(calls) == 2
    # The expired entry is dropped rather than accumulated: a long-lived server
    # must not grow one float per vault it ever failed to populate.
    assert list(semantic_contract._CORPUS_POPULATE_MEMO) == [cache_key]


@pytest.mark.parametrize(
    ("attempt_seconds", "at_least", "at_most"),
    [
        # Instant discard: the floor keeps the memo from being a no-op.
        (0.0, 0.0, 0.05),
        # A real attempt: the window is the attempt's own cost, which is what
        # makes it scale with vault size where no constant here could.
        (0.12, 0.05, 0.30),
        # A pathological build: the ceiling keeps one of them from silencing
        # populate for minutes.
        (0.40, 0.05, 0.30),
    ],
)
def test_the_quiet_window_is_the_clamped_cost_of_the_attempt(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    attempt_seconds: float,
    at_least: float,
    at_most: float,
) -> None:
    """Measured in the failed attempt, not in a number someone picked.

    A populate that discarded after eight seconds of walking is evidence the
    vault is still moving; retrying it immediately spends the next eight the
    same way. Waiting at least as long as the attempt cost caps populate at
    roughly half of wall-clock in the worst state.
    """
    monkeypatch.setattr(semantic_contract, "_CORPUS_POPULATE_MEMO_MIN_SECONDS", 0.05)
    monkeypatch.setattr(semantic_contract, "_CORPUS_POPULATE_MEMO_MAX_SECONDS", 0.30)
    _discarding_populate(monkeypatch, seconds=attempt_seconds)
    cache_key = semantic_contract._corpus_cache_key(vault)

    semantic_contract._populate_corpus_context_after_miss(vault)
    remaining = semantic_contract._CORPUS_POPULATE_MEMO[cache_key] - time.monotonic()

    # Read after the fact, so `remaining` can only understate the window.
    assert remaining <= at_most
    assert remaining > at_least


def test_a_quieted_publish_leaves_the_cache_cold_not_wrong(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound may only ever decline to start a build.

    Declining leaves the cache empty, which is exactly the state a publish onto
    a cold cache produced before populate-on-miss existed, and which every
    reader already handles by building. So a suppressed populate can cost a
    reader time; it can never cost it correctness.
    """
    calls = _discarding_populate(monkeypatch)
    cache_key = semantic_contract._corpus_cache_key(vault)
    semantic_contract._populate_corpus_context_after_miss(vault)
    assert len(calls) == 1

    page = vault / _PAGE_REL
    page.write_text(_page(title="Edited while quiet"), encoding="utf-8")
    semantic_contract.publish_corpus_files_changed(vault, changed=(page,))

    # The publish took the miss branch and was turned away there, not earlier.
    assert len(calls) == 1
    assert cache_key not in semantic_contract._CORPUS_CONTEXT_CACHE

    context = semantic_contract.build_corpus_context(vault)
    assert context.pages[_PAGE_REL].title == "Edited while quiet"


def test_resetting_the_cache_re_arms_populate(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`reset_corpus_context_cache` means cold *and* unquieted.

    It is the seam tests use to start from scratch, so a memo surviving it
    would leak one test's suppression into the next.
    """
    calls = _discarding_populate(monkeypatch)
    semantic_contract._populate_corpus_context_after_miss(vault)
    assert len(calls) == 1

    semantic_contract.reset_corpus_context_cache()
    semantic_contract._populate_corpus_context_after_miss(vault)

    assert len(calls) == 2


def test_eviction_does_not_re_arm_populate(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eviction is the front half of the loop being bounded.

    "Patch fails -> evict -> populate -> discard" is the exact cycle that made
    every write pay a doomed build. Letting the evict clear the memo would hand
    that amplification straight back.
    """
    calls = _discarding_populate(monkeypatch)
    semantic_contract._populate_corpus_context_after_miss(vault)
    assert len(calls) == 1

    semantic_contract.evict_corpus_context(vault)
    semantic_contract._populate_corpus_context_after_miss(vault)

    assert len(calls) == 1
