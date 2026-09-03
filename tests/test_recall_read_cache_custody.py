"""Exact path custody for the read-side substrate caches.

A governed write already names every path it touched. Until now nothing on the
read side consumed that: each substrate cache — the lexical corpus, the
eligibility catalogue, the frontmatter cache, the reference sidecar — kept its
own whole-scope notion of freshness, so a change anywhere in the scope was
indistinguishable from a change to the one page that actually moved.

This suite pins the other half of design Decision 2: when a governed write
commits, the receipt's path set is applied to a registered invalidation seam on
every substrate cache, rows for those paths are refreshed or retired, and
nothing else moves. A change that arrives WITHOUT a receipt — an external edit
reconciliation finds — still invalidates the scope it drifted in, because there
is no path set to be exact about.

The custody report is the contract, not a diagnostic: "an exact update of that
page's rows only (rebuild counters unchanged)" is only checkable if the seams
say what they did, and the same verification is what makes the audit able to
fail closed instead of serving a stale answer.

Every fixture here is produced by the real governed write path, so the receipts
and pending rows are the rows the writer actually emits, and every managed
assertion is taken against `warm_managed_cell` — the configuration the live
cell serves from.
"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

import pytest

from exomem import (
    commands,
    file_watcher,
    find_corpus,
    freshness,
    index_sync,
    lexstore,
    memory_refs,
    semantic_writes,
)
from exomem import find as find_module
from exomem.vault import kb_dirname

pytestmark = pytest.mark.skipif(
    not lexstore.fts5_available(), reason="SQLite build lacks FTS5"
)

_ALPHA = "Knowledge Base/Notes/Custody/custody-alpha.md"
_BETA = "Knowledge Base/Notes/Custody/custody-beta.md"
_GAMMA = "Knowledge Base/Notes/Custody/custody-gamma.md"
_MOVED = "Knowledge Base/Notes/Custody/custody-alpha-moved.md"

#: Every substrate cache the invariant names, plus the reference sidecar, which
#: is the fourth read-side index a recall consumes on the request thread.
_SEAMS = (
    "frontmatter_cache",
    "lexical_corpus",
    "eligibility_catalogue",
    "reference_sidecar",
)


def _source(title: str, marker: str) -> str:
    return (
        "---\n"
        f"title: {title}\n"
        "type: note\n"
        "status: active\n"
        "updated: 2026-08-20\n"
        "projects: [project-alpha]\n"
        "tags: [custody]\n"
        "---\n\n"
        f"# {title}\n\n"
        "## Observations\n\n"
        f"- [config] {marker} #custody (test) ^{title.lower().replace(' ', '-')}\n"
    )


def _seed(vault_root: Path, rel: str, title: str, marker: str) -> Path:
    """Put a page on disk BEFORE the cell is warmed, as part of the corpus."""
    path = vault_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_source(title, marker), encoding="utf-8")
    return path


def _govern(vault_root: Path, rel: str, title: str, marker: str) -> None:
    """One governed write through the real seam: receipt, pending rows and all."""
    preflight = semantic_writes.preflight_existing(
        vault_root,
        path=rel,
        after_source=_source(title, marker),
        operation="observe",
    )
    assert preflight.contract_result.should_block is False
    committed = semantic_writes.commit_existing(vault_root, preflight=preflight)
    assert committed.mutated is True


def _digest(vault_root: Path, rel: str) -> str | None:
    content = find_corpus._read_page_bytes(vault_root / rel, vault_root)
    return None if content is None else hashlib.sha256(content).hexdigest()


def _warm(vault_root: Path, warm_managed_cell) -> None:
    """Warm the cell and hydrate the substrate caches with a real recall."""
    warm_managed_cell(vault_root)
    commands.op_find(
        vault_root,
        query="custody",
        mode="hybrid",
        scope="kb-only",
        graph=False,
        include_timings=True,
    )


def test_one_governed_write_updates_only_its_rows_in_every_substrate_cache(
    vault: Path, warm_managed_cell
) -> None:
    """Spec: "A governed write keeps the substrate caches warm"."""
    _seed(vault, _ALPHA, "Custody alpha", "alpha-seed")
    _seed(vault, _BETA, "Custody beta", "beta-seed")
    _warm(vault, warm_managed_cell)

    beta_before = _digest(vault, _BETA)
    beta_row_before = lexstore.page_content_hashes(vault, [_BETA])[_BETA]
    freshness.reset_custody_telemetry()

    _govern(vault, _ALPHA, "Custody alpha", "alpha-committed")

    report = freshness.last_custody_report()
    assert report is not None, "a governed write published no custody report"
    assert _ALPHA in report.paths
    assert _BETA not in report.paths, "a write to alpha claimed custody of beta"
    for seam in _SEAMS:
        assert report.updated.get(seam) == 1, (
            f"{seam} did not take exact custody of the written page: {report}"
        )
    assert report.rebuilt == {}, f"a governed write rebuilt a cache: {report.rebuilt}"
    assert report.mismatches == ()
    assert freshness.custody_scope_invalidations() == ()

    # Nothing else moved: the untouched page's catalogue row is the same row.
    assert lexstore.page_content_hashes(vault, [_BETA])[_BETA] == beta_row_before
    assert beta_row_before == beta_before
    # And the written page's rows are current, not merely untouched.
    assert lexstore.page_content_hashes(vault, [_ALPHA])[_ALPHA] == _digest(vault, _ALPHA)


def test_receiptless_external_edit_invalidates_the_scope_on_reconciliation(
    vault: Path, warm_managed_cell
) -> None:
    """Spec: "A receipt-less external edit still invalidates"."""
    _seed(vault, _ALPHA, "Custody alpha", "alpha-seed")
    _warm(vault, warm_managed_cell)
    freshness.reset_custody_telemetry()

    # No receipt: the bytes change under the registry, as an external editor
    # or a sync client would leave them.
    (vault / _ALPHA).write_text(
        _source("Custody alpha", "alpha-external"), encoding="utf-8"
    )
    file_watcher.FileWatcher(vault)._reconcile_once(seed=False)

    invalidations = freshness.custody_scope_invalidations()
    assert invalidations, "reconciliation found drift and invalidated nothing"
    assert all(reason == "receiptless_drift" for _scope, reason in invalidations), (
        f"a receipt-less edit was recorded under the wrong reason: {invalidations}"
    )
    assert {scope for scope, _reason in invalidations} == set(freshness.SCOPES), (
        f"drift was found in a scope that was not invalidated: {invalidations}"
    )
    # Invalidating the scope is not the same as rebuilding it: reconciliation
    # dispatches the exact drift delta through the ordinary fan-out, so the
    # caches heal per path even though the change carried no receipt.
    assert freshness.custody_rebuilds() == {}, (
        f"a receipt-less edit rebuilt a cache: {freshness.custody_rebuilds()}"
    )
    result = commands.op_find(
        vault,
        query="alpha-external",
        mode="keyword",
        scope="kb-only",
        graph=False,
        include_timings=True,
    )
    assert any(hit["path"] == _ALPHA for hit in result["hits"]), (
        "the next recall did not reflect the receipt-less edit"
    )


def test_a_burst_of_distinct_writes_leaves_every_cache_row_current(
    vault: Path, warm_managed_cell
) -> None:
    _seed(vault, _ALPHA, "Custody alpha", "alpha-seed")
    _seed(vault, _BETA, "Custody beta", "beta-seed")
    _seed(vault, _GAMMA, "Custody gamma", "gamma-seed")
    _warm(vault, warm_managed_cell)
    freshness.reset_custody_telemetry()

    for rel, title in ((_ALPHA, "Custody alpha"), (_BETA, "Custody beta"), (_GAMMA, "Custody gamma")):
        _govern(vault, rel, title, f"{title.split()[-1]}-burst")

    audit = freshness.audit_custody(vault, [_ALPHA, _BETA, _GAMMA], reason="burst")
    assert audit.mismatches == (), f"a burst left a cache row stale: {audit.mismatches}"
    assert audit.invalidated is False
    assert freshness.custody_rebuilds() == {}, (
        f"a burst of exact writes rebuilt a cache: {freshness.custody_rebuilds()}"
    )
    for rel in (_ALPHA, _BETA, _GAMMA):
        assert lexstore.page_content_hashes(vault, [rel])[rel] == _digest(vault, rel)


def test_a_move_retires_the_old_row_and_creates_the_new_one(
    vault: Path, warm_managed_cell
) -> None:
    _seed(vault, _ALPHA, "Custody alpha", "alpha-seed")
    _warm(vault, warm_managed_cell)
    freshness.reset_custody_telemetry()

    commands.op_move_file(vault, _ALPHA, _MOVED, update_wikilinks=False)

    retirements = freshness.custody_reports_for(_ALPHA)
    assert retirements, "the moved page's old path took no custody action"
    assert any(
        report.retired.get(seam, 0) >= 1 for report in retirements for seam in _SEAMS
    ), f"the old row survived the move: {retirements}"
    for seam in _SEAMS:
        assert any(report.retired.get(seam, 0) >= 1 for report in retirements), (
            f"{seam} did not retire the moved page's old row"
        )

    creations = freshness.custody_reports_for(_MOVED)
    assert creations, "the moved page's new path took no custody action"
    for seam in _SEAMS:
        assert any(report.updated.get(seam, 0) >= 1 for report in creations), (
            f"{seam} did not create the moved page's new row"
        )

    assert lexstore.page_content_hashes(vault, [_ALPHA])[_ALPHA] is None
    assert lexstore.page_content_hashes(vault, [_MOVED])[_MOVED] == _digest(vault, _MOVED)
    assert (vault / _ALPHA) not in find_corpus.CACHE.entries
    assert freshness.custody_rebuilds() == {}


def test_a_delete_retires_the_rows_everywhere(vault: Path, warm_managed_cell) -> None:
    _seed(vault, _ALPHA, "Custody alpha", "alpha-seed")
    _warm(vault, warm_managed_cell)
    freshness.reset_custody_telemetry()

    commands.op_delete(vault, _ALPHA, confirm=True)

    reports = freshness.custody_reports_for(_ALPHA)
    assert reports, "the deleted page took no custody action"
    for seam in _SEAMS:
        assert any(report.retired.get(seam, 0) >= 1 for report in reports), (
            f"{seam} did not retire the deleted page's row"
        )
    assert lexstore.page_content_hashes(vault, [_ALPHA])[_ALPHA] is None
    assert memory_refs.ReferenceIndex(vault).ref_for_path(_ALPHA) is None
    assert (vault / _ALPHA) not in find_corpus.CACHE.entries
    assert freshness.custody_rebuilds() == {}

    audit = freshness.audit_custody(vault, [_ALPHA], reason="post_delete")
    assert audit.mismatches == ()


def test_restart_hydration_does_not_rebuild_receipt_covered_caches(
    vault: Path, warm_managed_cell, walk_sentinel
) -> None:
    """A restart re-seeds the registry; it must not re-derive the sidecars.

    The durable catalogue and reference sidecar were brought current by exact
    receipts before the process went away. Rebuilding them on the way back up
    would charge a reader the whole corpus for state that is already exact.
    """
    _seed(vault, _ALPHA, "Custody alpha", "alpha-seed")
    _warm(vault, warm_managed_cell)
    _govern(vault, _ALPHA, "Custody alpha", "alpha-before-restart")

    # The process goes away: every in-process cache and registry with it.
    find_module.unload_ram_caches()
    lexstore.clear_stores()
    freshness.clear()
    file_watcher.FileWatcher(vault)._reconcile_once(seed=True)

    freshness.reset_custody_telemetry()
    sentinel = walk_sentinel(vault, vault / kb_dirname(), current_thread_only=True)
    sentinel.reset()
    result = commands.op_find(
        vault,
        query="alpha-before-restart",
        mode="keyword",
        scope="kb-only",
        graph=False,
        include_timings=True,
    )

    assert sentinel.count == 0, sentinel.report()
    assert freshness.custody_rebuilds() == {}, (
        f"restart hydration rebuilt a receipt-covered cache: {freshness.custody_rebuilds()}"
    )
    assert any(hit["path"] == _ALPHA for hit in result["hits"]), (
        "the page committed before the restart is not visible after it"
    )


def test_audit_seeds_a_mismatch_and_fails_closed_to_scope_invalidation(
    vault: Path, warm_managed_cell
) -> None:
    """A cache row that does not match the page fails closed, never answers."""
    _seed(vault, _ALPHA, "Custody alpha", "alpha-seed")
    _warm(vault, warm_managed_cell)
    _govern(vault, _ALPHA, "Custody alpha", "alpha-committed")
    freshness.reset_custody_telemetry()

    # Seed the mismatch the way reality does: bytes that no receipt covers,
    # landing after the catalogue row was published.
    (vault / _ALPHA).write_text(
        _source("Custody alpha", "alpha-unreceipted"), encoding="utf-8"
    )

    audit = freshness.audit_custody(vault, [_ALPHA], reason="seeded_mismatch")

    assert audit.mismatches, "the audit compared nothing and reported clean"
    assert {seam for seam, _rel in audit.mismatches} & set(_SEAMS)
    assert all(rel == _ALPHA for _seam, rel in audit.mismatches)
    assert audit.invalidated is True, "the audit reported a mismatch and served on"
    invalidations = freshness.custody_scope_invalidations()
    assert invalidations, "a mismatch did not fail closed to a scope invalidation"
    assert all(reason.endswith("_mismatch") for _scope, reason in invalidations)
    # Content-free: the audit names seams and paths, never page bytes.
    marker = "alpha-unreceipted"
    assert marker not in repr(audit)


def test_a_declined_refs_sidecar_schedules_one_background_rebuild(
    vault: Path, warm_managed_cell, monkeypatch
) -> None:
    """Spec: "Background recovery restores readers".

    The decline is only half the contract. A reader that is told to retry has
    to have something to retry INTO, so the same request that declines starts
    exactly one background rebuild, and the next reader is served from it.
    """
    _seed(vault, _ALPHA, "Custody alpha", "alpha-seed")
    warm_managed_cell(vault, prebuild_refs=False)

    scheduled: list[bool] = []
    real_request = memory_refs.request_rebuild

    def counting_request(root: Path) -> bool:
        started = real_request(root)
        scheduled.append(started)
        return started

    monkeypatch.setattr(memory_refs, "request_rebuild", counting_request)

    with pytest.raises(find_module.RetrievalIndexWarming) as declined:
        commands.op_find(
            vault,
            query="custody",
            mode="hybrid",
            scope="kb-only",
            graph=False,
            include_timings=True,
        )
    assert declined.value.site == "reference_sidecar"
    assert scheduled, "a declined reader scheduled no rebuild"

    deadline = time.monotonic() + 30.0
    while memory_refs.rebuild_in_flight(vault) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not memory_refs.rebuild_in_flight(vault), "the background rebuild never finished"

    result = commands.op_find(
        vault,
        query="custody",
        mode="hybrid",
        scope="kb-only",
        graph=False,
        include_timings=True,
    )
    assert result["hits"], "the reader was not restored by the background rebuild"


def test_the_declined_refs_rebuild_is_single_flighted(
    vault: Path, monkeypatch
) -> None:
    """A burst of declining readers pays for ONE corpus scan between them.

    Without the in-flight guard every declined request starts its own thread,
    and the moment a cold sidecar is most likely -- a restart under load -- is
    exactly when the most readers arrive at once. Driven against a blocked
    rebuild so the second call is made while the first is genuinely running;
    on a fixture-sized corpus an unblocked scan finishes before a second caller
    could ever observe it, which is how a vacuous version of this passes.
    """
    started = threading.Event()
    release = threading.Event()

    def blocking_rebuild(self) -> dict[str, int]:
        started.set()
        assert release.wait(timeout=30.0)
        return {"indexed": 0, "duplicates": 0, "malformed": 0}

    monkeypatch.setattr(memory_refs.ReferenceIndex, "rebuild_all", blocking_rebuild)
    try:
        first = memory_refs.request_rebuild(vault)
        assert started.wait(timeout=30.0), "the first rebuild never started"
        second = memory_refs.request_rebuild(vault)
        assert (first, second) == (True, False), (
            f"the rebuild is not single-flighted: {(first, second)}"
        )
        assert memory_refs.rebuild_in_flight(vault)
    finally:
        release.set()

    deadline = time.monotonic() + 30.0
    while memory_refs.rebuild_in_flight(vault) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not memory_refs.rebuild_in_flight(vault)


_RECORDS_MANIFEST = """---
type: collection
exomem_id: 12345678-1234-4abc-8def-123456789abc
title: Measurements
semantic_profile: records
collection_version: 1
lifecycle: active
schema_version: 1
storage:
  strategy: markdown-items
  format_version: 1
  source: items
item_schema:
  natural_key: [observed]
  fields:
    observed:
      type: string
---
"""


def test_a_suppressed_record_write_is_exact_custody_not_a_mismatch(
    vault: Path, warm_managed_cell
) -> None:
    """A page the catalogue deliberately holds no row for is still exact.

    Raw Records are in the batch's path set and out of the recall corpus by
    design. If "no row" read as drift, every write that touched one would fail
    its whole scope closed -- which is the failure mode a fail-closed audit has
    to be built not to have.
    """
    manifest = vault / "Knowledge Base/Records/Custody/_collection.md"
    raw = manifest.parent / "items" / "raw.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(_RECORDS_MANIFEST, encoding="utf-8")
    raw.write_text("private measurement", encoding="utf-8")
    _warm(vault, warm_managed_cell)
    freshness.reset_custody_telemetry()

    index_sync.upsert_after_write(vault, [raw, manifest])

    report = freshness.last_custody_report()
    assert report is not None
    raw_rel = "Knowledge Base/Records/Custody/items/raw.md"
    assert raw_rel in report.paths
    assert report.mismatches == (), (
        f"a suppressed raw Record read as drift: {report.mismatches}"
    )
    assert freshness.custody_scope_invalidations() == (), (
        "a suppressed raw Record failed its scope closed"
    )
    for seam in _SEAMS:
        assert report.updated.get(seam, 0) == len(report.paths), (
            f"{seam} did not take exact custody of a suppressed Record batch: {report}"
        )


def test_a_correctness_eviction_does_not_discard_receipt_covered_pages(
    vault: Path, warm_managed_cell
) -> None:
    """An in-flight graph rebuild may not take the substrate caches with it.

    `epistemic_graph` evicts RAM caches to force a re-derivation of the
    resolver, and that eviction used to take the parsed-page cache too. On a
    busy cell that is the exact-custody rule broken from the other side: a
    whole-scope correctness event discarding rows that exact receipts already
    cover. The resolver still goes, because a stale resolver is a wrong answer;
    the pages stay, because every one of them is keyed to its file's content
    signature and evicted by its own receipt.

    The second half is what makes the first half safe: after the eviction, a
    governed write must still move exactly its own page's rows. Rows that
    survive a whole-scope event but stop tracking their receipts would be worse
    than rows that were dropped.
    """
    _seed(vault, _ALPHA, "Custody alpha", "alpha-seed")
    _seed(vault, _BETA, "Custody beta", "beta-seed")
    _warm(vault, warm_managed_cell)
    resident = len(find_corpus.CACHE.entries)
    assert resident, "the recall hydrated no pages, so this pins nothing"
    freshness.reset_custody_telemetry()

    # The seam `epistemic_graph.py` calls during a rebuild.
    find_module.unload_ram_caches()

    assert len(find_corpus.CACHE.entries) == resident, (
        "a correctness eviction discarded receipt-covered page rows"
    )
    assert freshness.custody_rebuilds() == {}
    assert (vault / _ALPHA) in find_corpus.CACHE.entries
    assert (vault / _BETA) in find_corpus.CACHE.entries

    _govern(vault, _ALPHA, "Custody alpha", "alpha-after-eviction")

    report = freshness.last_custody_report()
    assert report is not None
    assert report.paths == (_ALPHA,)
    for seam in _SEAMS:
        assert report.updated.get(seam) == 1, (
            f"{seam} lost track of its receipts after a whole-scope eviction: {report}"
        )
    assert report.rebuilt == {}
    assert (vault / _ALPHA) not in find_corpus.CACHE.entries, (
        "the written page's row survived its own receipt"
    )
    assert (vault / _BETA) in find_corpus.CACHE.entries, (
        "a write to alpha evicted beta's row"
    )

    # The other meaning of "evict" is unchanged: a caller releasing memory
    # still gets the large cache back.
    find_module.release_idle_ram_caches()
    assert find_corpus.CACHE.entries == {}


def test_cold_lexical_corpus_declines_with_warming_and_schedules_repair(
    vault: Path, warm_managed_cell, walk_sentinel
) -> None:
    """Spec: "A lexical corpus miss warms away from the request"."""
    _seed(vault, _ALPHA, "Custody alpha", "alpha-seed")
    warm_managed_cell(vault)

    # Retire the maintained catalogue under a managed reader.
    lexstore.clear_stores()
    catalog = lexstore.lexical_path(vault)
    for artifact in (catalog, Path(f"{catalog}-wal"), Path(f"{catalog}-shm")):
        if artifact.exists():
            artifact.unlink()

    sentinel = walk_sentinel(vault, vault / kb_dirname(), current_thread_only=True)
    sentinel.reset()
    with pytest.raises(find_module.RetrievalIndexWarming):
        commands.op_find(
            vault,
            query="custody",
            mode="keyword",
            scope="kb-only",
            graph=False,
            include_timings=True,
        )

    assert sentinel.count == 0, sentinel.report()
    assert lexstore.repair_progress(vault) is not None, (
        "a declined managed reader scheduled no background repair"
    )
