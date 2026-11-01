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
import os
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
from test_records_recall_graph import _manifest as _records_manifest

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
from exomem import vault as vault_module
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

    # Production workers have separate process-local recall registries. This
    # graph-loop fixture has completed lexical warm-up before its shared-process race.
    readiness.mark_ready("lexical")
    outcome: list[bool] = []
    loop = threading.Thread(
        target=lambda: outcome.append(
            service_standby.reprove_until_promoted(root, poll_seconds=0.01)
        ),
        daemon=True,
    )
    loop.start()
    try:
        EpistemicGraphIndex(root).rebuild_all()
        deadline = time.monotonic() + 30
        while service_standby.cutover_components()["graph_snapshot"] != "ready":
            assert loop.is_alive(), "the re-proof loop exited before the graph was ready"
            assert time.monotonic() < deadline, "the loop never re-proved the republished graph"
            time.sleep(0.05)
        # Ready is not the end: the loop keeps a proof current until promotion.
        assert loop.is_alive()
    finally:
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
        lambda self, conn, rels, *, resolver, owns_topology=False: set(),
        raising=True,
    )
    (root / GENERATED / "fresh-target.md").write_text(_note(501, []), encoding="utf-8")
    freshness.rebaseline(root)
    assert EpistemicGraphIndex(root).adopt_published_snapshot().adopted is True
    _drain_to_empty(root)
    with pytest.raises(AssertionError, match="missing="):
        _assert_matches_a_fresh_rebuild(root)


def test_a_snapshot_published_during_the_proof_is_held_and_promotes_as_advanced(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1: the standby holds the snapshot its proof read, not one published after it.

    The publication shows at promotion as `advanced`, so the promoted worker
    runs the handoff in full instead of the standby discarding a sound proof.
    """
    # Catches a standby that names S2 as proved, or declines a sound proof of S1.
    root = handoff_vault
    _commit_unpublished_page(root)
    _enter_standby(monkeypatch)
    real = EpistemicGraphIndex.adopt_published_snapshot
    read: list[str | None] = []

    def proof_then_publication(self, **kwargs):
        adoption = real(self, **kwargs)
        if not read:
            read.append(service_standby.snapshot_token(root))
            # The serving worker publishes S2 after the proof read S1.
            EpistemicGraphIndex(root).rebuild_all()
        return adoption

    monkeypatch.setattr(
        EpistemicGraphIndex, "adopt_published_snapshot", proof_then_publication, raising=True
    )

    assert service_standby.prove_graph_snapshot(root) is True
    assert service_standby.proved_checkpoint() == read[0], "the standby must hold S1"
    assert service_standby.snapshot_token(root) != read[0], "S2 must have been published"
    assert service_standby.cutover_components()["graph_snapshot"] == "ready"

    record = service_standby.promote(root, migrated=False)
    assert record["snapshot"] == "advanced"
    assert "graph_handoff" not in record["carried_from_standby"]


def _replace_with_identical_bytes(path: Path) -> None:
    """What a direct editor's save does: the same bytes, renamed over the page."""
    staged = path.with_name(path.name + ".editor-save")
    staged.write_bytes(path.read_bytes())
    os.replace(staged, path)


def _governed_write(root: Path, writes: dict[Path, str]) -> None:
    """One committed governed write through the lease manager a request uses."""
    from types import SimpleNamespace

    from exomem.writer_lease import get_manager, mark_active_mutation_committed

    def leaf(vault_root: Path, **_kwargs: object) -> dict[str, object]:
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(path=path, content=text) for path, text in writes.items()],
            vault_root=vault_root,
        )
        mark_active_mutation_committed()
        return {"status": "committed", "mutated": True}

    get_manager().invoke(
        SimpleNamespace(name="remember", read_only=False, leaf=leaf), (root,), {}
    )


def test_a_proof_under_live_writes_folds_them_and_converges_after_promotion(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live upgrade: a governed write, a direct save and a deletion during the proof."""
    # Catches a proof that declines while writers commit (`proof_raised` on a replaced
    # page, `snapshot_published_during_proof`), or adopts a residue missing a moved page.
    from exomem import warmup

    root = handoff_vault
    _enter_standby(monkeypatch)
    freshness.rebaseline(root)
    s1 = service_standby.snapshot_token(root)
    replaced = [f"{GENERATED}/generated-note-0001.md", f"{GENERATED}/generated-note-0002.md"]
    saved = f"{GENERATED}/generated-note-0003.md"
    deleted = f"{GENERATED}/generated-note-0199.md"
    trigger = f"{GENERATED}/generated-note-0010.md"
    created = f"{GENERATED}/created-during-proof.md"
    real_read = vault_module.read_bounded_guarded_bytes
    read: list[str] = []
    premise: list[bool] = []

    def writers_during_the_proof(vault_root, target, **kwargs):
        result = real_read(vault_root, target, **kwargs)
        read.append(target)
        if target == trigger and not premise:
            # Recorded, not asserted: the proof would catch an assertion as `proof_raised`.
            premise.append(set(replaced) | {saved} <= set(read) and deleted not in read)
            _governed_write(
                root,
                {
                    **{
                        root / rel: (root / rel).read_text(encoding="utf-8") + "\n- governed\n"
                        for rel in replaced
                    },
                    root / created: _note(9998, ["generated-note-0004", "generated-note-0005"]),
                },
            )
            # S2 lands before the proof reads on: the rebuild this process registered
            # for the write, then the repair the write queued.
            premise.append(graph_sync.drain_active_rebuilds(timeout=120.0))
            index_sync.drain_graph_work(root, limit=64)
            _replace_with_identical_bytes(root / saved)
            (root / deleted).unlink()
        return result

    monkeypatch.setattr(
        vault_module, "read_bounded_guarded_bytes", writers_during_the_proof, raising=True
    )

    assert service_standby.prove_graph_snapshot(root) is True, service_standby.adoption_record()
    assert premise == [True, True], "the writers must run mid-proof and publish S2"
    assert service_standby.snapshot_token(root) != s1, "the governed write must publish S2"
    assert service_standby.proved_checkpoint() == s1, "the standby must hold the S1 it proved"
    assert service_standby.adoption_record() == {"residue": 4, "reason": "adopted"}, (
        "S1 plus the residue must equal the disk: both replaced pages, the created "
        "page and the deleted page, and not the page saved with identical bytes"
    )
    monkeypatch.setattr(vault_module, "read_bounded_guarded_bytes", real_read, raising=True)

    record = service_standby.promote(root, migrated=False)
    assert record["snapshot"] == "advanced"
    assert "graph_handoff" not in record["carried_from_standby"]

    # The promoted worker seeds its registry, then its warm runs the handoff in full.
    freshness.rebaseline(root)
    monkeypatch.setattr(warmup, "warmup_enabled", lambda: True, raising=True)
    warmup.warm_graph_handoff(root)
    _drain_to_empty(root)

    assert EpistemicGraphIndex(root).available() is True
    assert _file_row(root, created)
    assert not _file_row(root, deleted)
    _assert_matches_a_fresh_rebuild(root)


def test_a_page_rewritten_after_every_read_declines_instead_of_folding_forever(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches an unbounded fold: an editor that saves a page after every read would hold
    # the proof, and the standby's warm budget, until the upgrade is discarded.
    root = handoff_vault
    _enter_standby(monkeypatch)
    freshness.rebaseline(root)
    churned = f"{GENERATED}/generated-note-0005.md"
    real_read = vault_module.read_bounded_guarded_bytes
    reads: list[str] = []

    def saved_after_every_read(vault_root, target, **kwargs):
        result = real_read(vault_root, target, **kwargs)
        if target == churned:
            reads.append(target)
            _replace_with_identical_bytes(root / churned)
        return result

    monkeypatch.setattr(
        vault_module, "read_bounded_guarded_bytes", saved_after_every_read, raising=True
    )

    assert service_standby.prove_graph_snapshot(root) is False
    assert service_standby.adoption_record()["reason"] == "sources_kept_moving"
    assert service_standby.proved_checkpoint() is None
    assert len(reads) > 1, "the proof must have re-read the page before it gave up"


def test_a_page_written_during_its_own_read_is_read_again_and_adopted_as_residue(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches a proof that settles on a round whose recheck was clean but which never
    # read one page, and adopts that page at the bytes its stale row holds.
    root = handoff_vault
    _enter_standby(monkeypatch)
    freshness.rebaseline(root)
    written = f"{GENERATED}/generated-note-0007.md"
    real_read = vault_module.read_bounded_guarded_bytes
    refused: list[str] = []

    def written_during_its_first_read(vault_root, target, **kwargs):
        if target == written and not refused:
            with (root / written).open("a", encoding="utf-8") as page:
                page.write("\n- appended by an editor\n")
            try:
                return real_read(vault_root, target, **kwargs)
            except vault_module.PathGuardError as error:
                refused.append(error.code)
                raise
        return real_read(vault_root, target, **kwargs)

    monkeypatch.setattr(
        vault_module, "read_bounded_guarded_bytes", written_during_its_first_read, raising=True
    )
    real_adopt = EpistemicGraphIndex.adopt_published_snapshot
    adoptions: list[epistemic_graph.SnapshotAdoption] = []

    def recorded(self, **kwargs):
        adoptions.append(real_adopt(self, **kwargs))
        return adoptions[-1]

    monkeypatch.setattr(EpistemicGraphIndex, "adopt_published_snapshot", recorded, raising=True)

    assert service_standby.prove_graph_snapshot(root) is True, service_standby.adoption_record()
    assert refused == [vault_module.PATH_GUARD_UNSAFE], "the write must race the page's read"
    assert written in adoptions[-1].residue
    monkeypatch.setattr(vault_module, "read_bounded_guarded_bytes", real_read, raising=True)

    service_standby.promote(root, migrated=False)
    # The promoted worker's watcher would see the editor's append; this process has none.
    freshness.rebaseline(root)
    _drain_to_empty(root)
    _assert_matches_a_fresh_rebuild(root)


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


def test_a_failed_reproof_retires_the_proof_whose_lineage_was_reseeded(
    handoff_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = handoff_vault
    _enter_standby(monkeypatch)
    monkeypatch.setattr(service_standby, "REPROVE_INTERVAL_SECONDS", 0.0, raising=True)
    assert service_standby.prove_graph_snapshot(root) is True

    _decline_on_an_oversized_residue(root, monkeypatch)
    _land_a_batch_without_fanout(root)
    assert service_standby.reprove_graph_snapshot_if_due(root) is False

    assert service_standby.cutover_components()["graph_snapshot"] == "waiting"
    assert service_standby.proved_checkpoint() is None
    record = service_standby.promote(root, migrated=False)
    assert record["snapshot"] == "unproven"
    assert "graph_handoff" not in record["carried_from_standby"]


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_reproof_interval_runs_from_the_end_of_the_last_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L1: a 50 s proof waits 30 s after it ends, not 30 s after it began."""
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

    clock.now = 1070.0  # 20 s after the end, 70 s after the start
    service_standby.reprove_graph_snapshot_if_due(tmp_path)
    assert proofs == [1000.0], "re-proved 30 s after the last proof began, not ended"
    clock.now = 1080.0  # 30 s after the end
    service_standby.reprove_graph_snapshot_if_due(tmp_path)
    assert proofs == [1000.0, 1080.0]


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
    # The proof's own read may touch the sidecar's WAL, which the first poll
    # may look at once; after that an unmoved graph is only ever stat-ed.
    assert service_standby.reprove_graph_snapshot_if_due(root) is False
    assert len(opened) <= 1
    opened.clear()
    for _ in range(5):
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


@pytest.mark.parametrize("withheld", [True, False], ids=["withheld", "published"])
def test_a_drain_does_not_record_topology_it_did_not_widen(
    vault: Path, monkeypatch: pytest.MonkeyPatch, withheld: bool
) -> None:
    """M2 review: the fingerprint describes rows, so it moves only with the rows it covers.

    A retitled page outside the indexed corpus has no graph rows, can never be
    residue, and re-targets a link on an indexed page. A drain of an unrelated
    page must not stamp the resolver's topology over it, or the next adoption
    matches, promotion retires the whole-vault marker that was the only repair,
    and the `linker -> outsider` edge a rebuild derives is gone for good.
    """
    outsider = "Outside/outsider.md"
    root = _build_small(
        vault,
        {
            outsider: "---\ntitle: Outsider Title\n---\n\nbody\n",
            LINKER: _note(500, []) + "\nSee [[Renamed Outsider]].\n",
        },
    )
    (root / outsider).write_text("---\ntitle: Renamed Outsider\n---\n\nbody\n", encoding="utf-8")
    edited = root / GENERATED / "generated-note-0002.md"
    edited.write_text(edited.read_text(encoding="utf-8") + "\n- edit\n", encoding="utf-8")
    freshness.rebaseline(root)
    real_identity = epistemic_graph._incremental_projection_identity
    calls = iter(range(1_000_000))

    def moving(vault_root):
        identity = real_identity(vault_root)
        return (identity, next(calls)) if withheld else identity

    monkeypatch.setattr(epistemic_graph, "_incremental_projection_identity", moving, raising=True)
    report = EpistemicGraphIndex(root).drain_paths([edited])
    monkeypatch.setattr(
        epistemic_graph, "_incremental_projection_identity", real_identity, raising=True
    )
    assert report["published"] is (not withheld)

    adoption = EpistemicGraphIndex(root).adopt_published_snapshot(apply_residue=False)
    if adoption.adopted:
        _assert_matches_a_fresh_rebuild(root)
    assert adoption.adopted is False
    assert adoption.reason == "resolver_topology_mismatch"


def _titled(title: str, extra: str) -> str:
    return f"---\ntype: pattern\nstatus: active\ntitle: {title}\n---\n\nbody\n{extra}\n"


@pytest.mark.parametrize("withheld", [True, False], ids=["withheld", "published"])
def test_a_drain_does_not_own_an_affected_page_s_own_retitle(
    vault: Path, monkeypatch: pytest.MonkeyPatch, withheld: bool
) -> None:
    """M2 review: widening follows the queued pages' keys, so only they explain topology.

    `linker` is rewritten as a page whose link now resolves to the created
    `fresh-target`, but its own retitle on disk was never queued and nothing
    widened to `mentioner`, which still links the old title. Owning that
    retitle would stamp the fingerprint over a `mentioner -> linker` edge a
    rebuild drops.
    """
    linker = f"{GENERATED}/linker.md"
    root = _build_small(
        vault,
        {
            linker: _titled("Linker Old", "See [[fresh-target]]."),
            f"{GENERATED}/mentioner.md": _note(600, []) + "\nSee [[Linker Old]].\n",
        },
    )
    created = root / GENERATED / "fresh-target.md"
    created.write_text(_note(501, []), encoding="utf-8")
    (root / linker).write_text(_titled("Linker New", "See [[fresh-target]]."), encoding="utf-8")
    freshness.rebaseline(root)
    real_identity = epistemic_graph._incremental_projection_identity
    calls = iter(range(1_000_000))

    def moving(vault_root):
        identity = real_identity(vault_root)
        return (identity, next(calls)) if withheld else identity

    monkeypatch.setattr(epistemic_graph, "_incremental_projection_identity", moving, raising=True)
    EpistemicGraphIndex(root).drain_paths([created])
    monkeypatch.setattr(
        epistemic_graph, "_incremental_projection_identity", real_identity, raising=True
    )

    adoption = EpistemicGraphIndex(root).adopt_published_snapshot(apply_residue=False)
    if adoption.adopted:
        _assert_matches_a_fresh_rebuild(root)
    assert adoption.adopted is False
    assert adoption.reason == "resolver_topology_mismatch"


def test_a_body_edit_of_a_linked_page_does_not_re_derive_its_linkers(vault: Path) -> None:
    """The drain's hold follows what moved, not how many pages link the queued one.

    A linker's rows read its target through resolution, existence and a `#`
    fragment. A body edit that keeps the title moves none of them, yet the drain
    re-derived every linker by path, stem and title: a hub with 1,000 linkers
    held the mutation boundary for 19.9 s on a synthetic 15,000-page vault.
    """
    hub = f"{GENERATED}/hub.md"
    root = _build_small(
        vault,
        {
            hub: _titled("Hub Title", "Hub body."),
            f"{GENERATED}/by-path.md": _note(600, []) + f"\nSee [[{hub.removesuffix('.md')}]].\n",
            f"{GENERATED}/by-stem.md": _note(601, []) + "\nSee [[hub]].\n",
            f"{GENERATED}/by-title.md": _note(602, []) + "\nSee [[Hub Title]].\n",
        },
    )
    (root / hub).write_text(_titled("Hub Title", "Hub body, edited."), encoding="utf-8")
    freshness.rebaseline(root)

    report = EpistemicGraphIndex(root).drain_paths([root / hub])

    assert report["published"] is True
    assert report["indexed"] == (hub,), "the drain re-derived pages the edit did not move"
    _assert_matches_a_fresh_rebuild(root)


def test_a_deleted_page_that_was_never_indexed_still_re_derives_its_linkers(vault: Path) -> None:
    """A relation to a Records page reads whether the page exists on disk.

    The graph never holds a Records page, so no stored row says it was there.
    While it exists the edge is suppressed; once an external delete queues it,
    a rebuild emits that edge as unresolved, and the drain has to re-derive the
    linker to match.
    """
    records = "Knowledge Base/Records/Health/sessions/2026-08-02.md"
    linker = f"{GENERATED}/records-linker.md"
    root = _build_small(
        vault,
        {
            "Knowledge Base/Records/Health/_collection.md": _records_manifest(),
            records: "# Private session\n\nPrivate.\n",
            linker: _note(600, []) + f"\n## Relations\n- derived_from [[{records.removesuffix('.md')}]]\n",
        },
    )
    (root / records).unlink()
    freshness.rebaseline(root)

    report = EpistemicGraphIndex(root).drain_paths([root / records])

    assert report["published"] is True
    _assert_matches_a_fresh_rebuild(root)


_CARRY_KEY = "recall_resolver_topology_carry"


def _meta(root: Path, key: str) -> str | None:
    connection = sqlite3.connect(epistemic_graph.sidecar_path(root))
    try:
        row = connection.execute("SELECT value FROM graph_meta WHERE key = ?", (key,)).fetchone()
    finally:
        connection.close()
    return None if row is None else str(row[0])


def _fingerprint(root: Path) -> str | None:
    return _meta(root, "recall_resolver_topology")


def _drain_as(root: Path, monkeypatch: pytest.MonkeyPatch, paths: list[Path], withheld: bool):
    real_identity = epistemic_graph._incremental_projection_identity
    calls = iter(range(1_000_000))

    def moving(vault_root):
        identity = real_identity(vault_root)
        return (identity, next(calls)) if withheld else identity

    monkeypatch.setattr(epistemic_graph, "_incremental_projection_identity", moving, raising=True)
    try:
        return EpistemicGraphIndex(root).drain_paths(paths)
    finally:
        monkeypatch.setattr(
            epistemic_graph, "_incremental_projection_identity", real_identity, raising=True
        )


def _created_pair(vault: Path, names: tuple[str, ...] = ("fresh-a", "fresh-b")) -> tuple[Path, list[Path]]:
    links = " and ".join(f"[[{name}]]" for name in names)
    root = _build_small(vault, {LINKER: _note(500, []) + f"\nSee {links}.\n"})
    pages = []
    for offset, name in enumerate(names):
        page = root / GENERATED / f"{name}.md"
        page.write_text(_note(501 + offset, []), encoding="utf-8")
        pages.append(page)
    freshness.rebaseline(root)
    return root, pages


@pytest.mark.parametrize("withheld", [True, False], ids=["withheld", "published"])
def test_a_split_pair_of_created_pages_records_topology_after_the_second_drain(
    vault: Path, monkeypatch: pytest.MonkeyPatch, withheld: bool
) -> None:
    """Carry-forward: `[a]` cannot explain `b`, but `[b]` plus `a`'s carried entry can.

    Without the carry, `a`'s row holds its new entry once `[a]` lands, so `[b]`'s
    revert never reproduces the stored fingerprint, which stays stuck until a
    whole-vault publication: the livelock again, with edges that already equal a
    rebuild.
    """
    root, (a, b) = _created_pair(vault)
    stored = _fingerprint(root)

    _drain_as(root, monkeypatch, [a], withheld)
    assert _fingerprint(root) == stored, "[a] cannot explain b"
    assert _meta(root, _CARRY_KEY) is not None, "[a]'s pre-pass entry must be carried"

    _drain_as(root, monkeypatch, [b], withheld)
    assert _fingerprint(root) != stored, "[b] plus the carry explains every change"
    assert _meta(root, _CARRY_KEY) is None, "a recorded fingerprint clears the carry"

    adoption = EpistemicGraphIndex(root).adopt_published_snapshot(apply_residue=False)
    assert adoption.adopted is True, adoption.reason
    _assert_matches_a_fresh_rebuild(root)


def test_an_adoption_between_split_drains_uses_the_carry(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A standby proving between `[a]` and `[b]` explains `a` from the carry, `b` as residue."""
    root, (a, b) = _created_pair(vault)
    _drain_as(root, monkeypatch, [a], True)

    adoption = EpistemicGraphIndex(root).adopt_published_snapshot()
    assert adoption.adopted is True, adoption.reason
    assert list(adoption.residue) == [f"{GENERATED}/fresh-b.md"]
    _drain_to_empty(root)
    _assert_matches_a_fresh_rebuild(root)


def test_a_requeued_page_keeps_its_first_carried_entry(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, (a, b) = _created_pair(vault)
    _drain_as(root, monkeypatch, [a], True)
    first = _meta(root, _CARRY_KEY)
    a.write_text(_titled("Fresh A Retitled", ""), encoding="utf-8")
    freshness.rebaseline(root)
    _drain_as(root, monkeypatch, [a], True)
    assert _meta(root, _CARRY_KEY) == first, "a re-queue must not overwrite the first entry"

    _drain_as(root, monkeypatch, [b], True)
    assert _meta(root, _CARRY_KEY) is None
    assert EpistemicGraphIndex(root).adopt_published_snapshot(apply_residue=False).adopted
    _assert_matches_a_fresh_rebuild(root)


def test_the_topology_carry_clears_on_a_whole_vault_publication(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, (a, _b) = _created_pair(vault)
    _drain_as(root, monkeypatch, [a], True)
    assert _meta(root, _CARRY_KEY) is not None
    EpistemicGraphIndex(root).rebuild_all()
    assert _meta(root, _CARRY_KEY) is None


def test_an_overflowing_topology_carry_falls_back_to_the_whole_vault_path(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(epistemic_graph, "TOPOLOGY_CARRY_LIMIT", 1, raising=True)
    root, (a, b, c) = _created_pair(vault, ("fresh-a", "fresh-b", "fresh-c"))
    stored = _fingerprint(root)
    _drain_as(root, monkeypatch, [a], True)
    assert _meta(root, _CARRY_KEY) is not None
    _drain_as(root, monkeypatch, [b], True)
    assert _meta(root, _CARRY_KEY) is None, "past the bound the record is dropped"
    _drain_as(root, monkeypatch, [c], True)
    # Without a and b's entries nothing can explain them: the fingerprint stays,
    # the proof fails closed, and a whole-vault pass is the repair, as before.
    assert _fingerprint(root) == stored
    adoption = EpistemicGraphIndex(root).adopt_published_snapshot(apply_residue=False)
    assert adoption.adopted is False
    assert adoption.reason == "resolver_topology_mismatch"


def test_a_topology_carry_survives_a_restart(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import find as find_module

    root, (a, b) = _created_pair(vault)
    stored = _fingerprint(root)
    _drain_as(root, monkeypatch, [a], True)
    # A new process: nothing process-local survives, only the sidecar.
    find_module.evict_resolver_caches(root)
    epistemic_graph.clear_publication_memos()
    freshness.rebaseline(root)

    _drain_as(root, monkeypatch, [b], False)
    assert _fingerprint(root) != stored
    assert EpistemicGraphIndex(root).adopt_published_snapshot(apply_residue=False).adopted
    _assert_matches_a_fresh_rebuild(root)


@pytest.mark.parametrize("withheld", [True, False], ids=["withheld", "published"])
def test_a_carry_is_not_recorded_over_an_unexplained_non_kb_change(
    vault: Path, monkeypatch: pytest.MonkeyPatch, withheld: bool
) -> None:
    """A carry may only stand for indexed work that will itself be drained.

    `[fresh-a]` is drained while a page outside the indexed corpus is retitled, so
    the affected `linker` is rewritten with a `linker -> outsider` edge through
    the new title. The retitle is then reverted. Had `[fresh-a]` recorded a
    carry, the next adoption would match with no residue, and the next drain
    would stamp the fingerprint, over an edge a full rebuild drops.
    """
    outsider = "Outside/outsider.md"
    root = _build_small(
        vault,
        {
            outsider: "---\ntitle: Outsider Title\n---\n\nbody\n",
            LINKER: _note(500, []) + "\nSee [[fresh-a]] and [[Renamed Outsider]].\n",
        },
    )
    stored = _fingerprint(root)
    created = root / GENERATED / "fresh-a.md"
    created.write_text(_note(501, []), encoding="utf-8")
    (root / outsider).write_text("---\ntitle: Renamed Outsider\n---\n\nbody\n", encoding="utf-8")
    freshness.rebaseline(root)
    _drain_as(root, monkeypatch, [created], withheld)
    assert _meta(root, _CARRY_KEY) is None, "an unexplained non-KB change drops the carry"

    (root / outsider).write_text("---\ntitle: Outsider Title\n---\n\nbody\n", encoding="utf-8")
    edited = root / GENERATED / "generated-note-0002.md"
    edited.write_text(edited.read_text(encoding="utf-8") + "\n- edit\n", encoding="utf-8")
    freshness.rebaseline(root)
    _drain_as(root, monkeypatch, [edited], withheld)
    assert _fingerprint(root) == stored, "no drain may stamp the fingerprint over the stale edge"

    adoption = EpistemicGraphIndex(root).adopt_published_snapshot(apply_residue=False)
    if adoption.adopted:
        _assert_matches_a_fresh_rebuild(root)
    assert adoption.adopted is False
    assert adoption.reason == "resolver_topology_mismatch"
