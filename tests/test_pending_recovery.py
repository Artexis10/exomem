"""Incomplete pending content must not disable unrelated admitted recall."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_pending_recall_delta as delta
from test_pending_recall_delta import _lane2_state, managed_runtime  # noqa: F401

from exomem import (
    deferred_index,
    derived_drain,
    derived_receipts,
    find,
    index_sync,
    lexstore,
    memory_refs,
    pending_recall,
)


@pytest.mark.usefixtures("managed_runtime")
def test_one_moved_pending_page_preserves_other_recall_and_worker_heals(
    tmp_path: Path,
) -> None:
    stable = "Knowledge Base/Notes/stable.md"
    moved = "Knowledge Base/Notes/moved.md"
    identity = "12345678-1234-4234-8234-123456789012"
    stable_identity = "87654321-4321-4321-8321-210987654321"
    old = delta._page(title="Moved", body="oldneedle", updated="2026-01-01", exomem_id=identity)
    delta._seed_corpus(tmp_path)
    delta._write(
        tmp_path,
        stable,
        delta._page(
            title="Stable", body="stableneedle", updated="2026-01-01", exomem_id=stable_identity
        ),
    )
    delta._write(tmp_path, moved, old)
    delta._prime(tmp_path)
    changes = [
        (moved, old, delta._page(title="Moved", body="pendingneedle", updated="2026-02-01"), None)
    ]
    for index in range(35):
        rel = f"Knowledge Base/Notes/proven-{index}.md"
        changes.append(
            (
                rel,
                None,
                delta._page(title=f"Proven {index}", body="provenneedle", updated="2026-02-01"),
                None,
            )
        )
    delta._publish_change(
        tmp_path, batch_id="mixed-ready", generation="generation-mixed", changes=tuple(changes)
    )
    delta._write(
        tmp_path, moved, delta._page(title="Moved", body="currentneedle", updated="2026-03-01")
    )
    pending_recall.reset()
    find.reset_page_and_result_caches()

    assert delta._keyword(tmp_path, "stableneedle") == [stable]
    assert delta._hybrid(tmp_path, "stableneedle") == [stable]
    assert len(delta._keyword(tmp_path, "provenneedle", limit=40)) == 35
    assert delta._keyword(tmp_path, "oldneedle") == []
    assert memory_refs.ReferenceIndex(tmp_path).refs_for_paths([moved])[moved] is None
    assert memory_refs.ReferenceIndex(tmp_path).refs_for_paths([stable])[
        stable
    ] == memory_refs.memory_ref(stable_identity)
    with pytest.raises(find.RetrievalIndexWarming):
        memory_refs.ReferenceIndex(tmp_path).resolve(identity)
    with pytest.raises(find.RetrievalIndexWarming):
        memory_refs.ReferenceIndex(tmp_path).resolve(stable_identity)
    with pytest.raises(find.RetrievalIndexWarming):
        memory_refs.paths_for_ids_read_only(tmp_path, [stable_identity])

    delta._publish_lanes(tmp_path, (moved,))
    derived_drain.drain_once(
        tmp_path,
        dispatch=lambda *_: True,
        observe_current_generation=lambda _: "generation-current",
        visibility_publisher=pending_recall.publish,
        retire_visibility=derived_drain.pending_visibility_retirer(),
        limit=1,
        now=100.0,
    )
    snapshot = derived_receipts.snapshot_pending_visibility(tmp_path, limit=512)
    assert moved not in snapshot.paths
    assert len(snapshot.paths) == 35
    assert delta._keyword(tmp_path, "currentneedle") == [moved]


def test_overflow_footprint_validates_every_path_without_receipt_hydration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(3):
        delta._prepare(
            tmp_path,
            batch_id=f"footprint-{index}",
            generation="generation-footprint",
            paths=(
                delta._batch_path(
                    f"Knowledge Base/Notes/path-{index}.md", before=None, after="pending"
                ),
            ),
        )
    monkeypatch.setattr(
        derived_receipts,
        "_receipt_from_connection",
        lambda *_: pytest.fail("overflow hydrated a receipt"),
    )
    snapshot = derived_receipts.snapshot_pending_visibility(tmp_path, limit=2)
    assert snapshot.outcome == "overflow"
    assert len(snapshot.paths) == 3
    with sqlite3.connect(deferred_index.store_path(tmp_path)) as connection:
        connection.execute(
            "UPDATE pending_recall_rows SET canonical_generation = 'wrong' WHERE batch_id = 'footprint-2'"
        )
    corrupt = derived_receipts.snapshot_pending_visibility(tmp_path, limit=2)
    assert corrupt.outcome == "unprovable"
    assert not corrupt.paths


def test_unreadable_custody_cannot_be_reported_as_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert pending_recall.overlay(tmp_path).empty
    delta._prepare(
        tmp_path,
        batch_id="unreadable-custody",
        generation="generation-unreadable",
        paths=(delta._batch_path("Knowledge Base/Notes/pending.md", before=None, after="pending"),),
    )
    store = deferred_index.store_path(tmp_path)
    original = Path.stat

    def unreadable(path, *args, **kwargs):
        if path == store:
            raise PermissionError("custody is unreadable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", unreadable)
    snapshot = derived_receipts.snapshot_pending_visibility(tmp_path, limit=512)
    assert snapshot.outcome == "unprovable"
    assert not snapshot.paths
    assert not pending_recall.overlay(tmp_path).servable


def test_disappeared_store_does_not_erase_observed_custody(tmp_path: Path) -> None:
    rel = "Knowledge Base/Notes/pending.md"
    delta._publish_change(
        tmp_path,
        batch_id="lost-store",
        generation="generation-lost",
        changes=(
            (
                rel,
                None,
                delta._page(title="Pending", body="pendingneedle", updated="2026-01-01"),
                None,
            ),
        ),
    )
    assert pending_recall.overlay(tmp_path).covers(rel)
    deferred_index.store_path(tmp_path).unlink()
    current = pending_recall.overlay(tmp_path)
    assert not current.servable
    assert current.failure_code == "pending_visibility_unprovable"
    assert pending_recall.readonly_visibility(tmp_path) == (
        "warming",
        "pending_visibility_unprovable",
    )


def test_moving_body_fence_falls_back_to_one_fresh_footprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rel = "Knowledge Base/Notes/pending.md"
    delta._publish_change(
        tmp_path,
        batch_id="moving-fence",
        generation="generation-fence",
        changes=(
            (
                rel,
                None,
                delta._page(title="Pending", body="pendingneedle", updated="2026-01-01"),
                None,
            ),
        ),
    )
    pending_recall.reset()
    calls = []
    original = derived_receipts.snapshot_pending_visibility

    def snapshot(*args, **kwargs):
        calls.append(kwargs.get("footprint_only", False))
        return original(*args, **kwargs)

    monkeypatch.setattr(derived_receipts, "snapshot_pending_visibility", snapshot)
    monkeypatch.setattr(
        derived_receipts, "pending_visibility_snapshot_is_current", lambda *_: False
    )
    monkeypatch.setattr(
        pending_recall, "_retire_settled", lambda *_: pytest.fail("reader retired custody")
    )
    current = pending_recall.overlay(tmp_path)
    assert current.servable
    assert not current.ready
    assert current.covers(rel)
    assert current.page(rel) is None
    assert calls == [False, True]


def test_completed_overflow_retires_after_restart_without_readers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    delta._seed_corpus(tmp_path, count=1)
    delta._prime(tmp_path)
    changes = tuple(
        (
            f"Knowledge Base/Notes/burst-{index}.md",
            None,
            delta._page(title=f"Burst {index}", body="burstneedle", updated="2026-01-01"),
            None,
        )
        for index in range(513)
    )
    delta._publish_change(
        tmp_path, batch_id="completed-burst", generation="generation-burst", changes=changes
    )
    with monkeypatch.context() as guard:
        guard.setattr(pending_recall, "note_persistent_publication", lambda *_: None)
        delta._publish_lanes(tmp_path, tuple(change[0] for change in changes))
    for _ in range(2):
        claims = derived_receipts.claim_ready_components(
            tmp_path, owner="crash-worker", limit=1, now=100.0, lease_seconds=30
        )
        assert len(claims) == 1
        assert derived_receipts.complete_component(
            tmp_path, claims[0], current_generation="generation-burst", now=100.0
        )
    assert derived_receipts.snapshot_pending_visibility(tmp_path, limit=512).outcome == "overflow"
    pending_recall.reset()
    original = derived_receipts.snapshot_pending_visibility

    def targeted_snapshot(*args, **kwargs):
        assert kwargs.get("batch_ids") or kwargs.get("rel_paths"), "worker used reader hydration"
        return original(*args, **kwargs)

    monkeypatch.setattr(derived_receipts, "snapshot_pending_visibility", targeted_snapshot)
    derived_drain.drain_once(
        tmp_path,
        dispatch=lambda *_: pytest.fail("completed batch dispatched"),
        observe_current_generation=lambda _: "generation-burst",
        retire_visibility=derived_drain.pending_visibility_retirer(),
        limit=1,
        now=101.0,
    )
    assert not original(tmp_path, limit=512).paths


@pytest.mark.parametrize("mixed", [False, True])
def test_delete_crash_replays_the_removal_owner(tmp_path: Path, mixed: bool) -> None:
    removed = "Knowledge Base/Notes/removed.md"
    before = delta._page(title="Removed", body="deletedneedle", updated="2026-01-01")
    delta._write(tmp_path, removed, before)
    delta._prime(tmp_path)
    changes = [(removed, before, None, None)]
    if mixed:
        changes.append(
            (
                "Knowledge Base/Notes/created.md",
                None,
                delta._page(title="Created", body="newneedle", updated="2026-02-01"),
                None,
            )
        )
    delta._publish_change(
        tmp_path, batch_id="delete-crash", generation="generation-delete", changes=tuple(changes)
    )
    index_sync.reset_derived_fanout_memo()
    derived_drain.drain_once(
        tmp_path,
        dispatch=derived_drain.component_dispatcher(),
        observe_current_generation=lambda _: "generation-delete",
        visibility_publisher=pending_recall.publish,
        retire_visibility=derived_drain.pending_visibility_retirer(),
        limit=16,
        now=time.time(),
    )
    assert lexstore.holds_content_identities(tmp_path, {removed: None})[removed]
    assert memory_refs.holds_content_identities(tmp_path, {removed: None})[removed]
    if mixed:
        created = "Knowledge Base/Notes/created.md"
        expected = delta._digest((tmp_path / created).read_text())
        assert lexstore.holds_content_identities(tmp_path, {created: expected})[created]
    assert not derived_receipts.snapshot_pending_visibility(tmp_path, limit=512).paths


def test_failed_unproven_prefix_backs_off_and_later_commit_recovers(tmp_path: Path) -> None:
    before = delta._page(title="Before", body="old", updated="2026-01-01")
    after = delta._page(title="After", body="new", updated="2026-02-01")
    first, second = "Knowledge Base/Notes/first.md", "Knowledge Base/Notes/second.md"
    delta._write(tmp_path, first, before)
    delta._write(tmp_path, second, before)
    receipt = delta._prepare(
        tmp_path,
        batch_id="prefix-unproven",
        generation="generation-prefix",
        now=9.0,
        paths=(
            delta._batch_path(first, before=before, after=after),
            delta._batch_path(second, before=before, after=after),
        ),
    )
    delta._write(tmp_path, first, after)
    later = "Knowledge Base/Notes/later.md"
    delta._prepare(
        tmp_path,
        batch_id="later-commit",
        generation="generation-later",
        paths=(delta._batch_path(later, before=None, after=after),),
    )
    delta._write(tmp_path, later, after)
    repaired = []
    options = dict(
        observe_current_generation=lambda _: "generation-current",
        visibility_publisher=pending_recall.publish,
        converge_current=lambda *_: repaired.append(True),
        limit=1,
    )
    assert derived_receipts.recover_prepared_batches(tmp_path, now=100.0, **options) == 0
    assert (
        derived_receipts.component_status(
            tmp_path, receipt, derived_receipts.DerivedComponent.LEXSTORE
        ).next_attempt_at
        > 100.0
    )
    assert derived_receipts.recover_prepared_batches(tmp_path, now=101.0, **options) == 1
    assert not repaired, "unproven mixed bytes gained committed authority"
    with sqlite3.connect(deferred_index.store_path(tmp_path)) as connection:
        assert connection.execute(
            "SELECT proven_at FROM derived_batches WHERE batch_id = ?", (receipt.batch_id,)
        ).fetchone() == (None,)


@pytest.mark.usefixtures("managed_runtime")
@pytest.mark.parametrize("hidden_count", [1, 513])
def test_restricted_recall_discloses_no_hidden_pending_coverage(
    tmp_path: Path,
    hidden_count: int,
) -> None:
    from exomem import commands
    from exomem.governance import principal

    visible = "Knowledge Base/Notes/visible.md"
    delta._write(
        tmp_path,
        visible,
        delta._page(title="Visible", body="visible orchard", updated="2026-01-01"),
    )
    delta._seed_corpus(tmp_path)
    delta._prime(tmp_path)
    guest = principal.RequestPrincipal(
        "principal:" + "ab" * 32, surface="mcp", issuer_family="mcp-oauth:synthetic"
    )
    with principal.request_scope(guest):
        before = commands.op_ask_memory(
            tmp_path, query="orchard", mode="keyword", limit=1, graph=False
        )
    changes = tuple(
        (
            f"Knowledge Base/Notes/__exomem_raw_v1__hidden-{index}.md",
            None,
            delta._page(title="Hidden", body="orchard", updated="2026-02-01"),
            None,
        )
        for index in range(hidden_count)
    )
    delta._publish_change(
        tmp_path, batch_id="hidden-burst", generation="generation-hidden", changes=changes
    )
    delta._write(tmp_path, changes[0][0], "unprovable hidden bytes")
    pending_recall.reset()
    with principal.request_scope(guest):
        after = commands.op_ask_memory(
            tmp_path, query="orchard", mode="keyword", limit=1, graph=False
        )
    assert after == before
    assert "hidden" not in repr(after).lower()
    assert "pending" not in repr(after).lower()


@pytest.mark.parametrize("deleted", [False, True])
def test_proven_stranded_batch_repairs_current_bytes_through_existing_owner(
    tmp_path: Path,
    deleted: bool,
) -> None:
    rel = "Knowledge Base/Notes/stranded.md"
    before = delta._page(title="Stranded", body="oldneedle", updated="2026-01-01")
    delta._write(tmp_path, rel, before)
    delta._prime(tmp_path)
    delta._publish_change(
        tmp_path,
        batch_id="proven-stranded",
        generation="generation-stranded",
        changes=(
            (
                rel,
                before,
                delta._page(title="Stranded", body="intendedneedle", updated="2026-02-01"),
                None,
            ),
        ),
    )
    if deleted:
        (tmp_path / rel).unlink()
        expected = None
    else:
        current = delta._page(title="Stranded", body="currentneedle", updated="2026-03-01")
        delta._write(tmp_path, rel, current)
        expected = delta._digest(current)
    receipt = derived_receipts._load_receipt(tmp_path, "proven-stranded")
    assert (
        derived_receipts.prove_committed(
            tmp_path, receipt, current_generation="generation-current", now=100.0
        ).outcome
        == "reconcile_required"
    )
    index_sync.reset_derived_fanout_memo()
    derived_drain.drain_once(
        tmp_path,
        dispatch=derived_drain.component_dispatcher(),
        observe_current_generation=lambda _: "generation-current",
        visibility_publisher=pending_recall.publish,
        retire_visibility=derived_drain.pending_visibility_retirer(),
        limit=1,
        now=101.0,
    )
    assert lexstore.holds_content_identities(tmp_path, {rel: expected})[rel]
    assert memory_refs.holds_content_identities(tmp_path, {rel: expected})[rel]
    assert not derived_receipts.snapshot_pending_visibility(tmp_path, limit=512).paths


def test_ready_live_recovery_preserves_component_retry_backoff(tmp_path: Path) -> None:
    rel = "Knowledge Base/Notes/healthy.md"
    delta._publish_change(
        tmp_path,
        batch_id="healthy-ready",
        generation="generation-healthy",
        changes=(
            (
                rel,
                None,
                delta._page(title="Healthy", body="healthyneedle", updated="2026-01-01"),
                None,
            ),
        ),
    )
    [claim] = derived_receipts.claim_ready_components(
        tmp_path, owner="retry-owner", limit=1, now=100.0, lease_seconds=30
    )
    waiting = derived_receipts.retry_component(
        tmp_path, claim, failure_code="dispatch_failed", now=100.0
    )
    receipt = derived_receipts._load_receipt(tmp_path, "healthy-ready")
    derived_receipts.recover_prepared_batches(
        tmp_path,
        observe_current_generation=lambda _: "generation-current",
        visibility_publisher=pending_recall.publish,
        limit=1,
        now=101.0,
    )
    assert derived_receipts.component_status(tmp_path, receipt, waiting.component) == waiting


def test_mixed_service_batch_removes_tombstone_while_semantic_publication_waits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import semantic_drain

    removed = "Knowledge Base/Notes/removed.md"
    present = "Knowledge Base/Notes/present.md"
    delta._write(tmp_path, present, "# Present\n")
    receipt = SimpleNamespace(
        batch_id="service-mixed",
        canonical_generation="generation-mixed",
        paths=(
            delta._batch_path(removed, before="# Removed\n", after=None),
            delta._batch_path(present, before=None, after="# Present\n"),
        ),
    )
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_RESOURCE_POLICY", "service-v1")
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(semantic_drain, "request", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(semantic_drain, "publication_ready", lambda *_args, **_kwargs: False)
    reports = {
        operation: index_sync.IndexSyncReport(
            operation,
            (),
            (),
            (index_sync.IndexComponentOutcome("embeddings", "completed", "publication_completed"),),
        )
        for operation in ("upsert", "delete")
    }
    removed_paths = []

    def remove(_root, paths):
        removed_paths.extend(paths)
        return reports["delete"]

    monkeypatch.setattr(index_sync, "delete_after_remove", remove)
    monkeypatch.setattr(
        index_sync, "upsert_after_write", lambda *_args, **_kwargs: reports["upsert"]
    )
    index_sync.reset_derived_fanout_memo()
    assert (
        index_sync.converge_derived_component(
            tmp_path, receipt, derived_receipts.DerivedComponent.EMBEDDINGS
        )
        == "pending"
    )
    assert removed_paths == [removed]
