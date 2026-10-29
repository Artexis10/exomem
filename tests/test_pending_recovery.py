"""Incomplete pending content must not disable unrelated admitted recall."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
import test_pending_recall_delta as delta
import yaml
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
    writer_lease,
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
    # The moved page's body is unproven, so its current bytes settle identity: it
    # no longer holds `identity`, and the stable page stays the one owner.
    with pytest.raises(memory_refs.ReferenceError, match="memory id not found"):
        memory_refs.ReferenceIndex(tmp_path).resolve(identity)
    assert memory_refs.ReferenceIndex(tmp_path).resolve(stable_identity) == stable
    assert memory_refs.paths_for_ids_read_only(tmp_path, [stable_identity]) == {
        stable_identity: (stable,)
    }

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


def test_footprint_fallback_is_rehydrated_by_the_next_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rel = "Knowledge Base/Notes/pending.md"
    delta._publish_change(
        tmp_path,
        batch_id="healing-fence",
        generation="generation-healing",
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
    with monkeypatch.context() as racing:
        racing.setattr(
            derived_receipts, "pending_visibility_snapshot_is_current", lambda *_: False
        )
        assert not pending_recall.overlay(tmp_path).ready
    healed = pending_recall.overlay(tmp_path)
    assert healed.ready
    assert healed.page(rel) is not None


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


def test_slow_committed_repair_keeps_unrelated_canonical_write_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rel = "Knowledge Base/Notes/stranded.md"
    delta._publish_change(
        tmp_path,
        batch_id="slow-repair",
        generation="generation-repair",
        changes=(
            (
                rel,
                None,
                delta._page(title="Stranded", body="committed", updated="2026-01-01"),
                None,
            ),
        ),
    )
    delta._write(tmp_path, rel, delta._page(title="Stranded", body="current", updated="2026-02-01"))
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "leases"),
        mutation_timeout_seconds=0.1,
    )
    monkeypatch.setattr(writer_lease, "active_manager", lambda: manager)
    entered, release = Event(), Event()

    def blocked_owner(*_args):
        entered.set()
        assert release.wait(timeout=5), "repair test did not release its owner"
        return False

    with ThreadPoolExecutor(max_workers=1) as worker:
        recovery = worker.submit(
            derived_receipts.recover_prepared_batches,
            tmp_path,
            observe_current_generation=lambda _: "generation-current",
            visibility_publisher=pending_recall.publish,
            converge_current=blocked_owner,
            limit=1,
            now=100.0,
        )
        try:
            assert entered.wait(timeout=5), "recovery did not enter its derived owner"
            with manager.consistency_guard(tmp_path, operation="unrelated_canonical_write"):
                unrelated = "Knowledge Base/Notes/unrelated.md"
                delta._write(tmp_path, unrelated, "# Unrelated write\n")
                assert not release.is_set()
        finally:
            release.set()
            try:
                assert recovery.result(timeout=5) == 0
            finally:
                manager.close()
    assert (tmp_path / unrelated).read_text() == "# Unrelated write\n"
    receipt = derived_receipts._load_receipt(tmp_path, "slow-repair")
    assert receipt.state == "reconcile_required"
    assert derived_receipts.snapshot_pending_visibility(tmp_path, limit=512).paths == {rel}
    assert (
        derived_receipts.component_status(
            tmp_path, receipt, derived_receipts.DerivedComponent.LEXSTORE
        ).next_attempt_at
        > 100.0
    )


@pytest.mark.usefixtures("managed_runtime")
@pytest.mark.parametrize(
    ("ceiling", "file_release"),
    [(0, None), (2, None), (None, None), (0, "same"), (0, "other")],
)
def test_governed_identity_uncertainty_obeys_the_callers_release_floor(
    tmp_path: Path,
    ceiling: int | None,
    file_release: str | None,
) -> None:
    from test_derived_identifier_egress import _govern

    from exomem import commands, plan_progress
    from exomem.governance import bridges, egress, policy, principal

    identity = "12345678-1234-4234-8234-123456789012"
    ref = memory_refs.memory_ref(identity)
    visible = "Knowledge Base/Notes/visible.md"
    pending = (
        "Knowledge Base/Notes/Withheld/pending.md"
        if ceiling is not None
        else "Knowledge Base/Notes/pending.md"
    )
    guest = principal.RequestPrincipal(
        "principal:" + "ab" * 32, surface="mcp", issuer_family="mcp-oauth:synthetic"
    )
    with principal.library_scope():
        delta._write(
            tmp_path,
            visible,
            delta._page(
                title="Visible", body="released orchard", updated="2026-01-01", exomem_id=identity
            ),
        )
        _govern(tmp_path, guest.audience_id)
        if ceiling is not None:
            rule = tmp_path / "Knowledge Base/_Governance/rules/withheld.yaml"
            rule.write_text(rule.read_text().replace("ceiling: 0", f"ceiling: {ceiling}"))
        if file_release is not None:
            import test_governance_bridges as bridge_cases

            source = "Knowledge Base/Notes/Withheld/source.md"
            delta._write(tmp_path, source, bridge_cases._bridge_source_text())
            delta._write(tmp_path, pending, bridge_cases._bridge_text(source_path=source))
            bridge = tmp_path / pending
            grant = yaml.safe_load(bridge_cases._release_document())
            grant["path"] = pending
            grant["content_hash"] = hashlib.sha256(bridge.read_bytes()).hexdigest()
            if file_release == "same":
                grant["to_audience"] = guest.audience_id
            grant["bridge_of"][0].update(
                path=source,
                content_hash=hashlib.sha256((tmp_path / source).read_bytes()).hexdigest(),
                restriction_signature=bridge_cases._restriction_signature(
                    tmp_path, audience=grant["to_audience"]
                ),
            )
            grant_path = tmp_path / "Knowledge Base/_Governance/grants/bridge-release.yaml"
            grant_path.parent.mkdir(exist_ok=True)
            grant_path.write_text(yaml.safe_dump(grant), encoding="utf-8")
            compiled = policy.load(tmp_path)
            assert not compiled.blocked
            assert bridges.admit(
                tmp_path, pending, bridge.read_bytes(),
                policy=compiled, audience=guest.audience_id,
            ).allowed is (file_release == "same")
            assert egress.release_level_for(tmp_path, pending, principal=guest) == 0
        delta._prime(tmp_path)
    command = next(
        command for command in commands.PRODUCT_COMMANDS if command.name == "read_memory"
    )
    with principal.request_scope(guest):
        before = writer_lease.invoke_command(command, tmp_path, path=ref)
        supersession = plan_progress._supersession(tmp_path, [identity])
        assert supersession == {identity: False}
    with principal.library_scope():
        delta._publish_change(
            tmp_path,
            batch_id="pending-identity",
            generation="generation-identity",
            changes=(
                (
                    pending,
                    None,
                    delta._page(title="Pending", body="pending", updated="2026-02-01"),
                    None,
                ),
            ),
        )
        # Undecodable bytes stay unprovable, so only the caller's release floor
        # decides whether the pending page is admitted and refuses.
        (tmp_path / pending).write_bytes(b"\xff")
        pending_recall.reset()
    with principal.request_scope(guest):
        if ceiling == egress.LEVEL_NONE:
            assert writer_lease.invoke_command(command, tmp_path, path=ref) == before
            assert egress.visible_memory_ref_paths(tmp_path, [ref]) == {ref: visible}
        else:
            with pytest.raises(find.RetrievalIndexWarming):
                writer_lease.invoke_command(command, tmp_path, path=ref)
            with pytest.raises(find.RetrievalIndexWarming):
                egress.visible_memory_ref_paths(tmp_path, [ref])
        if ceiling is not None:
            assert plan_progress._supersession(tmp_path, [identity]) == supersession
        else:
            with pytest.raises(find.RetrievalIndexWarming):
                plan_progress._supersession(tmp_path, [identity])
    with principal.library_scope(), pytest.raises(find.RetrievalIndexWarming):
        memory_refs.paths_for_ids_read_only(tmp_path, [identity])


@pytest.mark.usefixtures("managed_runtime")
@pytest.mark.parametrize(
    ("blocked_bytes", "outcome"),
    [
        (
            delta._page(title="Blocked", body="no identity", updated="2026-03-01").encode(),
            "commits",
        ),
        (
            delta._page(
                title="Blocked",
                body="claims the id",
                updated="2026-03-01",
                exomem_id="87654321-4321-4321-8321-210987654321",
            ).encode(),
            "AMBIGUOUS_REFERENCE",
        ),
        (b"\xff", "warming"),
    ],
    ids=["no-id", "duplicate-id", "undecodable"],
)
def test_edit_by_reference_settles_unprovable_pending_pages_from_canonical_bytes(
    tmp_path: Path, blocked_bytes: bytes, outcome: str
) -> None:
    from exomem import commands

    identity = "87654321-4321-4321-8321-210987654321"
    stable = "Knowledge Base/Notes/stable.md"
    blocked = "Knowledge Base/Notes/blocked.md"
    delta._write(
        tmp_path,
        stable,
        delta._page(title="Stable", body="Before.", updated="2026-01-01", exomem_id=identity),
    )
    delta._prime(tmp_path)
    delta._publish_change(
        tmp_path,
        batch_id="blocked-page",
        generation="generation-blocked",
        changes=(
            (
                blocked,
                None,
                delta._page(title="Blocked", body="pending", updated="2026-02-01"),
                None,
            ),
        ),
    )
    (tmp_path / blocked).write_bytes(blocked_bytes)
    pending_recall.reset()
    original = (tmp_path / stable).read_bytes()
    command = next(
        command for command in commands.PRODUCT_COMMANDS if command.name == "edit_memory"
    )

    def edit() -> object:
        return writer_lease.invoke_command(
            command,
            tmp_path,
            path=memory_refs.memory_ref(identity),
            why="Correct the stable page.",
            operation={"kind": "replace_string", "old_string": "Before.", "new_string": "After."},
        )

    if outcome == "commits":
        edit()
        assert b"After." in (tmp_path / stable).read_bytes()
        return
    if outcome == "AMBIGUOUS_REFERENCE":
        with pytest.raises(ValueError, match=outcome):
            edit()
    else:
        with pytest.raises(find.RetrievalIndexWarming):
            edit()
    assert (tmp_path / stable).read_bytes() == original


@pytest.mark.usefixtures("managed_runtime")
@pytest.mark.parametrize(
    "authority", ["no-grant", "session-grant", "unavailable", "standing", "canonical"]
)
def test_pending_identity_admission_retains_unproved_byte_bound_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authority: str,
) -> None:
    from exomem.governance import egress, policy, principal

    path = "Knowledge Base/Notes/pending.md"
    scope = policy.Scope(id="scope", source="synthetic", paths=("Notes/**",))
    compiled = policy.Policy(
        fingerprint="a" * 64,
        scopes={scope.id: scope},
        rules=(
            policy.Rule(
                id="rule", source="synthetic", scope_ids=(scope.id,), audience="guest", ceiling=0
            ),
        ),
        grants=(
            policy.StandingGrant(
                id="grant",
                source="synthetic",
                scope_ids=(scope.id,),
                audience="guest",
                ceiling=egress.LEVEL_FULL,
            ),
        )
        if authority == "standing"
        else (),
    )
    context = egress.authorization_session_lifecycle.AuthorizationSessionContext(
        session_id="session",
        principal_id="guest",
        issuer_family="synthetic",
        cell_id="cell",
        logical_vault_id="vault",
        keyring_id="keyring",
        credential_generation=1,
        expires_at=4_000_000_000,
    )
    guest = principal.RequestPrincipal(
        "guest",
        verified_authorization_session=context
        if authority in {"no-grant", "session-grant", "unavailable"}
        else None,
    )
    monkeypatch.setattr(egress.policy_module, "load", lambda _: compiled)
    monkeypatch.setattr(
        egress, "bound_writer", lambda _: object() if authority == "canonical" else None
    )
    monkeypatch.setattr(egress, "_declared_purpose", lambda *_: None)
    monkeypatch.setattr(
        egress.store,
        "open_authorization_session_connection",
        lambda _: SimpleNamespace(close=lambda: None),
    )

    def grants(_connection, **kwargs):
        if authority == "unavailable":
            raise egress.authorization_session_lifecycle.AuthorizationSessionUnavailable()
        if authority == "session-grant":
            assert kwargs["resolve"](path) is None
        return ()

    monkeypatch.setattr(
        egress.authorization_session_authority,
        "active_session_grants_for_projection_resolver",
        grants,
    )
    assert egress.pending_identity_candidates(tmp_path, [path], principal=guest) == (
        () if authority == "no-grant" else (path,)
    )


@pytest.mark.usefixtures("managed_runtime")
def test_overflow_hidden_identity_admission_never_reads_pending_bodies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_derived_identifier_egress import _govern

    from exomem.governance import egress, principal

    identity = "12345678-1234-4234-8234-123456789012"
    visible = "Knowledge Base/Notes/visible.md"
    guest = principal.RequestPrincipal(
        "principal:" + "ab" * 32, surface="mcp", issuer_family="mcp-oauth:synthetic"
    )
    with principal.library_scope():
        delta._write(
            tmp_path,
            visible,
            delta._page(
                title="Visible",
                body="released",
                updated="2026-01-01",
                exomem_id=identity,
            ),
        )
        _govern(tmp_path, guest.audience_id)
        delta._prime(tmp_path)
        changes = tuple(
            (
                f"Knowledge Base/Notes/Withheld/pending-{index}.md",
                None,
                delta._page(title="Pending", body="pending", updated="2026-02-01"),
                None,
            )
            for index in range(pending_recall.PENDING_HYDRATION_LIMIT + 1)
        )
        delta._publish_change(
            tmp_path, batch_id="hidden-overflow", generation="generation-overflow", changes=changes
        )
        pending_recall.reset()
    pending_paths = {path for path, *_ in changes}
    reads = set()
    admitting = False
    original_read = Path.read_bytes
    original_admit = egress.pending_identity_candidates

    def read(path):
        if admitting and path.is_relative_to(tmp_path):
            relative = path.relative_to(tmp_path).as_posix()
            if relative in pending_paths:
                reads.add(relative)
        return original_read(path)

    def admit(*args, **kwargs):
        nonlocal admitting
        admitting = True
        try:
            return original_admit(*args, **kwargs)
        finally:
            admitting = False

    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr(egress, "pending_identity_candidates", admit)
    with principal.request_scope(guest):
        assert (
            egress.resolve_visible_identifier(tmp_path, memory_refs.memory_ref(identity)) == visible
        )
    assert reads == set()


@pytest.mark.usefixtures("managed_runtime")
def test_pending_keyword_lane_keeps_its_finite_candidate_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance.principal import library_scope

    for index in range(400):
        delta._write(
            tmp_path,
            f"Knowledge Base/Notes/match-{index:03d}.md",
            delta._page(title=f"Match {index}", body="sharedneedle", updated="2026-01-01"),
        )
    delta._prime(tmp_path)
    delta._publish_change(
        tmp_path,
        batch_id="unrelated-pending",
        generation="generation-pending",
        changes=(
            (
                "Knowledge Base/Notes/pending.md",
                None,
                delta._page(title="Pending", body="other text", updated="2026-02-01"),
                None,
            ),
        ),
    )
    calls = []
    original = lexstore.search_substring_result

    def search(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append((kwargs.get("k"), len(result.value or ())))
        return result

    monkeypatch.setattr(lexstore, "search_substring_result", search)
    with library_scope():
        hits = find.find(
            tmp_path,
            query="sharedneedle",
            scope="kb-only",
            mode="hybrid",
            limit=5,
            graph=False,
            rerank=False,
        )
    assert len(hits) == 5
    assert calls and all(bound is not None and count <= bound < 400 for bound, count in calls)


@pytest.mark.parametrize("matched", [False, True])
def test_publication_notice_reads_only_custody_for_its_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    matched: bool,
) -> None:
    original = derived_receipts._connect_receipt_read
    steps = 0

    def connect(root):
        connection = original(root)

        def progress():
            nonlocal steps
            steps += 1
            return 0

        connection.set_progress_handler(progress, 1)
        return connection

    monkeypatch.setattr(derived_receipts, "_connect_receipt_read", connect)
    delta._prepare(
        tmp_path,
        batch_id="small-backlog",
        generation="generation-backlog",
        paths=tuple(
            delta._batch_path(
                f"Knowledge Base/Notes/small-{index}.md", before=None, after="pending"
            )
            for index in range(200)
        ),
    )
    published = "Knowledge Base/Notes/small-0.md" if matched else "Knowledge Base/Notes/settled.md"
    steps = 0
    pending_recall.note_persistent_publication(tmp_path, "lexstore", [published])
    small = steps
    delta._prepare(
        tmp_path,
        batch_id="large-backlog",
        generation="generation-backlog",
        paths=tuple(
            delta._batch_path(
                f"Knowledge Base/Notes/large-{index}.md", before=None, after="pending"
            )
            for index in range(2000)
        ),
    )
    steps = 0
    pending_recall.note_persistent_publication(tmp_path, "lexstore", [published])
    assert steps <= small + 64, (small, steps)


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


@pytest.mark.usefixtures("managed_runtime")
@pytest.mark.parametrize("floor", ["raw", "connector"])
def test_independent_pending_floor_preserves_a_released_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    floor: str,
) -> None:
    from test_derived_identifier_egress import SCOPE_ID, _govern

    from exomem import commands
    from exomem.governance import egress, principal

    identity = "12345678-1234-4234-8234-123456789012"
    ref = memory_refs.memory_ref(identity)
    visible = "Knowledge Base/Notes/visible.md"
    name = f"{egress.raw_protection.PREFIX}pending.md" if floor == "raw" else "pending.md"
    hidden = f"Knowledge Base/Notes/Withheld/{name}"
    guest = principal.RequestPrincipal("guest", surface="mcp", issuer_family="synthetic")
    with principal.library_scope():
        delta._write(
            tmp_path,
            visible,
            delta._page(title="Visible", body="released", updated="2026-01-01", exomem_id=identity),
        )
        _govern(tmp_path, guest.audience_id)
        rule = tmp_path / "Knowledge Base/_Governance/rules/withheld.yaml"
        rule.write_text(rule.read_text().replace("ceiling: 0", f"ceiling: {egress.LEVEL_FULL}"))
        delta._prime(tmp_path)
    if floor == "connector":
        boundary = egress.connector_boundary.Snapshot("synthetic", frozenset((SCOPE_ID,)), (), ())
        monkeypatch.setattr(
            egress.connector_boundary, "snapshot", lambda *_args, **_kwargs: boundary
        )
    command = next(
        command for command in commands.PRODUCT_COMMANDS if command.name == "read_memory"
    )
    with principal.request_scope(guest):
        before = writer_lease.invoke_command(command, tmp_path, path=ref)
    with principal.library_scope():
        delta._publish_change(
            tmp_path,
            batch_id="hidden-floor",
            generation="generation-floor",
            changes=(
                (
                    hidden,
                    None,
                    delta._page(title="Hidden", body="pending", updated="2026-02-01"),
                    None,
                ),
            ),
        )
        delta._write(tmp_path, hidden, "unprovable pending bytes")
        pending_recall.reset()
    with principal.request_scope(guest):
        assert writer_lease.invoke_command(command, tmp_path, path=ref) == before
        assert egress.visible_memory_ref_paths(tmp_path, [ref]) == {ref: visible}


@pytest.mark.parametrize("release", ["none", "unrelated", "applicable", "unknown-authority"])
def test_raw_pending_admission_proves_only_metadata_denials_without_body_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    release: str,
) -> None:
    from exomem.governance import policy, principal, raw_protection

    path = f"Knowledge Base/Notes/{raw_protection.PREFIX}artifact.txt"
    guest = principal.RequestPrincipal("guest", surface="mcp", issuer_family="synthetic")
    grant = SimpleNamespace(
        path=(path if release == "applicable" else path + "-other") + ".md",
        to_audience=guest.audience_id,
        raw_protection={
            "surface": guest.surface,
            "issuer_family": guest.issuer_family,
            "purpose": None,
        },
    )
    compiled = policy.Policy(
        fingerprint="a" * 64, release_grants=() if release == "none" else (grant,)
    )
    monkeypatch.setattr(policy, "load", lambda _: compiled)
    monkeypatch.setattr(
        raw_protection,
        "binding",
        lambda *_args, **_kwargs: pytest.fail("no-byte RAW admission opened a binding"),
    )
    if release == "unknown-authority":
        guest = principal.RequestPrincipal("guest")
    assert raw_protection.permits(tmp_path, path, guest, resolve_bytes=False) is (
        None if release in {"applicable", "unknown-authority"} else False
    )
