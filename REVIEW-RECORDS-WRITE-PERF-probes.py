"""Adversarial probes for the records write-perf change (review only)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from record_fixtures import ledger_item, setup_ledger_collection

from exomem import access, record_governance, record_item_cache, records, vault as vault_mod
from exomem import structured_collections as collections

COLLECTION = "Knowledge Base/Records/Publications/_collection.md"


def _seed(vault: Path, count: int) -> None:
    setup_ledger_collection(vault)
    for index in range(count):
        records.append_record(
            vault, COLLECTION, item=ledger_item(slug=f"seed-{index:03d}"), why="seed"
        )


def _items(vault: Path) -> list[Path]:
    return sorted((vault / "Knowledge Base/Records/Publications/Entries").rglob("*.md"))


def _refuse(monkeypatch, needle: str) -> None:
    real = access.refuse_if_excluded
    monkeypatch.setattr(
        access, "refuse_if_excluded", lambda root, rel: needle in rel or real(root, rel)
    )


def _vault(tmp_path: Path, n: int = 3) -> Path:
    record_item_cache.clear()
    v = tmp_path / "vault"
    v.mkdir()
    _seed(v, n)
    return v


def test_empty_census_is_not_proof_of_visibility(tmp_path, monkeypatch):
    v = _vault(tmp_path)
    manifest = collections.load_manifest(v, v / COLLECTION)
    hidden = _items(v)[0].relative_to(v).as_posix()
    _refuse(monkeypatch, Path(hidden).name)
    with pytest.raises(collections.CollectionError):
        record_governance.require_mutation_visibility(v, manifest)
    # A census that names no directory at all must not waive the scan.
    with pytest.raises(collections.CollectionError):
        record_governance.require_mutation_visibility(v, manifest, census=())


def test_census_of_another_directory_is_not_proof(tmp_path, monkeypatch):
    v = _vault(tmp_path)
    manifest = collections.load_manifest(v, v / COLLECTION)
    hidden = _items(v)[0].relative_to(v).as_posix()
    _refuse(monkeypatch, Path(hidden).name)
    elsewhere = v / "Knowledge Base/Elsewhere"
    elsewhere.mkdir()
    (elsewhere / "a.md").write_text("x")
    census = (
        vault_mod.DirectoryCensusGuard.capture(v, "Knowledge Base/Elsewhere", max_entries=10),
    )
    with pytest.raises(collections.CollectionError):
        record_governance.require_mutation_visibility(v, manifest, census=census)


def test_refused_item_still_makes_inspect_audit_incomplete(tmp_path, monkeypatch):
    v = _vault(tmp_path)
    hidden = _items(v)[0].name
    _refuse(monkeypatch, hidden)
    result = record_governance.inspect_collection(v, COLLECTION)
    assert result["audit"]["status"] != "complete", result["audit"]


def test_untrusted_platform_reads_by_content(tmp_path, monkeypatch):
    monkeypatch.setattr(vault_mod, "STAT_GENERATION_TRUSTED", False)
    monkeypatch.setattr(record_item_cache, "RACY_WINDOW_NS", 0)
    v = _vault(tmp_path)
    target = _items(v)[0]
    rel = target.relative_to(v).as_posix()
    time.sleep(0.05)
    data, _d, guard = record_item_cache.read_item(v, rel, limit=1 << 20)
    assert record_item_cache._ITEMS == {} and guard.leaf_policy == "content"
    st = target.stat()
    edited = data.replace(b"First", b"Frist", 1)
    assert edited != data and len(edited) == len(data)
    target.write_bytes(edited)
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
    with pytest.raises(vault_mod.PathGuardError):
        guard.recheck(v)
    assert record_item_cache.read_item(v, rel, limit=1 << 20)[0] == edited


def test_recent_write_is_never_cached(tmp_path):
    v = _vault(tmp_path)
    rel = _items(v)[0].relative_to(v).as_posix()
    _data, _d, guard = record_item_cache.read_item(v, rel, limit=1 << 20)
    assert record_item_cache._ITEMS == {} and guard.leaf_policy != "generation"


def test_generation_guard_catches_same_size_restored_mtime(tmp_path, monkeypatch):
    monkeypatch.setattr(record_item_cache, "RACY_WINDOW_NS", 0)
    v = _vault(tmp_path)
    target = _items(v)[0]
    rel = target.relative_to(v).as_posix()
    time.sleep(0.05)
    data, _d, guard = record_item_cache.read_item(v, rel, limit=1 << 20)
    assert guard.leaf_policy == "generation"
    st = target.stat()
    target.write_bytes(data.replace(b"First", b"Frist", 1))
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
    with pytest.raises(vault_mod.PathGuardError):
        guard.recheck(v)
    assert record_item_cache.read_item(v, rel, limit=1 << 20)[0] != data


def test_ancestor_swap_invalidates_a_cached_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(record_item_cache, "RACY_WINDOW_NS", 0)
    v = _vault(tmp_path)
    target = _items(v)[0]
    rel = target.relative_to(v).as_posix()
    time.sleep(0.05)
    record_item_cache.read_item(v, rel, limit=1 << 20)
    parent = target.parent
    moved = parent.with_name(parent.name + "-real")
    parent.rename(moved)
    parent.symlink_to(moved, target_is_directory=True)  # same inode reached via symlink
    _data, _d, guard = record_item_cache.read_item(v, rel, limit=1 << 20)
    with pytest.raises(vault_mod.PathGuardError):
        vault_mod.recheck_path_guards(v, [guard])


def test_pass_scope_does_not_outlive_the_call(tmp_path):
    v = _vault(tmp_path)
    assert record_governance._AUTHORIZATION_PASS.get() is None
    record_governance.inspect_collection(v, COLLECTION)
    assert record_governance._AUTHORIZATION_PASS.get() is None
