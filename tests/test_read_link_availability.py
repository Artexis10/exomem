"""Optional links preserve page availability through cold and changing projections."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from exomem import freshness, mode, readiness
from exomem import vault as vault_module
from exomem.file_watcher import FileWatcher

TARGET = "Knowledge Base/Notes/link-target.md"
SOURCE = "Knowledge Base/Notes/link-source.md"


def _seed(vault):
    (vault / TARGET).write_text("# Target\n", encoding="utf-8")
    (vault / SOURCE).write_text("# Source\n\nSee [[Notes/link-target]].\n", encoding="utf-8")
    vault_module.clear_inbound_index()
    freshness.seed(vault, "vault", [
        (str(path), freshness.stat_signature(path))
        for path in vault_module.walk_vault_md(vault)
    ])


def test_existing_owner_restores_links_without_holding_cold_readers(vault, monkeypatch):
    """A held background scan cannot stop concurrent readers or defeat quiet laziness."""
    from exomem.link_summary import link_summary

    _seed(vault)
    monkeypatch.setattr(mode, "preload_cpu_caches", lambda: False)
    watcher = FileWatcher(vault)
    watcher.arm_optional_recovery()
    entered, release = threading.Event(), threading.Event()
    build = vault_module._build_inbound_index

    def held_build(root):
        entered.set()
        assert release.wait(5), "test did not release the optional build"
        return build(root)

    monkeypatch.setattr(vault_module, "_build_inbound_index", held_build)
    watcher._prime_inbound_if_needed()
    assert not entered.is_set(), "quiet mode built an unrequested index"
    assert link_summary(vault, TARGET, "# Target\n")["inbound"] is None
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(watcher._prime_inbound_if_needed)
        try:
            assert entered.wait(5)
            readers = [pool.submit(link_summary, vault, SOURCE, "See [[Notes/link-target]].") for _ in range(3)]
            for reader in readers:
                assert reader.result(timeout=2) == {"inbound": None, "outbound": ["Notes/link-target"]}
        finally:
            release.set()
        owner.result(timeout=5)

    actual = link_summary(vault, TARGET, "# Target\n")["inbound"]
    expected = [entry.as_dict() for entry in vault_module.find_inbound_wikilinks(vault, TARGET)]
    assert actual == expected
    assert any(item["path"] == SOURCE and item["context"] == "See [[Notes/link-target]]." for item in actual)
    vault_module.evict_inbound_index(vault)
    assert link_summary(vault, TARGET, "# Target\n")["inbound"] is None
    watcher._prime_inbound_if_needed()
    assert link_summary(vault, TARGET, "# Target\n")["inbound"] == expected
    vault_module.clear_inbound_index()
    watcher._prime_inbound_if_needed()
    assert link_summary(vault, TARGET, "# Target\n")["inbound"] == expected


def test_changed_visibility_proof_withdraws_cached_ambiguity_result(vault):
    """A stale copied candidate set cannot pass after a policy callback evicts it."""
    _seed(vault)
    duplicate = vault / "Other" / "link-target.md"
    duplicate.parent.mkdir()
    duplicate.write_text("# Other target\n", encoding="utf-8")
    (vault / SOURCE).write_text("# Source\n\nSee [[link-target]].\n", encoding="utf-8")
    freshness.seed(vault, "vault", [
        (str(path), freshness.stat_signature(path))
        for path in vault_module.walk_vault_md(vault)
    ])
    vault_module.find_inbound_wikilinks(vault, TARGET)
    def visible(rel):
        return rel.startswith("Knowledge Base/")
    expected = vault_module.find_inbound_wikilinks(vault, TARGET, visible=visible)
    assert expected
    assert vault_module.cached_inbound_wikilinks(vault, TARGET, visible=visible) == expected

    def changed_view(rel):
        vault_module.evict_inbound_index(vault)
        return visible(rel)

    assert vault_module.cached_inbound_wikilinks(vault, TARGET, visible=changed_view) is None
    # Complete safety callers retain the full path when optional metadata is gone.
    assert vault_module.find_inbound_wikilinks(vault, TARGET, visible=visible) == expected


def test_incremental_patch_withdraws_data_before_mutating_it(vault, monkeypatch):
    """Readers never claim a partially patched resident index is complete."""
    _seed(vault)
    vault_module.find_inbound_wikilinks(vault, TARGET)
    (vault / SOURCE).write_text("# Source\n\nThe link was removed.\n", encoding="utf-8")
    freshness.on_files_changed(vault, changed=[vault / SOURCE])
    entered, release = threading.Event(), threading.Event()
    plan = vault_module._plan_inbound_edit

    def held_patch(root, changed, deleted):
        entered.set()
        assert release.wait(5)
        return plan(root, changed, deleted)

    monkeypatch.setattr(vault_module, "_plan_inbound_edit", held_patch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(vault_module.on_inbound_files_changed, vault, [SOURCE], [])
        try:
            assert entered.wait(5)
            assert vault_module.cached_inbound_wikilinks(vault, TARGET) is None
        finally:
            release.set()
        writer.result(timeout=5)
    assert vault_module.cached_inbound_wikilinks(vault, TARGET) == []


def test_optional_owner_waits_for_activation_and_warmup(vault, monkeypatch):
    """Optional metadata cannot run before startup workers or model warm-up finish."""
    _seed(vault)
    assert vault_module.cached_inbound_wikilinks(vault, TARGET) is None
    watcher = FileWatcher(vault)
    calls = []
    monkeypatch.setattr(vault_module, "prime_inbound_index", lambda *args, **kwargs: calls.append(True))
    watcher._prime_inbound_if_needed()
    assert calls == []
    watcher.arm_optional_recovery()
    monkeypatch.setattr(readiness, "is_warming", lambda: True)
    watcher._prime_inbound_if_needed()
    assert calls == []
    monkeypatch.setattr(readiness, "is_warming", lambda: False)
    watcher._prime_inbound_if_needed()
    assert calls == [True]


def test_the_only_contended_read_retains_demand_for_recovery(vault, monkeypatch):
    """A cold request cannot lose its sole recovery signal behind a busy cache."""
    _seed(vault)
    monkeypatch.setattr(mode, "preload_cpu_caches", lambda: False)
    watcher = FileWatcher(vault)
    watcher.arm_optional_recovery()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with vault_module._INBOUND_INDEX_LOCK:
            read = pool.submit(vault_module.cached_inbound_wikilinks, vault, TARGET)
            assert read.result(timeout=2) is None
    watcher._prime_inbound_if_needed()
    assert vault_module.cached_inbound_wikilinks(vault, TARGET)


def test_unreadable_source_does_not_claim_an_empty_complete_result(vault, monkeypatch):
    """Skipped source bytes must remain unknown until a complete owner pass succeeds."""
    _seed(vault)
    watcher = FileWatcher(vault)
    watcher.arm_optional_recovery()
    read = vault_module.read_guarded_text

    def unavailable(root, path):
        if path == vault / SOURCE:
            raise PermissionError("fixture source temporarily unreadable")
        return read(root, path)

    monkeypatch.setattr(vault_module, "read_guarded_text", unavailable)
    assert vault_module.cached_inbound_wikilinks(vault, TARGET) is None
    watcher._prime_inbound_if_needed()
    assert vault_module.cached_inbound_wikilinks(vault, TARGET) is None
    monkeypatch.setattr(vault_module, "read_guarded_text", read)
    watcher._prime_inbound_if_needed()
    assert vault_module.cached_inbound_wikilinks(vault, TARGET)


def test_a_later_patch_cannot_caption_a_missed_earlier_event(vault):
    """A delayed earlier fan-out cannot disappear behind a later complete caption."""
    _seed(vault)
    (vault / SOURCE).write_text("# Source\n", encoding="utf-8")
    freshness.on_files_changed(vault, changed=[vault / SOURCE])
    assert vault_module.find_inbound_wikilinks(vault, TARGET) == []

    (vault / SOURCE).write_text("# Source\n\nSee [[Notes/link-target]].\n", encoding="utf-8")
    freshness.on_files_changed(vault, changed=[vault / SOURCE])
    assert vault_module.cached_inbound_wikilinks(vault, TARGET) is None
    other = vault / "Knowledge Base/Notes/other.md"
    other.write_text("# A later event\n", encoding="utf-8")
    freshness.on_files_changed(vault, changed=[other])
    vault_module.on_inbound_files_changed(vault, ["Knowledge Base/Notes/other.md"], [])

    assert vault_module.cached_inbound_wikilinks(vault, TARGET) is None
    assert any(link.path == SOURCE for link in vault_module.find_inbound_wikilinks(vault, TARGET))
    assert vault_module.prime_inbound_index(vault)
    assert any(link.path == SOURCE for link in vault_module.cached_inbound_wikilinks(vault, TARGET))
