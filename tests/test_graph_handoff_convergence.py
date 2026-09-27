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

import logging
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_graph_post_handoff_writes import (
    GENERATED,
    NOTE_COUNT,
    _build_vault,
    _note,
    _seed_live_freshness,
)

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
LINKER = f"{GENERATED}/linker.md"
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


def _build_small(vault: Path, extra: dict[str, str]) -> Path:
    """A 40-note vault plus `extra` pages, published, for the edge oracle."""
    generated = vault / GENERATED
    generated.mkdir(parents=True, exist_ok=True)
    names = [f"generated-note-{i:04d}" for i in range(40)]
    for i, name in enumerate(names):
        links = [names[(i + offset) % 40] for offset in (1, 7)]
        (generated / f"{name}.md").write_text(_note(i, links), encoding="utf-8")
    for rel, text in extra.items():
        page = vault / rel
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(text, encoding="utf-8")
    _seed_live_freshness(vault)
    EpistemicGraphIndex(vault).rebuild_all()
    epistemic_graph.clear_publication_memos()
    return vault


def _graph_rows(root: Path) -> tuple[set[tuple[object, ...]], list[tuple[object, ...]]]:
    connection = sqlite3.connect(epistemic_graph.sidecar_path(root))
    try:
        edges = connection.execute(
            "SELECT src_key, dst_key, relation_type, origin, source_path, "
            "COALESCE(source_anchor, ''), metadata FROM graph_edges"
        ).fetchall()
        files = connection.execute(
            "SELECT path, source_hash FROM graph_nodes WHERE kind = 'file' ORDER BY 1"
        ).fetchall()
    finally:
        connection.close()
    return set(edges), files


def _assert_matches_a_fresh_rebuild(root: Path) -> None:
    """Edges on every page, not only the residue's, equal a whole-vault rebuild."""
    drained_edges, drained_files = _graph_rows(root)
    EpistemicGraphIndex(root).rebuild_all()
    rebuilt_edges, rebuilt_files = _graph_rows(root)
    assert drained_files == rebuilt_files
    missing = sorted(map(str, rebuilt_edges - drained_edges))
    extra = sorted(map(str, drained_edges - rebuilt_edges))
    assert not missing and not extra, f"missing={missing[:5]} extra={extra[:5]}"


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
    _assert_matches_a_fresh_rebuild(root)


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
    _assert_matches_a_fresh_rebuild(root)


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


def test_an_unexplained_non_kb_title_change_alongside_a_residue_declines(
    vault: Path,
) -> None:
    """A page outside the indexed corpus has no stored title to revert to.

    Its title still moves wikilink resolution for indexed pages, so a change to
    it is a topology change the residue cannot explain, even beside a residue
    that is otherwise bounded.
    """
    outsider = "Outside/outsider.md"
    root = _build_small(
        vault,
        {
            outsider: "---\ntitle: Outsider Title\n---\n\nbody\n",
            LINKER: _note(500, []) + "\nSee [[Outsider Title]] and [[Renamed Outsider]].\n",
        },
    )
    index = EpistemicGraphIndex(root)
    assert outsider in (index._recall_membership() or ()), "the resolver must see it"
    assert outsider not in (index._indexed_recall_membership() or ())

    (root / GENERATED / "residue-created.md").write_text(_note(700, []), encoding="utf-8")
    (root / outsider).write_text("---\ntitle: Renamed Outsider\n---\n\nbody\n", encoding="utf-8")
    freshness.rebaseline(root)

    adoption = EpistemicGraphIndex(root).adopt_published_snapshot(apply_residue=False)
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


def test_the_standby_reproof_loop_runs_until_promotion_or_discard(
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
            service_standby.reprove_until_promoted(root, poll_seconds=0.01)
        ),
        daemon=True,
    )
    loop.start()
    EpistemicGraphIndex(root).rebuild_all()
    deadline = time.monotonic() + 30
    while service_standby.cutover_components()["graph_snapshot"] != "ready":
        assert loop.is_alive(), "the re-proof loop exited before the graph was ready"
        assert time.monotonic() < deadline, "the loop never re-proved the republished graph"
        time.sleep(0.05)
    # Ready is not the end: the loop keeps a proof current until promotion.
    assert loop.is_alive()
    service_standby.discard()
    loop.join(timeout=60)
    assert not loop.is_alive()
    assert outcome == [True], "the loop ends holding the proof it made"

    # A discarded standby stops re-proving at once.
    service_standby.reset_for_tests()
    _enter_standby(monkeypatch)
    service_standby.discard()
    assert service_standby.reprove_until_promoted(root, poll_seconds=0.01) is False


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


def _fresh_target(root: Path) -> None:
    (root / GENERATED / "fresh-target.md").write_text(_note(501, []), encoding="utf-8")
    (root / GENERATED / "other.md").write_text(
        "---\ntype: pattern\nstatus: active\ntitle: Fresh Title\n---\n\n# Fresh Title\n\nbody\n",
        encoding="utf-8",
    )


def _ambiguous_stem(root: Path) -> None:
    other = root / "Knowledge Base" / "Other"
    other.mkdir(parents=True, exist_ok=True)
    (other / "generated-note-0003.md").write_text(_note(502, []), encoding="utf-8")


def _rename(root: Path) -> None:
    (root / GENERATED / "generated-note-0005.md").rename(root / GENERATED / "renamed-five.md")


def _remove_linked(root: Path) -> None:
    (root / GENERATED / "generated-note-0006.md").unlink()


def _retitle(root: Path) -> None:
    (root / GENERATED / "titled.md").write_text(
        "---\ntype: pattern\nstatus: active\ntitle: Alpha Title\n---\n\nbody\n", encoding="utf-8"
    )


_EDGE_SHAPES = {
    # A created page that existing links now resolve to, by stem and by title.
    "created_gains_incoming_links": (
        _note(500, []) + "\nSee [[fresh-target]] and [[Fresh Title]].\n",
        _fresh_target,
    ),
    "created_makes_a_stem_ambiguous": (
        _note(500, []) + "\nSee [[generated-note-0003]].\n",
        _ambiguous_stem,
    ),
    "renamed_linked_page": (
        _note(500, []) + "\nSee [[generated-note-0005]] and [[renamed-five]].\n",
        _rename,
    ),
    "removed_linked_page": (_note(500, []) + "\nSee [[generated-note-0006]].\n", _remove_linked),
    "retitled_page_retargets_links": (_note(500, []) + "\nSee [[Alpha Title]].\n", _retitle),
}


@pytest.mark.parametrize("shape", sorted(_EDGE_SHAPES))
def test_an_adopted_residue_drains_to_the_edges_of_a_fresh_rebuild(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    whole_vault_passes: list[str],
    shape: str,
) -> None:
    linker_text, mutate = _EDGE_SHAPES[shape]
    extra = {LINKER: linker_text}
    if shape == "retitled_page_retargets_links":
        extra[f"{GENERATED}/titled.md"] = (
            "---\ntype: pattern\nstatus: active\ntitle: Beta Title\n---\n\nbody\n"
        )
    root = _build_small(vault, extra)
    whole_vault_passes.clear()
    mutate(root)
    freshness.rebaseline(root)
    _enter_standby(monkeypatch)

    assert service_standby.prove_graph_snapshot(root) is True, service_standby.adoption_record()
    service_standby.promote(root, migrated=False)
    _drain_to_empty(root)

    assert EpistemicGraphIndex(root).available() is True
    assert whole_vault_passes == []
    _assert_matches_a_fresh_rebuild(root)


def test_the_edge_oracle_sees_a_drain_that_does_not_widen(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: without widening, the oracle must find the missing edge."""
    root = _build_small(vault, {LINKER: _note(500, []) + "\nSee [[fresh-target]].\n"})
    monkeypatch.setattr(
        EpistemicGraphIndex,
        "_topology_affected_sources",
        lambda self, conn, rels, *, resolver: set(),
        raising=True,
    )
    (root / GENERATED / "fresh-target.md").write_text(_note(501, []), encoding="utf-8")
    freshness.rebaseline(root)
    assert EpistemicGraphIndex(root).adopt_published_snapshot().adopted is True
    _drain_to_empty(root)
    with pytest.raises(AssertionError, match="missing="):
        _assert_matches_a_fresh_rebuild(root)


def test_a_snapshot_published_during_the_proof_is_not_named_as_proved(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1: the proof covers the snapshot it opened, not one published after it."""
    root = handoff_vault
    _commit_unpublished_page(root)
    _enter_standby(monkeypatch)
    monkeypatch.setattr(service_standby, "REPROVE_INTERVAL_SECONDS", 0.0, raising=True)
    real = EpistemicGraphIndex.adopt_published_snapshot
    published: list[str] = []

    def proof_then_publication(self, **kwargs):
        adoption = real(self, **kwargs)
        if not published:
            # The serving worker publishes S2 after the proof read S1.
            EpistemicGraphIndex(root).rebuild_all()
            published.append("S2")
        return adoption

    monkeypatch.setattr(
        EpistemicGraphIndex, "adopt_published_snapshot", proof_then_publication, raising=True
    )

    assert service_standby.prove_graph_snapshot(root) is False
    assert service_standby.proved_checkpoint() is None
    assert service_standby.cutover_components()["graph_snapshot"] == "waiting"

    # The published snapshot moved, so the waiting standby proves S2 itself.
    assert service_standby.reprove_graph_snapshot_if_due(root) is True
    assert service_standby.proved_checkpoint() == service_standby.snapshot_token(root)
    assert service_standby.adoption_record() == {"residue": 0, "reason": "adopted"}


def _land_a_batch_without_fanout(root: Path) -> None:
    """Canonical bytes and a new checkpoint generation; the sidecar is untouched."""
    from exomem import vault as vault_module

    page = root / GENERATED / "generated-note-0011.md"
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(page, page.read_text(encoding="utf-8") + "\n- landed\n")],
        vault_root=root,
        post_commit_fanout=False,
    )


def _durable_generation(root: Path) -> int | None:
    checkpoint = graph_sync.read_checkpoint(root)
    return None if checkpoint is None else checkpoint.generation


def test_a_full_marker_raised_before_its_batch_lands_is_not_retired_by_an_earlier_proof(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OQ1: a full-scope batch marks its debt before its bytes land."""
    root = handoff_vault
    deferred_index.mark_graph_full_rebuild(root, generation=1)
    _commit_unpublished_page(root)
    _enter_standby(monkeypatch)
    before = _durable_generation(root)
    assert service_standby.prove_graph_snapshot(root) is True

    _land_a_batch_without_fanout(root)
    assert _durable_generation(root) != before, "the batch must move the durable generation"
    record = service_standby.promote(root, migrated=False)

    assert record["snapshot"] == "current", "the sidecar did not move, only the bytes"
    assert record["full_marker"] == "retained"
    assert deferred_index.graph_full_rebuild_pending(root) is not None


def test_a_proved_standby_keeps_reproving_while_the_graph_moves(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L2: a proof that saw the batch's bytes may retire the marker after all."""
    root = handoff_vault
    deferred_index.mark_graph_full_rebuild(root, generation=1)
    _commit_unpublished_page(root)
    _enter_standby(monkeypatch)
    monkeypatch.setattr(service_standby, "REPROVE_INTERVAL_SECONDS", 0.0, raising=True)
    assert service_standby.prove_graph_snapshot(root) is True

    _land_a_batch_without_fanout(root)
    freshness.rebaseline(root)
    assert service_standby.reprove_graph_snapshot_if_due(root) is True
    assert service_standby.adoption_record() == {"residue": 2, "reason": "adopted"}

    record = service_standby.promote(root, migrated=False)
    assert record["full_marker"] == "retired"


def test_a_failed_reproof_keeps_the_proof_already_held(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = handoff_vault
    _enter_standby(monkeypatch)
    monkeypatch.setattr(service_standby, "REPROVE_INTERVAL_SECONDS", 0.0, raising=True)
    assert service_standby.prove_graph_snapshot(root) is True
    held = service_standby.proved_checkpoint()

    _decline_on_an_oversized_residue(root, monkeypatch)
    _land_a_batch_without_fanout(root)
    assert service_standby.reprove_graph_snapshot_if_due(root) is False

    assert service_standby.cutover_components()["graph_snapshot"] == "ready"
    assert service_standby.proved_checkpoint() == held
    assert service_standby.adoption_record() == {"residue": 0, "reason": "adopted"}


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_reproof_interval_runs_from_the_end_of_the_last_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L1: a 50 s proof waits max(30 s, 50 s) after it ends, not 30 s after it began."""
    clock = _Clock()
    monkeypatch.setattr(service_standby, "_clock", clock, raising=True)
    moves = iter(range(1_000_000))
    monkeypatch.setattr(service_standby, "_graph_stat", lambda _root: next(moves), raising=True)
    monkeypatch.setattr(
        service_standby, "_graph_snapshot_signal", lambda _root: (next(moves), "t"), raising=True
    )
    proofs: list[float] = []

    def slow_decline(self, **_kwargs):
        proofs.append(clock.now)
        clock.now += 50.0
        return epistemic_graph.SnapshotAdoption(False, reason="resolver_topology_mismatch")

    monkeypatch.setattr(EpistemicGraphIndex, "adopt_published_snapshot", slow_decline, raising=True)
    _enter_standby(monkeypatch)
    assert service_standby.prove_graph_snapshot(tmp_path) is False  # 1000 -> 1050

    clock.now = 1080.0  # 30 s after the end, 80 s after the start
    service_standby.reprove_graph_snapshot_if_due(tmp_path)
    assert proofs == [1000.0], "re-proved 30 s after the last proof ended, not 50"
    clock.now = 1100.0  # max(30, 50) s after the end
    service_standby.reprove_graph_snapshot_if_due(tmp_path)
    assert proofs == [1000.0, 1100.0]


def test_an_unchanged_graph_is_polled_without_opening_the_sidecar(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L1: the once-a-second poll is two stats when nothing moved."""
    root = handoff_vault
    _decline_on_an_oversized_residue(root, monkeypatch)
    _enter_standby(monkeypatch)
    monkeypatch.setattr(service_standby, "REPROVE_INTERVAL_SECONDS", 0.0, raising=True)
    assert service_standby.prove_graph_snapshot(root) is False
    opened: list[str] = []
    real = service_standby._read_snapshot_token

    def counted(vault_root, *, quiet):
        opened.append("sidecar")
        return real(vault_root, quiet=quiet)

    monkeypatch.setattr(service_standby, "_read_snapshot_token", counted, raising=True)
    for _ in range(3):
        assert service_standby.reprove_graph_snapshot_if_due(root) is False
    assert opened == []


def test_an_unknown_full_rebuild_cause_is_logged_as_given(
    handoff_vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """L3: a new call site's cause must never fail the enqueue it follows."""
    caplog.set_level(logging.INFO, logger="exomem.graph_drain")
    assert graph_drain._request_full_rebuild(handoff_vault, cause="a_new_cause") is True
    assert any("a_new_cause" in line for line in caplog.messages), caplog.messages


def test_a_promotion_that_raises_does_not_leave_the_standby_promoting(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L4: a promotion that failed must not stop the standby proving."""
    root = handoff_vault
    _enter_standby(monkeypatch)
    assert service_standby.prove_graph_snapshot(root) is True

    def refused() -> None:
        raise RuntimeError("lease refused")

    monkeypatch.setattr(service_standby, "_acquire_ownership", refused, raising=True)
    with pytest.raises(RuntimeError, match="lease refused"):
        service_standby.promote(root, migrated=False)

    assert service_standby.promoted() is False
    assert service_standby.prove_graph_snapshot(root) is True


def test_rows_an_unpublished_drain_landed_carry_their_topology(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M2: a drain that withholds publication still records the topology of its rows.

    Otherwise the rows gain the created page while the stored fingerprint does
    not, and the next adoption proof declines a snapshot that matches the disk.
    """
    root = _build_small(vault, {})
    created = root / GENERATED / "drained-unpublished.md"
    created.write_text(_note(800, []), encoding="utf-8")
    freshness.rebaseline(root)
    real_identity = epistemic_graph._incremental_projection_identity
    calls = iter(range(1_000_000))

    def moving(vault_root):
        # Never equal twice: the vault moved elsewhere under the drain.
        return (real_identity(vault_root), next(calls))

    monkeypatch.setattr(epistemic_graph, "_incremental_projection_identity", moving, raising=True)
    report = EpistemicGraphIndex(root).drain_paths([created])
    monkeypatch.setattr(
        epistemic_graph, "_incremental_projection_identity", real_identity, raising=True
    )
    assert report["published"] is False
    assert _file_row(root, f"{GENERATED}/drained-unpublished.md")

    adoption = EpistemicGraphIndex(root).adopt_published_snapshot(apply_residue=False)
    assert adoption.adopted is True, adoption.reason
    assert adoption.residue == ()
