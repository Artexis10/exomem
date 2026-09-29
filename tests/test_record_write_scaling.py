"""A guarded append or an inspect must not do per-item governance work.

The Records write path used to reload the governance policy and rescan the
tombstone generation once per item, so one append cost O(N) filesystem probes.
These are structural counts, not latency thresholds: the number of policy loads
and tombstone scans in one operation must be the same at 3 items as at 12.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from record_fixtures import ledger_item, setup_ledger_collection

from exomem import record_governance, records
from exomem.governance import lifecycle
from exomem.governance import policy as policy_module

COLLECTION = "Knowledge Base/Records/Publications/_collection.md"


def _seed(vault: Path, count: int) -> None:
    setup_ledger_collection(vault)
    for index in range(count):
        records.append_record(
            vault,
            COLLECTION,
            item=ledger_item(slug=f"seed-{index:03d}"),
            why="seed",
        )


class _Counts:
    def __init__(self) -> None:
        self.policy_loads = 0
        self.tombstone_scans = 0


@pytest.fixture
def counts(monkeypatch: pytest.MonkeyPatch) -> _Counts:
    seen = _Counts()
    real_load = policy_module.load
    real_scan = lifecycle._tombstone_generation

    def load(*args, **kwargs):
        seen.policy_loads += 1
        return real_load(*args, **kwargs)

    def scan(*args, **kwargs):
        seen.tombstone_scans += 1
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(policy_module, "load", load)
    monkeypatch.setattr(lifecycle, "_tombstone_generation", scan)
    return seen


def _measure(vault: Path, counts: _Counts, operation) -> tuple[int, int]:
    counts.policy_loads = 0
    counts.tombstone_scans = 0
    operation()
    return counts.policy_loads, counts.tombstone_scans


def _append_counts(tmp_path: Path, counts: _Counts, size: int) -> tuple[int, int]:
    vault = tmp_path / f"append-{size}"
    vault.mkdir()
    _seed(vault, size)
    return _measure(
        vault,
        counts,
        lambda: records.append_record(
            vault, COLLECTION, item=ledger_item(slug="measured"), why="measure"
        ),
    )


def _inspect_counts(tmp_path: Path, counts: _Counts, size: int) -> tuple[int, int]:
    vault = tmp_path / f"inspect-{size}"
    vault.mkdir()
    _seed(vault, size)
    return _measure(
        vault, counts, lambda: record_governance.inspect_collection(vault, COLLECTION)
    )


def test_append_governance_work_does_not_grow_with_item_count(
    tmp_path: Path, counts: _Counts
) -> None:
    small = _append_counts(tmp_path, counts, 3)
    large = _append_counts(tmp_path, counts, 12)
    assert large == small, f"per-append governance work grew with items: {small} -> {large}"


def test_inspect_governance_work_does_not_grow_with_item_count(
    tmp_path: Path, counts: _Counts
) -> None:
    small = _inspect_counts(tmp_path, counts, 3)
    large = _inspect_counts(tmp_path, counts, 12)
    assert large == small, f"per-inspect governance work grew with items: {small} -> {large}"


# --- item reads: an unchanged item is never re-read, re-hashed or re-parsed ----------------


def _age(vault: Path) -> None:
    """Make every seeded item look old enough for its stat generation to be trusted."""
    import os

    old = 1_600_000_000
    for path in (vault / "Knowledge Base/Records/Publications/Entries").rglob("*.md"):
        os.utime(path, (old, old))


def _no_graph_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    """The derived-graph rebuild sometimes runs on the calling thread and re-reads items."""
    from exomem import graph_sync

    monkeypatch.setattr(graph_sync, "start_registered", lambda *a, **k: None)
    monkeypatch.setattr(graph_sync, "join_registered_if_settled", lambda *a, **k: True)


@pytest.fixture
def item_reads(monkeypatch: pytest.MonkeyPatch) -> _Counts:
    from exomem import vault as vault_module

    _no_graph_rebuild(monkeypatch)

    seen = _Counts()
    seen.reads = 0  # type: ignore[attr-defined]
    seen.parses = 0  # type: ignore[attr-defined]
    real_read = vault_module._read_bounded_guarded_snapshot
    real_parse = vault_module.parse_frontmatter
    # Count only this thread: the derived-graph rebuild reads items too.
    caller = threading.get_ident()

    def read(*args, **kwargs):
        if threading.get_ident() == caller:
            seen.reads += 1  # type: ignore[attr-defined]
        return real_read(*args, **kwargs)

    def parse(*args, **kwargs):
        if threading.get_ident() == caller:
            seen.parses += 1  # type: ignore[attr-defined]
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(vault_module, "_read_bounded_guarded_snapshot", read)
    monkeypatch.setattr(vault_module, "parse_frontmatter", parse)
    return seen


def _warm_then_count(tmp_path: Path, seen, size: int, operation) -> tuple[int, int]:
    from exomem import record_item_cache

    record_item_cache.clear()
    vault = tmp_path / f"reads-{size}"
    vault.mkdir()
    _seed(vault, size)
    _age(vault)
    records.append_record(vault, COLLECTION, item=ledger_item(slug="warm"), why="warm")
    seen.reads = 0
    seen.parses = 0
    operation(vault)
    return seen.reads, seen.parses


def test_repeat_append_does_not_reread_unchanged_items(
    tmp_path: Path, item_reads, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import record_item_cache

    monkeypatch.setattr(record_item_cache, "RACY_WINDOW_NS", 0)
    op = lambda vault: records.append_record(  # noqa: E731
        vault, COLLECTION, item=ledger_item(slug="measured"), why="measure"
    )
    small = _warm_then_count(tmp_path, item_reads, 3, op)
    large = _warm_then_count(tmp_path, item_reads, 12, op)
    assert large == small, f"reads/parses grew with items: {small} -> {large}"


def test_repeat_inspect_does_not_reread_unchanged_items(
    tmp_path: Path, item_reads, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import record_item_cache

    monkeypatch.setattr(record_item_cache, "RACY_WINDOW_NS", 0)
    op = lambda vault: record_governance.inspect_collection(vault, COLLECTION)  # noqa: E731
    small = _warm_then_count(tmp_path, item_reads, 3, op)
    large = _warm_then_count(tmp_path, item_reads, 12, op)
    assert large == small, f"reads/parses grew with items: {small} -> {large}"


# --- out-of-band edits are still detected --------------------------------------------------


def _container_hash(vault: Path) -> str:
    from exomem import record_formats
    from exomem import structured_collections as collections

    manifest = collections.load_manifest(vault, vault / COLLECTION)
    return record_formats.load_adapter(vault, manifest).read().snapshot


def _first_item(vault: Path) -> Path:
    return sorted((vault / "Knowledge Base/Records/Publications/Entries").rglob("*.md"))[0]


@pytest.mark.parametrize("trusting_window", [0, 2_000_000_000])
def test_same_size_edit_with_restored_mtime_changes_the_container_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trusting_window: int
) -> None:
    """An editor that rewrites in place and restores mtime is still seen (ctime, or racy window)."""
    import os
    import sys

    from exomem import record_item_cache

    if sys.platform == "win32":
        pytest.skip("stat generations are not trusted on this platform")
    monkeypatch.setattr(record_item_cache, "RACY_WINDOW_NS", trusting_window)
    record_item_cache.clear()
    vault = tmp_path / "vault"
    vault.mkdir()
    _seed(vault, 3)
    _age(vault)
    before = _container_hash(vault)
    target = _first_item(vault)
    original = target.stat()
    data = target.read_bytes()
    edited = data.replace(b"First", b"Frist", 1)
    assert edited != data and len(edited) == len(data)
    target.write_bytes(edited)
    os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert _container_hash(vault) != before


def test_out_of_band_edit_makes_a_guarded_append_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import record_item_cache
    from exomem.structured_collections import CollectionError

    monkeypatch.setattr(record_item_cache, "RACY_WINDOW_NS", 0)
    record_item_cache.clear()
    vault = tmp_path / "vault"
    vault.mkdir()
    _seed(vault, 3)
    _age(vault)
    guard = _container_hash(vault)
    target = _first_item(vault)
    target.write_bytes(target.read_bytes() + b"\nedited outside\n")
    with pytest.raises(CollectionError) as error:
        records.append_record(
            vault,
            COLLECTION,
            item=ledger_item(slug="late"),
            why="late",
            expected_container_hash=guard,
        )
    assert error.value.code == "STALE_RECORD"


# --- guard rechecks: a leaf is stat'd a small constant number of times per append ------------


def _stat_calls_for_append(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int) -> int:
    import os
    import threading as _threading

    from exomem import record_item_cache

    _no_graph_rebuild(monkeypatch)
    monkeypatch.setattr(record_item_cache, "RACY_WINDOW_NS", 0)
    record_item_cache.clear()
    vault = tmp_path / f"stat-{size}-{len(list(tmp_path.iterdir()))}"
    vault.mkdir()
    _seed(vault, size)
    _age(vault)
    records.append_record(vault, COLLECTION, item=ledger_item(slug="warm"), why="warm")

    caller = _threading.get_ident()
    calls = {"n": 0}
    real_lstat, real_stat = os.lstat, os.stat

    def counting(real):
        def inner(*args, **kwargs):
            if _threading.get_ident() == caller:
                calls["n"] += 1
            return real(*args, **kwargs)

        return inner

    with monkeypatch.context() as patch:
        # Path.lstat and Path.stat route through these on this interpreter.
        patch.setattr(os, "lstat", counting(real_lstat))
        patch.setattr(os, "stat", counting(real_stat))
        records.append_record(
            vault, COLLECTION, item=ledger_item(slug="measured"), why="measure"
        )
    return calls["n"]


def test_append_stats_each_item_a_bounded_number_of_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Marginal filesystem probes per extra item stay small: ancestors are verified once a round."""
    _stat_calls_for_append(tmp_path, monkeypatch, 3)  # warm process-wide memo caches
    small = _stat_calls_for_append(tmp_path, monkeypatch, 3)
    large = _stat_calls_for_append(tmp_path, monkeypatch, 30)
    per_item = (large - small) / 27
    # Before: ~10 guard rounds x (6 ancestors + 2 leaf probes) plus censuses, ~700 per item.
    assert per_item < 40, f"{per_item:.0f} marginal stat calls per item per append"


def test_append_parses_the_manifest_yaml_a_bounded_number_of_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One append re-resolves its manifest many times; identical bytes parse once."""
    from exomem import structured_collections as collections

    vault = tmp_path / "vault"
    vault.mkdir()
    _seed(vault, 3)
    calls = {"n": 0}
    real = collections._manifest_from_frontmatter

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(collections, "_manifest_from_frontmatter", counting)
    records.append_record(vault, COLLECTION, item=ledger_item(slug="measured"), why="measure")
    # The unchanged manifest parses at most once; the audit-head rewrite is one new document.
    assert calls["n"] <= 3, f"{calls['n']} manifest parses in one append"


def test_inspect_snapshots_the_collection_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard refresh a client pays between appends reads the collection one time."""
    from exomem import record_formats

    vault = tmp_path / "vault"
    vault.mkdir()
    _seed(vault, 3)
    calls = {"n": 0}
    real = record_formats.MarkdownItemsAdapter.read

    def counting(self):
        calls["n"] += 1
        return real(self)

    monkeypatch.setattr(record_formats.MarkdownItemsAdapter, "read", counting)
    result = record_governance.inspect_collection(vault, COLLECTION)
    assert calls["n"] == 1, f"{calls['n']} snapshots in one inspect"
    assert "expected_container_hash" in str(result)
