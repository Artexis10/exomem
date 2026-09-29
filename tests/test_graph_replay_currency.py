"""A replayed path outside the recall delta is proved against the graph first.

On the live 0.96.0 worker the periodic reconcile replayed deferred full-index
receipts through the graph's standalone refresh. Every replayed page had changed
long before the graph's stored checkpoint, so it lay outside the recall delta, the
refresh fell back with `caller_path_outside_delta`, and a standalone caller paid an
in-process whole-vault rebuild -- 59-101 s each, one per isolated receipt, with
concurrent requests at 4-5 s p95.

These tests pin the proof that replaces that fallback: a page the registry records
exactly as the disk has it is either already reflected by its stored row (a no-op)
or stale work the drain repairs incrementally; only a page the registry does not
vouch for still rebuilds the vault. Every case is compared edge for edge with a
fresh whole-vault rebuild.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest
from test_graph_handoff_convergence import (
    GENERATED,
    _build_small,
    _graph_rows,
    _note,
    _titled,
)

from exomem import deferred_index, epistemic_graph, freshness, index_sync
from exomem.epistemic_graph import EpistemicGraphIndex

REPLAYED = f"{GENERATED}/generated-note-0003.md"
RETITLED = f"{GENERATED}/retitled.md"
# One page per title, so the retitle drops one edge and adds the other: a drain
# that does not widen to the retitle's dependants leaves both visibly wrong.
OLD_LINKER = f"{GENERATED}/old-linker.md"
NEW_LINKER = f"{GENERATED}/new-linker.md"
# A page with semantic units, whose graph rows carry a projection generation.
UNITS = f"{GENERATED}/units.md"


def _replay(root: Path, paths: list[Path], **kwargs) -> epistemic_graph.GraphDispatchResult:
    """The graph dispatch as the deferred-receipt replay calls it."""
    return epistemic_graph.upsert_after_write(root, paths, replayed=True, **kwargs)


def _projection_rows(root: Path) -> dict[str, set[tuple[object, ...]]]:
    """The rows a replay's repair can get wrong beyond edges: titles and dependencies."""
    connection = sqlite3.connect(epistemic_graph.sidecar_path(root))
    try:
        nodes = connection.execute("SELECT kind, path, title FROM graph_nodes").fetchall()
        dependencies = connection.execute(
            "SELECT source_path, lookup_key, raw_target FROM graph_dependencies"
        ).fetchall()
    finally:
        connection.close()
    return {"nodes": set(nodes), "dependencies": set(dependencies)}


def _assert_matches_a_fresh_rebuild(root: Path) -> None:
    """Edges, file hashes, nodes and dependencies all equal a whole-vault rebuild."""
    drained_edges, drained_files = _graph_rows(root)
    drained = _projection_rows(root)
    EpistemicGraphIndex(root).rebuild_all()
    rebuilt_edges, rebuilt_files = _graph_rows(root)
    rebuilt = _projection_rows(root)
    assert drained_files == rebuilt_files
    missing = sorted(map(str, rebuilt_edges - drained_edges))
    extra = sorted(map(str, drained_edges - rebuilt_edges))
    assert not missing and not extra, f"missing={missing[:5]} extra={extra[:5]}"
    for table in ("nodes", "dependencies"):
        missing = sorted(map(str, rebuilt[table] - drained[table]))
        extra = sorted(map(str, drained[table] - rebuilt[table]))
        assert not missing and not extra, f"{table}: missing={missing[:5]} extra={extra[:5]}"


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


def _built(vault: Path, whole_vault_passes: list[str]) -> Path:
    root = _build_small(
        vault,
        {
            OLD_LINKER: _note(500, []) + "\nSee [[Old Title]].\n",
            NEW_LINKER: _note(501, []) + "\nSee [[New Title]].\n",
            RETITLED: _titled("Old Title", ""),
            UNITS: _titled("Units", "- [config] first observation ^first\n- [rule] second one ^second"),
        },
    )
    whole_vault_passes.clear()
    return root


def _publish_past_a_stale_row(root: Path) -> None:
    """Leave `retitled.md` with a stale row under a checkpoint that already includes it.

    The 7.6 shape: the registry records the retitle, and a drain of another page
    publishes the marker at the registry's checkpoint, so the retitle is behind
    the stored checkpoint -- outside the recall delta -- while its own receipt
    has not drained.
    """
    (root / RETITLED).write_text(_titled("New Title", ""), encoding="utf-8")
    other = root / GENERATED / "generated-note-0007.md"
    other.write_text(other.read_text(encoding="utf-8") + "\n- edit\n", encoding="utf-8")
    freshness.rebaseline(root)
    report = EpistemicGraphIndex(root).drain_paths([other])
    assert report["published"] is True


def test_a_replayed_path_the_graph_already_reflects_rebuilds_nothing(
    vault: Path, whole_vault_passes: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    root = _built(vault, whole_vault_passes)
    before = _graph_rows(root)
    caplog.set_level(logging.INFO, logger="exomem.epistemic_graph")

    result = _replay(root, [root / REPLAYED])

    assert whole_vault_passes == [], "a replay the graph already reflects rebuilt the vault"
    assert "caller_path_outside_delta" not in caplog.text
    assert result.outcome in {"completed", "not_required"}, result
    assert _graph_rows(root) == before
    assert EpistemicGraphIndex(root).available() is True
    _assert_matches_a_fresh_rebuild(root)


def test_a_caller_that_is_not_a_replay_still_refreshes_an_unchanged_page(
    vault: Path, whole_vault_passes: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    """Reconcile and explicit repair refresh unchanged pages to reproject them.

    The currency proof is for replayed deferred receipts only; any other caller
    naming a page outside the delta keeps the whole-vault fallback.
    """
    root = _built(vault, whole_vault_passes)
    caplog.set_level(logging.INFO, logger="exomem.epistemic_graph")

    epistemic_graph.upsert_after_write(root, [root / REPLAYED])

    assert "reason=caller_path_outside_delta" in caplog.text
    assert len(whole_vault_passes) == 1
    _assert_matches_a_fresh_rebuild(root)


def _unit_generations(root: Path, rel: str) -> set[str]:
    connection = sqlite3.connect(epistemic_graph.sidecar_path(root))
    try:
        rows = connection.execute(
            "SELECT metadata FROM graph_nodes WHERE path = ?", (rel,)
        ).fetchall()
    finally:
        connection.close()
    return {
        str(metadata["parent_generation"])
        for (raw,) in rows
        if (metadata := json.loads(raw)).get("record_type") == "semantic_unit"
    }


def test_a_replayed_page_with_stale_generation_unit_rows_is_repaired(
    vault: Path, whole_vault_passes: list[str]
) -> None:
    """The file row's source hash is not the page's whole projection."""
    root = _built(vault, whole_vault_passes)
    current = _unit_generations(root, UNITS)
    assert current and "stale-generation" not in current
    connection = sqlite3.connect(epistemic_graph.sidecar_path(root))
    try:
        for node_key, raw in connection.execute(
            "SELECT node_key, metadata FROM graph_nodes WHERE path = ?", (UNITS,)
        ).fetchall():
            metadata = json.loads(raw)
            if metadata.get("record_type") == "semantic_unit":
                metadata["parent_generation"] = "stale-generation"
                connection.execute(
                    "UPDATE graph_nodes SET metadata = ? WHERE node_key = ?",
                    (json.dumps(metadata, sort_keys=True), node_key),
                )
        connection.commit()
    finally:
        connection.close()
    deferred_index.add_full_receipts(root, [UNITS])

    assert index_sync.drain_deferred_work(root, paths=[root / UNITS]) == 1

    assert _unit_generations(root, UNITS) == current, "the stale unit rows were kept"
    _assert_matches_a_fresh_rebuild(root)


def test_a_replayed_full_index_receipt_the_graph_reflects_rebuilds_nothing(
    vault: Path, whole_vault_passes: list[str]
) -> None:
    """The live path: a deferred full-index receipt replayed by the drain."""
    root = _built(vault, whole_vault_passes)
    deferred_index.add_full_receipts(root, [REPLAYED])

    assert index_sync.drain_deferred_work(root, paths=[root / REPLAYED]) == 1

    assert deferred_index.snapshot_full(root) == []
    assert whole_vault_passes == []
    _assert_matches_a_fresh_rebuild(root)


def test_a_current_replayed_path_does_not_hold_back_the_delta(
    vault: Path, whole_vault_passes: list[str]
) -> None:
    root = _built(vault, whole_vault_passes)
    edited = root / GENERATED / "generated-note-0010.md"
    edited.write_text(edited.read_text(encoding="utf-8") + "\n- edited\n", encoding="utf-8")
    # Recorded as an event, so the edited page is in the recall delta while the
    # replayed page is not; the refresh must still derive the delta.
    freshness.on_files_changed(root, changed=[edited])
    # A serving worker holds its recall resolver resident; a cold one is a
    # separate, queued deferral this test is not about.
    from exomem import find as find_module

    find_module.recall_resolver_snapshot(root)

    _replay(root, [root / REPLAYED, edited])

    assert whole_vault_passes == []
    assert EpistemicGraphIndex(root).available() is True
    _assert_matches_a_fresh_rebuild(root)


def test_a_replayed_stale_page_is_drained_incrementally(
    vault: Path, whole_vault_passes: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    root = _built(vault, whole_vault_passes)
    _publish_past_a_stale_row(root)
    caplog.set_level(logging.INFO, logger="exomem.epistemic_graph")

    _replay(root, [root / RETITLED])

    assert whole_vault_passes == [], "a stale page the registry vouches for rebuilt the vault"
    assert "caller_path_outside_delta" not in caplog.text
    assert EpistemicGraphIndex(root).available() is True
    # The retitle moves an edge from one linker to the other; the drain widens to both.
    _assert_matches_a_fresh_rebuild(root)


def test_a_replayed_page_the_registry_does_not_vouch_for_still_rebuilds(
    vault: Path, whole_vault_passes: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    root = _built(vault, whole_vault_passes)
    _publish_past_a_stale_row(root)
    # Changed again behind the registry's back: its record no longer matches disk.
    (root / RETITLED).write_text(_titled("Newer Title", "moved again"), encoding="utf-8")
    caplog.set_level(logging.INFO, logger="exomem.epistemic_graph")

    _replay(root, [root / RETITLED])

    assert "reason=caller_path_outside_delta" in caplog.text
    assert len(whole_vault_passes) == 1
    _assert_matches_a_fresh_rebuild(root)


def test_a_replayed_stale_page_does_not_publish_past_a_standing_full_marker(
    vault: Path, whole_vault_passes: list[str]
) -> None:
    """The drain's first rule: a standing full marker drains the queue with it.

    The replay's repair is the drain's repair, so it must not serve reads as
    current while whole-vault debt is outstanding: either the graph stays
    unavailable or the marker's debt is paid.
    """
    root = _built(vault, whole_vault_passes)
    _publish_past_a_stale_row(root)
    deferred_index.advance_graph_full_rebuild(root)
    EpistemicGraphIndex(root)._mark_unavailable()
    whole_vault_passes.clear()

    _replay(root, [root / RETITLED])

    if EpistemicGraphIndex(root).available():
        assert deferred_index.graph_full_rebuild_pending(root) is None, (
            "reads served as current while the whole-vault debt stands"
        )
        assert whole_vault_passes, "the full marker's debt was never paid"
        assert deferred_index.snapshot_graph(root) == [], "left receipts for a duplicate drain"
        _assert_matches_a_fresh_rebuild(root)
    else:
        assert deferred_index.graph_full_rebuild_pending(root) is not None


def test_a_replayed_stale_page_is_not_repaired_against_an_unsettled_epoch(
    vault: Path, whole_vault_passes: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drain's second rule: per-path repair needs a lineage it can classify."""
    root = _built(vault, whole_vault_passes)
    _publish_past_a_stale_row(root)
    drained: list[list[Path]] = []
    real_drain = EpistemicGraphIndex.drain_paths

    def recorded(self, paths, *args, **kwargs):
        drained.append(list(paths))
        return real_drain(self, paths, *args, **kwargs)

    monkeypatch.setattr(EpistemicGraphIndex, "drain_paths", recorded, raising=True)
    monkeypatch.setattr(
        EpistemicGraphIndex, "epoch_admits_incremental_repair", lambda self: False, raising=True
    )

    _replay(root, [root / RETITLED])

    assert drained == [], "repaired paths against a lineage it could not classify"
    if EpistemicGraphIndex(root).available():
        assert whole_vault_passes, "published without repairing the stale row"
        _assert_matches_a_fresh_rebuild(root)


def test_a_replayed_stale_page_clears_its_own_receipts(
    vault: Path, whole_vault_passes: list[str]
) -> None:
    """The drain's third rule: receipts the repair covered clear by CAS."""
    root = _built(vault, whole_vault_passes)
    _publish_past_a_stale_row(root)

    _replay(root, [root / RETITLED])

    assert whole_vault_passes == []
    assert deferred_index.snapshot_graph(root) == [], "left receipts for a duplicate drain"
    _assert_matches_a_fresh_rebuild(root)


def test_the_oracle_sees_a_replay_drain_that_does_not_widen(
    vault: Path, whole_vault_passes: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: the stale-page test must be able to see missing widening.

    The retitle drops one linker's edge and gives the other one; a drain that
    re-derives only the retitled page leaves both linkers as they were, and the
    comparison with a fresh rebuild has to say so.
    """
    root = _built(vault, whole_vault_passes)
    _publish_past_a_stale_row(root)
    monkeypatch.setattr(
        EpistemicGraphIndex,
        "_topology_affected_sources",
        lambda self, conn, rels, *, resolver: set(),
        raising=True,
    )

    _replay(root, [root / RETITLED])

    assert whole_vault_passes == [], "a rebuild healed what the control must expose"
    with pytest.raises(AssertionError):
        _assert_matches_a_fresh_rebuild(root)


def test_a_stale_page_named_through_a_vault_alias_is_not_judged_current(
    vault: Path, whole_vault_passes: list[str], tmp_path: Path
) -> None:
    """The proof reads the page under the vault's own spelling, not the caller's.

    A page with no row yet, named through a symlinked alias of the vault, is not
    a recall candidate under the alias spelling; judged by that spelling it
    looks like a non-page with no row -- current -- and the replay drops it.
    """
    root = _built(vault, whole_vault_passes)
    created = root / GENERATED / "created-late.md"
    created.write_text(_note(502, []) + "\nSee [[Old Title]].\n", encoding="utf-8")
    other = root / GENERATED / "generated-note-0007.md"
    other.write_text(other.read_text(encoding="utf-8") + "\n- edit\n", encoding="utf-8")
    freshness.rebaseline(root)
    assert EpistemicGraphIndex(root).drain_paths([other])["published"] is True
    alias = tmp_path / "vault-alias"
    alias.symlink_to(root, target_is_directory=True)

    _replay(root, [alias / GENERATED / "created-late.md"])

    rel = f"{GENERATED}/created-late.md"
    assert any(path == rel for path, _ in _graph_rows(root)[1]), "the stale page was dropped"
    _assert_matches_a_fresh_rebuild(root)


def test_a_current_created_path_outside_the_delta_does_not_rebuild_the_vault(
    vault: Path, whole_vault_passes: list[str]
) -> None:
    """A fan-out replays its batch's created pages too; a current one owes nothing."""
    root = _built(vault, whole_vault_passes)
    edited = root / GENERATED / "generated-note-0010.md"
    edited.write_text(edited.read_text(encoding="utf-8") + "\n- edited\n", encoding="utf-8")
    freshness.on_files_changed(root, changed=[edited])
    from exomem import find as find_module

    find_module.recall_resolver_snapshot(root)
    replayed_creation = root / NEW_LINKER

    _replay(
        root, [replayed_creation, edited], created_paths=[replayed_creation]
    )

    assert whole_vault_passes == [], "a current created page outside the delta rebuilt the vault"
    assert EpistemicGraphIndex(root).available() is True
    _assert_matches_a_fresh_rebuild(root)


@pytest.fixture
def caller_whole_vault_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Whole-vault work started while a mutation request is the caller."""
    from exomem import writer_lease

    calls: list[str] = []

    def spied(name: str, owner, attr: str) -> None:
        real = getattr(owner, attr)

        def spy(*args, **kwargs):
            if writer_lease.active_mutation_request_id() is not None:
                calls.append(name)
            return real(*args, **kwargs)

        monkeypatch.setattr(owner, attr, spy, raising=True)

    spied("converge_full_graph_marker", epistemic_graph, "converge_full_graph_marker")
    spied("rebuild_all", EpistemicGraphIndex, "rebuild_all")
    spied("rebuild_all_off_boundary", EpistemicGraphIndex, "_rebuild_all_off_boundary")
    return calls


def _replay_under_a_mutation_request(root: Path) -> epistemic_graph.GraphDispatchResult:
    from exomem import writer_lease

    trace = writer_lease._ACTIVE_MUTATION_TRACE.set(("request", "command", "receipt"))
    try:
        return _replay(root, [root / RETITLED])
    finally:
        writer_lease._ACTIVE_MUTATION_TRACE.reset(trace)


def _assert_the_daemon_drains_it_once(root: Path) -> None:
    queued = {entry.rel_path for entry in deferred_index.snapshot_graph(root)}
    assert RETITLED in queued, "the replay's receipt was not left for the daemon"
    assert index_sync.drain_graph_work(root) >= 1
    assert deferred_index.snapshot_graph(root) == []
    assert index_sync.drain_graph_work(root) == 0, "the receipt drained twice"
    assert EpistemicGraphIndex(root).available() is True
    _assert_matches_a_fresh_rebuild(root)


def test_a_request_replay_leaves_a_standing_full_marker_to_the_daemon(
    vault: Path, whole_vault_passes: list[str], caller_whole_vault_calls: list[str]
) -> None:
    """A caller that can report pending does not pay the marker's debt inline."""
    root = _built(vault, whole_vault_passes)
    _publish_past_a_stale_row(root)
    deferred_index.advance_graph_full_rebuild(root)
    EpistemicGraphIndex(root)._mark_unavailable()

    result = _replay_under_a_mutation_request(root)

    assert caller_whole_vault_calls == [], "a whole-vault pass ran on the request's thread"
    assert result.code != "incremental_completed", result
    assert deferred_index.graph_full_rebuild_pending(root) is not None
    assert EpistemicGraphIndex(root).available() is False
    _assert_the_daemon_drains_it_once(root)


def test_a_request_replay_leaves_an_unsettled_epoch_to_the_daemon(
    vault: Path,
    whole_vault_passes: list[str],
    caller_whole_vault_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The epoch refuses per-path repair; the request does not rebuild instead."""
    root = _built(vault, whole_vault_passes)
    _publish_past_a_stale_row(root)
    with monkeypatch.context() as unsettled:
        unsettled.setattr(
            EpistemicGraphIndex,
            "epoch_admits_incremental_repair",
            lambda self: False,
            raising=True,
        )
        result = _replay_under_a_mutation_request(root)

    assert caller_whole_vault_calls == [], "a whole-vault pass ran on the request's thread"
    assert result.code != "incremental_completed", result
    assert EpistemicGraphIndex(root).available() is False
    _assert_the_daemon_drains_it_once(root)


def test_a_standalone_replay_that_rebuilds_does_not_report_incremental_completion(
    vault: Path, whole_vault_passes: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that must converge still pays inline, and says it did."""
    root = _built(vault, whole_vault_passes)
    _publish_past_a_stale_row(root)
    monkeypatch.setattr(
        EpistemicGraphIndex, "epoch_admits_incremental_repair", lambda self: False, raising=True
    )

    result = _replay(root, [root / RETITLED])

    assert whole_vault_passes, "a standalone caller must still converge"
    assert result.code != "incremental_completed", result
    assert result.whole_vault_attempted, result


def _terminal_graph_diagnostic(
    monkeypatch: pytest.MonkeyPatch, result: epistemic_graph.GraphDispatchResult
) -> dict[str, object]:
    """The graph's entry in the request terminal's component diagnostics."""
    from exomem import writer_lease

    captured: dict[str, object] = {}

    def capture(terminal, **kwargs):
        captured.update(kwargs)
        return terminal

    monkeypatch.setattr(writer_lease, "with_fast_acknowledgement", capture, raising=True)
    report = index_sync.IndexSyncReport(
        "upsert",
        (RETITLED,),
        (RETITLED,),
        (index_sync._graph_component(lambda: result),),
    )
    writer_lease._with_post_terminal_fanout_acknowledgement(
        {"state": "committed"}, [report], drain_failed=False
    )
    (graph,) = [
        entry
        for entry in captured["component_diagnostics"]
        if entry["component"] == "epistemic_graph"
    ]
    return dict(graph)


@pytest.mark.parametrize("marker", [False, True], ids=["epoch", "marker"])
def test_a_request_replay_deferral_under_an_acknowledged_checkpoint_dispatches_as_queued(
    vault: Path,
    whole_vault_passes: list[str],
    caller_whole_vault_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
    marker: bool,
) -> None:
    """The live state: a durable checkpoint the acknowledgement covers, graph available.

    A request replay that leaves its repair to the drain dispatches as a queued
    deferral with a coverage code, and the terminal's graph diagnostic says so,
    whatever the acknowledgement covers -- not a completed rebuild. The terminal's
    separate `graph_sync` probe is a follow-up (task 9.5).
    """
    from exomem import graph_sync

    root = _built(vault, whole_vault_passes)
    graph_sync._write_floor(root, graph_sync.GraphSyncGenerationFloor.create(1))
    graph_sync._write_checkpoint(
        root,
        graph_sync.GraphSyncCheckpoint.create(
            generation=1, mutation_id="1" * 24, paths=(), created_paths=(), scope="full"
        ),
    )
    EpistemicGraphIndex(root).rebuild_all()
    _publish_past_a_stale_row(root)
    required = graph_sync.read_checkpoint(root)
    acknowledged = graph_sync.acknowledged_checkpoint(root)
    assert required is not None and acknowledged is not None and acknowledged.covers(required)
    assert EpistemicGraphIndex(root).available() is True
    whole_vault_passes.clear()

    with monkeypatch.context() as live:
        if marker:
            deferred_index.advance_graph_full_rebuild(root)
        else:
            live.setattr(
                EpistemicGraphIndex,
                "epoch_admits_incremental_repair",
                lambda self: False,
                raising=True,
            )
        result = _replay_under_a_mutation_request(root)

    assert caller_whole_vault_calls == [], "a whole-vault pass ran on the request's thread"
    assert (result.outcome, result.code) == ("deferred", "graph_repair_queued"), result
    assert result.code in index_sync._GRAPH_COVERAGE_CODES
    assert not result.whole_vault_attempted
    assert _terminal_graph_diagnostic(monkeypatch, result) == {
        "component": "epistemic_graph",
        "state": "deferred",
        "code": "graph_repair_queued",
    }
    assert EpistemicGraphIndex(root).available() is False
    _assert_the_daemon_drains_it_once(root)
