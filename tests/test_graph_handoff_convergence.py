"""A managed handoff converges the graph even when the serving worker cannot republish.

The 0.93.0 -> 0.95.1 upgrade on the personal cell fell back to a cold start. The
serving worker published generation 5576, an agent created a page 50 s later, and
the standby's single proof declined with `indexed_membership_differs`: a created
page was treated as a different corpus, the proof never ran again, and the warm
budget expired waiting on `graph_snapshot`. The serving worker could not have
helped: a boundary held by the same command had turned its queued repair into
whole-vault debt, and a whole-vault pass never held still under agent writes.

These tests pin the handoff that does not depend on that republish:

* created and removed pages are adopted as residue and repaired by the drain;
* a standby whose proof declined re-proves when the graph moves;
* promotion retires whole-vault debt its source proof covered;
* a whole-vault rebuild's outcome line names its reason, and a refused claim
  is reported as coalesced rather than failed.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_graph_post_handoff_writes import GENERATED, NOTE_COUNT, _build_vault, _note

from exomem import (
    deferred_index,
    epistemic_graph,
    freshness,
    graph_drain,
    graph_sync,
    index_sync,
    readiness,
    service_standby,
)
from exomem.epistemic_graph import EpistemicGraphIndex

CREATED = f"{GENERATED}/created-after-publication.md"
REMOVED = f"{GENERATED}/generated-note-0050.md"


@pytest.fixture(autouse=True)
def _clean_standby_state() -> Iterator[None]:
    service_standby.reset_for_tests()
    readiness.reset()
    yield
    service_standby.reset_for_tests()
    readiness.reset()


@pytest.fixture
def handoff_vault(vault: Path) -> Iterator[Path]:
    yield from _build_vault(vault, NOTE_COUNT)


@pytest.fixture
def whole_vault_passes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Count every whole-vault pass, whichever caller starts it."""
    passes: list[str] = []
    real = EpistemicGraphIndex._rebuild_all_off_boundary

    def counted(self, **kwargs):
        passes.append("pass")
        return real(self, **kwargs)

    monkeypatch.setattr(EpistemicGraphIndex, "_rebuild_all_off_boundary", counted, raising=True)
    return passes


class _Activation:
    def release(self) -> None:
        return None


def _enter_standby(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "_acquire_ownership", lambda: None)
    service_standby.enter_standby()
    service_standby.register_activation(_Activation())


def _commit_unpublished_page(root: Path) -> None:
    """A page the serving worker committed but never published into the graph."""
    (root / CREATED).write_text(
        _note(9999, ["generated-note-0001", "generated-note-0002"]), encoding="utf-8"
    )
    # What `service_standby.warm` does before it proves: seed this process's
    # recall registry from the disk as it is.
    freshness.rebaseline(root)


def _file_row(root: Path, rel: str) -> bool:
    connection = sqlite3.connect(epistemic_graph.sidecar_path(root))
    try:
        return (
            connection.execute(
                "SELECT 1 FROM graph_nodes WHERE path = ? AND kind = 'file'", (rel,)
            ).fetchone()
            is not None
        )
    finally:
        connection.close()


def _drain_to_empty(root: Path) -> None:
    for _ in range(12):
        if not deferred_index.list_graph_paths(root) and (
            deferred_index.graph_full_rebuild_pending(root) is None
        ):
            break
        index_sync.drain_graph_work(root, limit=64)


def test_a_page_created_after_the_last_publication_does_not_strand_the_standby(
    handoff_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    whole_vault_passes: list[str],
) -> None:
    root = handoff_vault
    _commit_unpublished_page(root)
    _enter_standby(monkeypatch)

    assert service_standby.prove_graph_snapshot(root) is True, (
        "a page created after the last publication is a bounded repair, not a "
        f"different corpus: {service_standby.adoption_record()}"
    )
    assert service_standby.cutover_components()["graph_snapshot"] == "ready"
    assert service_standby.adoption_record() == {"residue": 1, "reason": "adopted"}

    record = service_standby.promote(root, migrated=False)
    assert record["snapshot"] == "current"
    assert record["residue_applied"] == 1
    assert EpistemicGraphIndex(root).available() is False, (
        "the created page has no rows yet, so reads that need currency must refuse"
    )

    _drain_to_empty(root)

    assert deferred_index.list_graph_paths(root) == []
    assert EpistemicGraphIndex(root).available() is True
    assert _file_row(root, CREATED), "the drain never added the created page's rows"
    assert epistemic_graph.graph_drift(root) == []
    assert whole_vault_passes == [], "the handoff paid a whole-vault pass after all"


def test_a_page_removed_after_the_last_publication_is_adopted_and_its_rows_deleted(
    handoff_vault: Path, whole_vault_passes: list[str]
) -> None:
    root = handoff_vault
    assert _file_row(root, REMOVED)
    (root / REMOVED).unlink()
    freshness.rebaseline(root)

    adoption = EpistemicGraphIndex(root).adopt_published_snapshot()

    assert adoption.adopted is True, adoption.reason
    assert list(adoption.residue) == [REMOVED]
    assert EpistemicGraphIndex(root).available() is False
    assert set(deferred_index.list_graph_paths(root)) == {REMOVED}

    _drain_to_empty(root)

    assert deferred_index.list_graph_paths(root) == []
    assert EpistemicGraphIndex(root).available() is True
    assert not _file_row(root, REMOVED), "the removed page's rows survived the repair"
    assert epistemic_graph.graph_drift(root) == []
    assert whole_vault_passes == []


def test_a_page_still_on_disk_but_no_longer_indexed_still_declines(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaving the admitted set is not a residue the drain is proven to remove."""
    root = handoff_vault
    real = EpistemicGraphIndex._indexed_recall_membership

    def without_removed(self):
        membership = real(self)
        return None if membership is None else membership - {REMOVED}

    monkeypatch.setattr(
        EpistemicGraphIndex, "_indexed_recall_membership", without_removed, raising=True
    )
    adoption = EpistemicGraphIndex(root).adopt_published_snapshot()
    assert adoption.adopted is False
    assert adoption.reason == "indexed_membership_differs"


def test_a_topology_change_the_residue_does_not_explain_still_declines(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = handoff_vault
    _commit_unpublished_page(root)
    monkeypatch.setattr(
        epistemic_graph,
        "_resolver_topology_fingerprint",
        lambda _resolver: "not-the-published-topology",
        raising=True,
    )
    adoption = EpistemicGraphIndex(root).adopt_published_snapshot()
    assert adoption.adopted is False
    assert adoption.reason == "resolver_topology_mismatch"


def _decline_on_an_oversized_residue(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for i in range(3):
        page = root / GENERATED / f"generated-note-{80 + i:04d}.md"
        page.write_text(page.read_text(encoding="utf-8") + "\n- unattributed\n", encoding="utf-8")
    monkeypatch.setattr(graph_drain, "DRAIN_LIMIT", 2, raising=True)
    freshness.rebaseline(root)


def test_a_standby_reproves_after_the_serving_worker_republishes(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = handoff_vault
    _decline_on_an_oversized_residue(root, monkeypatch)
    _enter_standby(monkeypatch)
    monkeypatch.setattr(service_standby, "REPROVE_INTERVAL_SECONDS", 0.0, raising=True)

    assert service_standby.prove_graph_snapshot(root) is False
    assert service_standby.adoption_record()["reason"] == "residue_exceeds_drain_limit"
    assert service_standby.cutover_components()["graph_snapshot"] == "waiting"

    # The serving worker republishes a snapshot that covers those pages.
    EpistemicGraphIndex(root).rebuild_all()

    assert service_standby.reprove_graph_snapshot_if_due(root) is True
    assert service_standby.cutover_components()["graph_snapshot"] == "ready"
    assert service_standby.adoption_record() == {"residue": 0, "reason": "adopted"}


def test_a_waiting_standby_does_not_reprove_an_unchanged_graph(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = handoff_vault
    _decline_on_an_oversized_residue(root, monkeypatch)
    _enter_standby(monkeypatch)
    monkeypatch.setattr(service_standby, "REPROVE_INTERVAL_SECONDS", 0.0, raising=True)
    assert service_standby.prove_graph_snapshot(root) is False

    proofs: list[str] = []
    real = EpistemicGraphIndex.adopt_published_snapshot

    def traced(self, **kwargs):
        proofs.append("proof")
        return real(self, **kwargs)

    monkeypatch.setattr(EpistemicGraphIndex, "adopt_published_snapshot", traced, raising=True)
    assert service_standby.reprove_graph_snapshot_if_due(root) is False
    assert proofs == [], "nothing moved, so the O(vault) proof must not run again"


def test_a_standby_reproof_is_rate_limited(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = handoff_vault
    _decline_on_an_oversized_residue(root, monkeypatch)
    _enter_standby(monkeypatch)
    assert service_standby.REPROVE_INTERVAL_SECONDS == 30.0
    assert service_standby.prove_graph_snapshot(root) is False
    EpistemicGraphIndex(root).rebuild_all()
    # The graph moved, but the interval since the warm's own proof has not passed.
    assert service_standby.reprove_graph_snapshot_if_due(root) is False
    assert service_standby.cutover_components()["graph_snapshot"] == "waiting"


def test_the_standby_reproof_loop_ends_once_ready_or_discarded(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = handoff_vault
    _decline_on_an_oversized_residue(root, monkeypatch)
    _enter_standby(monkeypatch)
    monkeypatch.setattr(service_standby, "REPROVE_INTERVAL_SECONDS", 0.0, raising=True)
    assert service_standby.prove_graph_snapshot(root) is False

    outcome: list[bool] = []
    loop = threading.Thread(
        target=lambda: outcome.append(
            service_standby.reprove_until_ready(root, poll_seconds=0.01)
        ),
        daemon=True,
    )
    loop.start()
    EpistemicGraphIndex(root).rebuild_all()
    loop.join(timeout=60)
    assert not loop.is_alive()
    assert outcome == [True]

    # A discarded standby stops re-proving at once.
    service_standby.reset_for_tests()
    _enter_standby(monkeypatch)
    service_standby.discard()
    assert service_standby.reprove_until_ready(root, poll_seconds=0.01) is False


def test_promotion_retires_the_full_marker_its_proof_covered(
    handoff_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    whole_vault_passes: list[str],
) -> None:
    """The incident's shape: whole-vault debt standing and a page never published."""
    root = handoff_vault
    deferred_index.mark_graph_full_rebuild(root, generation=1)
    _commit_unpublished_page(root)
    _enter_standby(monkeypatch)

    assert service_standby.prove_graph_snapshot(root) is True
    record = service_standby.promote(root, migrated=False)

    assert record["full_marker"] == "retired"
    assert deferred_index.graph_full_rebuild_pending(root) is None
    _drain_to_empty(root)
    assert EpistemicGraphIndex(root).available() is True
    assert _file_row(root, CREATED)
    assert epistemic_graph.graph_drift(root) == []
    assert whole_vault_passes == [], (
        "the promoted worker's drain went back to whole-vault convergence"
    )


def test_a_migrated_promotion_retires_the_marker_its_reproof_covered(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = handoff_vault
    _commit_unpublished_page(root)
    _enter_standby(monkeypatch)
    assert service_standby.prove_graph_snapshot(root) is True
    # Raised after the warm's proof but before the promotion re-proof samples it.
    deferred_index.mark_graph_full_rebuild(root, generation=1)

    record = service_standby.promote(root, migrated=True)

    assert record["reproved"] is True
    assert record["full_marker"] == "retired"
    assert deferred_index.graph_full_rebuild_pending(root) is None


def test_debt_raised_after_the_proof_survives_promotion(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = handoff_vault
    deferred_index.mark_graph_full_rebuild(root, generation=1)
    _commit_unpublished_page(root)
    _enter_standby(monkeypatch)
    assert service_standby.prove_graph_snapshot(root) is True
    # A repeat raise keeps the value but moves the raise count.
    deferred_index.mark_graph_full_rebuild(root, generation=1)

    record = service_standby.promote(root, migrated=False)

    assert record["full_marker"] == "retained"
    assert deferred_index.graph_full_rebuild_pending(root) is not None


def _checkpoint() -> graph_sync.GraphSyncCheckpoint:
    return graph_sync.GraphSyncCheckpoint.create(
        generation=7, mutation_id="0" * 24, paths=(), created_paths=(), scope="full"
    )


def test_a_refused_rebuild_claim_is_logged_as_coalesced_with_its_reason(
    handoff_vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = handoff_vault
    index = EpistemicGraphIndex(root)
    state_root = index._mutation_coordinator.state_root
    probe = index.path.with_name(".graph-rebuild-test-holder.sqlite")
    assert graph_sync.claim_rebuild_owner(root, probe, state_root=state_root)
    caplog.set_level("INFO", logger="exomem.epistemic_graph")
    try:
        with pytest.raises(graph_sync.GraphRebuildInProgress):
            epistemic_graph._rebuild_outcome(index, _checkpoint())
        with pytest.raises(graph_sync.GraphRebuildInProgress):
            EpistemicGraphIndex(root).rebuild_all()
    finally:
        graph_sync.release_rebuild_owner(root, probe, state_root=state_root)

    finished = [line for line in caplog.messages if "graph rebuild finished" in line]
    assert len(finished) == 2, finished
    for line in finished:
        assert "outcome=coalesced" in line, line
        assert "reason=GRAPH_SYNC_REBUILD_IN_PROGRESS" in line, line
        assert "outcome=failed" not in line


def test_a_failed_rebuild_names_its_reason(
    handoff_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = handoff_vault

    def defeated(self, **_kwargs):
        raise epistemic_graph.GraphPublicationUnavailable("defeated by recorded writes")

    monkeypatch.setattr(EpistemicGraphIndex, "_rebuild_all_off_boundary", defeated, raising=True)
    caplog.set_level("INFO", logger="exomem.epistemic_graph")
    with pytest.raises(epistemic_graph.GraphPublicationUnavailable):
        epistemic_graph._rebuild_outcome(EpistemicGraphIndex(root), _checkpoint())
    with pytest.raises(epistemic_graph.GraphPublicationUnavailable):
        EpistemicGraphIndex(root).rebuild_all()

    finished = [line for line in caplog.messages if "graph rebuild finished" in line]
    assert len(finished) == 2, finished
    for line in finished:
        assert "outcome=failed" in line
        assert "reason=GRAPH_SYNC_PUBLICATION_UNAVAILABLE" in line


def test_a_direct_rebuild_logs_its_published_outcome(
    handoff_vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Start-up validation and the reconcile path call `rebuild_all` directly."""
    caplog.set_level("INFO", logger="exomem.epistemic_graph")
    EpistemicGraphIndex(handoff_vault).rebuild_all()
    finished = [line for line in caplog.messages if "graph rebuild finished" in line]
    assert len(finished) == 1 and "outcome=published" in finished[0], finished


def test_the_drain_names_the_barrier_branch_when_it_queues_whole_vault_debt(
    handoff_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = handoff_vault
    monkeypatch.setattr(graph_drain, "_queue_pending", lambda _root: False, raising=True)
    monkeypatch.setattr(graph_drain, "_barrier_pending", lambda _root: True, raising=True)
    monkeypatch.setattr(graph_drain, "_recover_once", lambda _root: False, raising=True)
    monkeypatch.setattr(freshness, "external_pending", lambda _root: True, raising=True)
    monkeypatch.setattr(
        epistemic_graph, "publication_refusal_active", lambda _root: False, raising=True
    )
    caplog.set_level("INFO", logger="exomem.graph_drain")

    assert graph_drain._work_once(root) == 1

    queued = [line for line in caplog.messages if "queued a whole-vault rebuild" in line]
    assert len(queued) == 1, caplog.messages
    assert "no barrier" not in queued[0], queued[0]
    assert "unpublished external epoch" in queued[0], queued[0]
