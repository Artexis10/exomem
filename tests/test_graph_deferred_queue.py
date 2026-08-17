"""Phase 2 of `converge-graph-incrementally`: graph repair proportional to the change.

Phase 1 took the graph rebuild off the interactive write path. That made writes
fast and left the *convergence* defect untouched: every bail-out from the
incremental refresh path still means "re-walk the entire vault", and a
whole-vault pass is guarded by a vault-global optimistic check that any
concurrent write invalidates. The pass therefore gets less likely to succeed the
larger the vault and the busier the writer -- a livelock by construction, which
is why seven `fix(graph):` commits inside that loop did not end it.

The fix is the one the codebase already runs for the semantic and embedding
indexes: a durable per-path dirty queue, drained off the write path. What these
tests pin down is not the queue's existence but its four load-bearing
properties, each of which is a way the naive version silently fails:

1. **Durability at the right seam.** The changed-path set has to be enqueued
   before the canonical batch commits, not after. Over-enqueueing is free -- a
   drain re-indexes a path whose content did not change and writes nothing.
   Under-enqueueing is unrecoverable without a full rebuild, which is the thing
   being removed. The asymmetry decides the ordering.
2. **Equivalence.** A graph assembled by incremental drains has to equal the
   graph a full rebuild produces from the same vault. Without this the queue is
   just a faster way to be wrong.
3. **Monotonicity.** A write landing *during* a drain must append work, not
   invalidate the drain's completed work. This is the exact property the current
   global-proof design lacks, and the reason its retry budget cannot save it.
4. **Proportionality, stated as a table.** "Bail-outs no longer rebuild" is only
   half true: a handful of them genuinely cannot be repaired path-locally.
   Which ones is a judgement, and a judgement that lives implicitly in sixteen
   `return fallback(...)` sites is a judgement nobody can audit. So it lives in
   one declared mapping, and this suite fails if a reason appears, disappears,
   or changes side without the table changing with it.

Red-first: these describe the seams Phase 2 builds. They fail until it does.
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from exomem import deferred_index, epistemic_graph, freshness, graph_sync, index_sync
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.epistemic_graph import EpistemicGraphIndex

PAGE_A = "Knowledge Base/Notes/Insights/queue-a.md"
PAGE_B = "Knowledge Base/Notes/Insights/queue-b.md"
PAGE_C = "Knowledge Base/Notes/Insights/queue-c.md"


def _page(title: str, body: str) -> str:
    return f"---\ntype: insight\nstatus: active\n---\n# {title}\n\n## Claim\n\n{body}\n"


def _seed_live_freshness(root: Path) -> None:
    freshness.seed(
        root,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(root)),
    )
    kb = root / "Knowledge Base"
    freshness.seed(
        root,
        "kb",
        ((str(path), freshness.stat_signature(path)) for path in find_module._walk_md(kb)),
    )


@pytest.fixture
def vault(tmp_path: Path) -> Any:
    root = tmp_path / "vault"
    (root / "Knowledge Base/Notes/Insights").mkdir(parents=True)
    (root / PAGE_A).write_text(_page("A", "A claims against [[queue-b]]."), encoding="utf-8")
    (root / PAGE_B).write_text(_page("B", "B is a plain claim."), encoding="utf-8")
    _seed_live_freshness(root)
    EpistemicGraphIndex(root).rebuild_all()
    epistemic_graph.clear_publication_memos()
    yield root
    epistemic_graph.clear_publication_memos()


def _graph_contents(root: Path) -> dict[str, list[tuple[Any, ...]]]:
    """The three projections a drain and a rebuild must agree on, byte for byte.

    `graph_meta` is deliberately excluded: it carries the instance discriminator
    and the publication lineage, which are *supposed* to differ between a
    rebuild and a drain. Comparing them would make this test unfailable for the
    right reason and unpassable for the wrong one.
    """
    conn = sqlite3.connect(EpistemicGraphIndex(root).path)
    try:
        return {
            "nodes": conn.execute(
                "SELECT node_key, kind, path, anchor, title, text, source_hash, "
                "line_start, line_end, metadata, unit_ref, unit_category, unit_kind "
                "FROM graph_nodes ORDER BY node_key"
            ).fetchall(),
            "edges": conn.execute(
                "SELECT edge_key, src_key, dst_key, relation_type, raw_relation, "
                "parent_relation, registry_status, registry_version, registry_hash, "
                "origin, source_path, source_anchor, metadata "
                "FROM graph_edges ORDER BY edge_key"
            ).fetchall(),
            "parent_refs": conn.execute(
                "SELECT path, parent_ref FROM graph_parent_refs ORDER BY path"
            ).fetchall(),
        }
    finally:
        conn.close()


# --- 1. The changed-path set is enqueued durably, never discarded ---------------


def test_a_canonical_write_enqueues_its_changed_paths(vault: Path) -> None:
    """The checkpoint already records exactly what changed; stop throwing it away."""
    deferred_index.clear_graph(vault)

    vault_module.batch_atomic_write(
        [
            vault_module.PlannedWrite(
                vault / PAGE_A, _page("A", "A now claims against [[queue-b]] twice.")
            )
        ],
        vault_root=vault,
    )

    assert PAGE_A in deferred_index.list_graph_paths(vault)


def test_a_created_path_is_enqueued_alongside_the_changed_ones(vault: Path) -> None:
    """`created_paths` is a separate field on the checkpoint and a separate bug."""
    deferred_index.clear_graph(vault)

    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(vault / PAGE_C, _page("C", "C is new."))],
        vault_root=vault,
    )

    assert PAGE_C in deferred_index.list_graph_paths(vault)


def test_the_enqueue_precedes_the_commit_so_a_crash_cut_cannot_lose_it(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordering *is* the durability argument, so assert the ordering.

    A crash between the markdown replace and the enqueue would leave the vault
    changed and the graph with no record that it must catch up -- recoverable
    only by the whole-vault rebuild this change exists to retire. Enqueueing
    first inverts the failure: a crash leaves a path queued whose content never
    changed, and re-indexing an unchanged path is a no-op.
    """
    deferred_index.clear_graph(vault)
    order: list[str] = []
    real_enqueue = deferred_index.enqueue_graph_checkpoint

    def record_enqueue(root: Path, checkpoint: Any) -> int:
        order.append("enqueue")
        return real_enqueue(root, checkpoint)

    monkeypatch.setattr(deferred_index, "enqueue_graph_checkpoint", record_enqueue)

    class Crash(RuntimeError):
        pass

    def crash_before_commit(*_args: Any, **_kwargs: Any) -> None:
        order.append("commit")
        raise Crash("power cut between staging and commit")

    monkeypatch.setattr(vault_module, "replace_tolerating_transient_sharing", crash_before_commit)

    with pytest.raises(Crash):
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(vault / PAGE_A, _page("A", "A changes."))],
            vault_root=vault,
        )

    assert order[:2] == ["enqueue", "commit"], (
        "the graph dirty set must be durable before the markdown replace, not after"
    )
    assert PAGE_A in deferred_index.list_graph_paths(vault)


def test_a_full_scope_batch_enqueues_a_marker_rather_than_a_path_list(vault: Path) -> None:
    """Above the checkpoint path limit the queue would be unbounded; a marker is not."""
    deferred_index.clear_graph(vault)
    deferred_index.clear_graph_full_rebuild(vault)

    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=7,
        mutation_id=f"{7:024x}",
        paths=(),
        created_paths=(),
        scope="full",
    )
    deferred_index.enqueue_graph_checkpoint(vault, checkpoint)

    assert deferred_index.list_graph_paths(vault) == []
    assert deferred_index.graph_full_rebuild_pending(vault) == 7


def test_a_poisoned_path_is_rotated_behind_the_rest_of_the_queue(vault: Path) -> None:
    """One unindexable page must not pin every later page in the sorted batch."""
    receipts = deferred_index.add_graph_receipts(vault, [PAGE_A, PAGE_B])
    assert len(receipts) == 2

    poison = next(receipt for receipt in receipts if receipt.rel_path == PAGE_A)
    deferred_index.rotate_graph_receipts(vault, [poison])

    assert deferred_index.list_graph_paths(vault)[-1] == PAGE_A, (
        "a rotated receipt must sort behind untouched work, not disappear from it"
    )
    assert deferred_index.snapshot_graph(vault, limit=1)[0].rel_path == PAGE_B


def test_clearing_a_receipt_is_compare_and_swap_not_delete(vault: Path) -> None:
    """A write that lands mid-drain must not be retired by the drain that missed it."""
    stale = deferred_index.add_graph_receipts(vault, [PAGE_A])
    deferred_index.add_graph_receipts(vault, [PAGE_A])  # a second write, new revision

    assert deferred_index.clear_graph_receipts(vault, stale) == 0
    assert PAGE_A in deferred_index.list_graph_paths(vault)


# --- 2. A drain is equivalent to a full rebuild ---------------------------------


def test_queued_drains_reach_the_same_graph_as_a_full_rebuild(vault: Path) -> None:
    """Incremental repair that is merely *faster* than a rebuild is not repair."""
    for content, page in (
        ("A now points at [[queue-c]] instead.", PAGE_A),
        ("C answers [[queue-a]].", PAGE_C),
        ("B is revised and links [[queue-c]].", PAGE_B),
    ):
        (vault / page).write_text(_page(page, content), encoding="utf-8")
        _seed_live_freshness(vault)
        deferred_index.add_graph_receipts(vault, [page])
        index_sync.drain_deferred_work(vault)

    drained = _graph_contents(vault)

    EpistemicGraphIndex(vault).rebuild_all()
    rebuilt = _graph_contents(vault)

    assert drained["nodes"] == rebuilt["nodes"]
    assert drained["edges"] == rebuilt["edges"]
    assert drained["parent_refs"] == rebuilt["parent_refs"]


def test_a_drain_does_not_empty_the_node_and_edge_tables(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point is that repair is scoped; a `DELETE FROM` is not scoped."""
    executed: list[str] = []
    real_connect = EpistemicGraphIndex._connect

    def tracing_connect(self: Any) -> sqlite3.Connection:
        conn = real_connect(self)
        conn.set_trace_callback(executed.append)
        return conn

    (vault / PAGE_A).write_text(_page("A", "A is revised."), encoding="utf-8")
    _seed_live_freshness(vault)
    deferred_index.add_graph_receipts(vault, [PAGE_A])

    monkeypatch.setattr(EpistemicGraphIndex, "_connect", tracing_connect)
    try:
        index_sync.drain_deferred_work(vault)
    finally:
        monkeypatch.undo()

    assert executed, "the drain never opened the graph sidecar"
    unscoped = [
        sql
        for sql in executed
        if sql.strip().upper().startswith("DELETE FROM GRAPH_")
        and "WHERE" not in sql.upper()
    ]
    assert unscoped == [], f"a drain issued an unscoped delete: {unscoped}"


# --- 3. The queue is monotone under concurrency ---------------------------------


def test_a_write_landing_during_a_drain_is_repaired_by_the_next_drain(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Appending work is the property the global-proof design cannot have."""
    (vault / PAGE_A).write_text(_page("A", "A is revised once."), encoding="utf-8")
    _seed_live_freshness(vault)
    deferred_index.add_graph_receipts(vault, [PAGE_A])

    real_index_path = EpistemicGraphIndex._index_path
    landed = False

    def index_then_write(self: Any, conn: Any, path: Path, **kwargs: Any) -> bool:
        nonlocal landed
        outcome = real_index_path(self, conn, path, **kwargs)
        if not landed:
            landed = True
            (vault / PAGE_B).write_text(_page("B", "B lands mid-drain."), encoding="utf-8")
            _seed_live_freshness(vault)
            deferred_index.add_graph_receipts(vault, [PAGE_B])
        return outcome

    monkeypatch.setattr(EpistemicGraphIndex, "_index_path", index_then_write)
    index_sync.drain_deferred_work(vault)
    monkeypatch.undo()

    assert PAGE_B in deferred_index.list_graph_paths(vault), (
        "the mid-drain write must survive the drain that did not cover it"
    )

    index_sync.drain_deferred_work(vault)

    assert deferred_index.list_graph_paths(vault) == []
    assert _graph_contents(vault) == _graph_contents_after_full_rebuild(vault)


def _graph_contents_after_full_rebuild(root: Path) -> dict[str, list[tuple[Any, ...]]]:
    EpistemicGraphIndex(root).rebuild_all()
    return _graph_contents(root)


# --- 4. Repair is proportional, and the exceptions are declared -----------------

#: Every `fallback(...)` reason in `_refresh_paths_locked`, and which of the two
#: repairs it earns.
#:
#: `"defer"` means the incremental path could not *prove* its result but the
#: scope of the damage is known: enqueue the affected paths and let a drain
#: repair them. Every reason here is a race -- a concurrent writer moved a
#: durable token between two reads -- and a race is precisely what the retry
#: budget cannot win, because each whole-vault attempt widens the window that
#: loses it.
#:
#: `"rebuild"` means the scope is *unknown*: the graph sidecar could not be read,
#: or the delta that tells us what changed is itself incomplete. Enqueuing a
#: bounded set there would silently leave the rest of the graph stale, which is
#: worse than the cost this change is removing. These stay whole-vault, and the
#: point of writing them down is that they stay *few*.
_DECLARED_FALLBACK_DISPOSITIONS = {
    # Races and stale bindings: bounded, known scope.
    "path_outside_vault": "defer",
    "path_unreadable": "defer",
    "durable_checkpoint_moved": "defer",
    "checkpoint_paths_mismatch": "defer",
    "checkpoint_created_paths_mismatch": "defer",
    "acknowledgement_is_not_the_predecessor": "defer",
    "delta_target_moved": "defer",
    "caller_path_outside_delta": "defer",
    "topology_proof_moved": "defer",
    "incremental_marker_refused": "defer",
    "unreachable": "defer",
    # Unknown scope: the sidecar or the delta cannot be trusted to bound it.
    "checkpoint_scope_is_not_paths": "rebuild",
    "graph_snapshot_unavailable": "rebuild",
    "recall_checkpoint_absent_or_registry_not_live": "rebuild",
    "recall_delta_incomplete": "rebuild",
    "stored_resolver_entries_unreadable": "rebuild",
    "resolver_snapshot_unavailable": "rebuild",
    "topology_snapshot_unavailable": "rebuild",
    "stored_topology_unreadable": "rebuild",
    "stored_topology_fingerprint_mismatch": "rebuild",
}


def _fallback_reasons_in_source() -> set[str]:
    """Read the reasons out of the module rather than trusting a hand list."""
    tree = ast.parse(Path(epistemic_graph.__file__).read_text(encoding="utf-8"))
    reasons: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "fallback"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            reasons.add(node.args[0].value)
    return reasons


def test_every_fallback_reason_has_a_declared_disposition() -> None:
    assert _fallback_reasons_in_source() == set(_DECLARED_FALLBACK_DISPOSITIONS), (
        "an incremental-refresh bail-out appeared, moved, or was renamed: decide "
        "whether its damage has a known scope (defer) or not (rebuild) and say so "
        "here and in epistemic_graph._FALLBACK_DISPOSITIONS"
    )


def test_the_module_and_the_suite_agree_on_every_disposition() -> None:
    """One definition, imported -- the rule task 1.4 already established."""
    assert epistemic_graph._FALLBACK_DISPOSITIONS == _DECLARED_FALLBACK_DISPOSITIONS


def test_most_bail_outs_defer_so_the_common_case_stays_proportional() -> None:
    """A table that quietly drifted to all-rebuild would pass the checks above."""
    deferring = sum(
        1 for value in _DECLARED_FALLBACK_DISPOSITIONS.values() if value == "defer"
    )
    assert deferring >= len(_DECLARED_FALLBACK_DISPOSITIONS) // 2


def test_an_ordinary_bail_out_performs_no_whole_vault_walk(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measurable claim: O(changed), not O(vault)."""
    walks: list[Any] = []
    real_pass = EpistemicGraphIndex._rebuild_all_pass

    def record(self: Any, *args: Any, **kwargs: Any) -> Any:
        walks.append(self)
        return real_pass(self, *args, **kwargs)

    monkeypatch.setattr(EpistemicGraphIndex, "_rebuild_all_pass", record)

    index = EpistemicGraphIndex(vault)
    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=99,
        mutation_id=f"{99:024x}",
        paths=((PAGE_A, "d" * 64),),
        created_paths=(),
    )
    (vault / PAGE_A).write_text(_page("A", "A is revised."), encoding="utf-8")

    with index._mutation_coordinator.hold(
        operation="epistemic_graph_refresh_paths", holder_kind="graph"
    ):
        report = index._refresh_paths_locked(
            [vault / PAGE_A], graph_checkpoint=checkpoint
        )

    assert report.get("deferred") == 1
    assert walks == [], "an ordinary bail-out re-walked the whole vault"
    assert PAGE_A in deferred_index.list_graph_paths(vault)


def test_a_missing_sidecar_still_rebuilds_the_whole_vault(vault: Path) -> None:
    """Proportional repair is an optimisation; the rebuild must stay reachable."""
    index = EpistemicGraphIndex(vault)
    index.path.unlink()

    index.rebuild_all()

    assert _graph_contents(vault)["nodes"], "the whole-vault rebuild stopped working"


def test_a_page_written_before_its_link_target_still_gains_the_edge(vault: Path) -> None:
    """The forward reference: the defect the equivalence test actually found.

    An unresolved wikilink produces no edge at all -- `_body_wikilink_paths`
    drops it -- so a page written before its target exists has a hole, and
    indexing the *target* later cannot repair the *source*. A full rebuild never
    notices because it re-derives everything once the corpus is complete. Named
    on its own so a regression reads as "forward references broke" rather than
    "some graph contents differ".
    """
    (vault / PAGE_A).write_text(_page("A", "A points at [[queue-c]]."), encoding="utf-8")
    _seed_live_freshness(vault)
    deferred_index.add_graph_receipts(vault, [PAGE_A])
    index_sync.drain_deferred_work(vault)

    (vault / PAGE_C).write_text(_page("C", "C exists now."), encoding="utf-8")
    _seed_live_freshness(vault)
    deferred_index.add_graph_receipts(vault, [PAGE_C])
    index_sync.drain_deferred_work(vault)

    edges = {
        (row[1], row[2])
        for row in _graph_contents(vault)["edges"]
    }
    assert (f"file:{PAGE_A}", f"file:{PAGE_C}") in edges


def test_an_ordinary_edit_does_not_pay_for_topology_repair(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repairing forward references costs a corpus scan; ordinary edits must not.

    If this ever fires on a plain body edit, every write pays the O(vault) read
    the whole change exists to stop paying.
    """
    scans: list[Any] = []
    real_scan = EpistemicGraphIndex._sources_linking_to

    def record(self: Any, targets: set[str], **kwargs: Any) -> set[str]:
        scans.append(targets)
        return real_scan(self, targets, **kwargs)

    monkeypatch.setattr(EpistemicGraphIndex, "_sources_linking_to", record)

    (vault / PAGE_A).write_text(_page("A", "A says something else."), encoding="utf-8")
    _seed_live_freshness(vault)
    deferred_index.add_graph_receipts(vault, [PAGE_A])
    index_sync.drain_deferred_work(vault)

    assert scans == [], "a plain edit triggered the corpus scan reserved for topology changes"


def test_a_drain_retires_the_generation_it_converged(vault: Path) -> None:
    """Converging the content is only half the job; the epoch has to learn it.

    The graph_sync acknowledgement is what tells every later reader that the
    committed generation is covered. A drain that repairs the pages but leaves
    the acknowledgement behind converges nothing that anybody can observe: the
    epoch stays stale, `available()` stays false, and the next dispatch takes
    the whole-vault rebuild anyway. The queue would then be pure overhead --
    the expensive path still runs, and now there is a second store to keep.
    """
    write = vault / PAGE_C
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(write, _page("C", "C cites [[queue-a]]."))],
        vault_root=vault,
        post_commit_fanout=False,
    )
    _seed_live_freshness(vault)
    required = graph_sync.read_checkpoint(vault)
    assert required is not None
    assert deferred_index.snapshot_graph(vault), "the write left no graph debt to drain"

    index_sync.drain_deferred_work(vault)

    assert graph_sync.status(vault) == {
        "state": "current",
        "generation": required.generation,
    }
    assert EpistemicGraphIndex(vault).available()
