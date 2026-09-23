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
from concurrent.futures import ThreadPoolExecutor
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


def test_direct_full_rebuild_debt_advances_atomically(vault: Path) -> None:
    """A drain between direct writers must not clear the later writer's debt."""
    deferred_index.clear_graph_full_rebuild(vault)
    deferred_index.mark_graph_full_rebuild(vault, generation=7)
    assert deferred_index.clear_graph_full_rebuild(vault, generation=7) is True
    assert deferred_index.advance_graph_full_rebuild(vault) == 8

    with ThreadPoolExecutor(max_workers=3) as pool:
        generations = list(
            pool.map(
                lambda _item: deferred_index.advance_graph_full_rebuild(vault),
                range(3),
            )
        )

    assert sorted(generations) == [9, 10, 11]
    assert deferred_index.clear_graph_full_rebuild(vault, generation=7) is False
    assert deferred_index.graph_full_rebuild_pending(vault) == 11


def test_legacy_marker_clear_seeds_the_durable_generation_sequence(vault: Path) -> None:
    """The first post-upgrade drain cannot reset generation history to zero."""
    deferred_index.mark_graph_full_rebuild(vault, generation=1)
    conn = sqlite3.connect(deferred_index.store_path(vault))
    try:
        with conn:
            conn.execute(
                "DELETE FROM maintenance_state WHERE key IN (?, ?)",
                ("graph_full_rebuild_generation", "graph_full_rebuild_sequence"),
            )
            conn.execute(
                "INSERT INTO maintenance_state(key, value) VALUES (?, ?)",
                ("graph_full_rebuild_generation", "7"),
            )
    finally:
        conn.close()

    assert deferred_index.clear_graph_full_rebuild(vault, generation=7) is True
    assert deferred_index.advance_graph_full_rebuild(vault) == 8
    assert deferred_index.clear_graph_full_rebuild(vault, generation=7) is False
    assert deferred_index.graph_full_rebuild_pending(vault) == 8


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
    # Not a race: a cold process-local resolver cache. The delta is proven
    # complete when it fires, so the queue owns the repair.
    "resolver_snapshot_unavailable": "defer",
    # Unknown scope: the sidecar or the delta cannot be trusted to bound it.
    "checkpoint_scope_is_not_paths": "rebuild",
    "graph_snapshot_unavailable": "rebuild",
    "recall_checkpoint_absent_or_registry_not_live": "rebuild",
    "recall_delta_incomplete": "rebuild",
    "stored_resolver_entries_unreadable": "rebuild",
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


def test_a_queue_predating_the_graph_table_reads_as_empty(vault: Path) -> None:
    """An existing vault upgrades into this feature; it does not start at it.

    Every deployed vault already has a `.deferred-index.sqlite` carrying the
    semantic and full queues, and none of them has a `graph_upserts` table until
    something opens the store for writing. The readers run first -- a drain asks
    what is queued before it queues anything -- and they open read-only, where
    creating the table is not possible. Raising there turns an ordinary upgrade
    into a hard failure on the first drain, which is how CI found it.
    """
    deferred_index.add_graph(vault, [PAGE_A])
    with sqlite3.connect(deferred_index.store_path(vault)) as conn:
        conn.execute("DROP TABLE graph_upserts")

    assert deferred_index.snapshot_graph(vault) == []
    assert deferred_index.list_graph_paths(vault) == []
    assert deferred_index.graph_status(vault)["count"] == 0


# --------------------------------------------------------------------------- #
# Receipt lineage (task 1.13a)
# --------------------------------------------------------------------------- #


def test_a_receipt_records_the_generation_it_was_queued_for(tmp_path: Path) -> None:
    """Coverage of a lineage gap is a claim about a generation, not a path name."""
    receipts = deferred_index.add_graph_receipts(
        tmp_path, ["Knowledge Base/Notes/a.md"], generation=7
    )
    assert [receipt.graph_generation for receipt in receipts] == [7]
    assert [
        receipt.graph_generation for receipt in deferred_index.snapshot_graph(tmp_path)
    ] == [7]
    assert deferred_index.graph_receipt_generations(tmp_path) == (frozenset({7}), False)


def test_a_caller_with_no_checkpoint_leaves_the_generation_unknown(tmp_path: Path) -> None:
    """Unknown is honest, and never usable as coverage."""
    deferred_index.add_graph_receipts(tmp_path, ["Knowledge Base/Notes/a.md"])
    known, unknown = deferred_index.graph_receipt_generations(tmp_path)
    assert known == frozenset()
    assert unknown is True


def test_a_requeue_moves_the_generation_forward_and_never_backward(tmp_path: Path) -> None:
    """The row owes the newest repair; an unknown re-queue does not erase what it owes."""
    rel = "Knowledge Base/Notes/a.md"
    deferred_index.add_graph_receipts(tmp_path, [rel], generation=4)
    deferred_index.add_graph_receipts(tmp_path, [rel], generation=9)
    assert deferred_index.graph_receipt_generations(tmp_path) == (frozenset({9}), False)

    deferred_index.add_graph_receipts(tmp_path, [rel], generation=2)
    assert deferred_index.graph_receipt_generations(tmp_path) == (frozenset({9}), False), (
        "a later enqueue at an older generation must not walk the claim backwards"
    )

    deferred_index.add_graph_receipts(tmp_path, [rel])
    assert deferred_index.graph_receipt_generations(tmp_path) == (frozenset({9}), False), (
        "an enqueue that does not know its generation must not erase one that did"
    )


def test_a_pre_upgrade_receipt_row_survives_the_migration_as_unknown(
    tmp_path: Path,
) -> None:
    """An existing queue upgrades in place, and none of it blesses anything.

    Written against the exact pre-upgrade DDL rather than by dropping the
    column, because the migration's contract is about the schema a shipped
    store actually has.
    """
    store = deferred_index.store_path(tmp_path)
    store.parent.mkdir(parents=True, exist_ok=True)
    legacy = sqlite3.connect(store)
    try:
        with legacy:
            legacy.execute(
                "CREATE TABLE IF NOT EXISTS graph_upserts ("
                "rel_path TEXT PRIMARY KEY, created_at REAL NOT NULL, "
                "updated_at REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 1)"
            )
            legacy.execute(
                "INSERT INTO graph_upserts(rel_path, created_at, updated_at, revision) "
                "VALUES ('Knowledge Base/Notes/legacy.md', 1.0, 1.0, 3)"
            )
    finally:
        legacy.close()

    receipts = deferred_index.snapshot_graph(tmp_path)

    assert [receipt.rel_path for receipt in receipts] == ["Knowledge Base/Notes/legacy.md"]
    assert receipts[0].revision == 3, "the migration must not lose queued work"
    assert receipts[0].graph_generation is None
    assert deferred_index.graph_receipt_generations(tmp_path) == (frozenset(), True)


def test_a_stale_receipt_does_not_bless_a_fresh_batch(vault: Path, monkeypatch) -> None:
    """The staleness hole: an older generation's row naming the same path.

    Repair owed for bytes that have since been overwritten is not repair for
    what just changed, and blessing this batch with it leaves the new content
    with nothing scheduled at all.
    """
    root = vault
    rel = PAGE_A
    target = root / rel
    # A canonical write is what mints a graph checkpoint, and the generation it
    # mints is the one a deferral on this batch would report.
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(target, _page("A", "A claims against [[queue-b]]."))],
        vault_root=root,
    )

    checkpoint = graph_sync.read_checkpoint(root)
    assert checkpoint is not None, "this shape needs a canonical graph checkpoint"
    required = int(checkpoint.generation)

    report = index_sync.IndexSyncReport(
        operation="upsert",
        requested_paths=(rel,),
        eligible_paths=(rel,),
        components=(
            index_sync.IndexComponentOutcome("memory_refs", "completed", "ok"),
            index_sync.IndexComponentOutcome("resolver", "completed", "ok"),
            index_sync.IndexComponentOutcome("semantic_purge", "completed", "ok"),
            index_sync.IndexComponentOutcome("lexstore", "completed", "ok"),
            index_sync.IndexComponentOutcome(
                "epistemic_graph", "deferred", "graph_repair_queued"
            ),
            index_sync.IndexComponentOutcome("embeddings", "completed", "ok"),
        ),
    )
    monkeypatch.setattr(
        graph_sync, "registered_checkpoint", lambda *_a, **_kw: None, raising=True
    )

    deferred_index._clear(root, table="graph_upserts", rel_paths=[rel])
    deferred_index.add_graph_receipts(root, [rel], generation=required - 1)
    index_sync.reset_deferral_telemetry()
    assert index_sync.full_upsert_succeeded(root, [target], report) is False, (
        "a receipt from an earlier generation blessed a fresh deferral"
    )
    assert index_sync.deferral_telemetry()["uncovered_deferral_escalated"] == 1

    deferred_index.add_graph_receipts(root, [rel], generation=required)
    index_sync.reset_deferral_telemetry()
    assert index_sync.full_upsert_succeeded(root, [target], report) is True
    assert index_sync.deferral_telemetry()["covered_deferral_accepted"] == 1


#: Every call in `src/exomem/` that records a graph receipt UNDER A GENERATION,
#: keyed by module and enclosing function, and what path set it claims for that
#: generation. The predecessor probe reads these rows as proof that a skipped
#: generation's repair is already queued (`seamless-managed-worker-handoff`
#: 1.13), and that proof rests on a convention no probe can check: **an enqueue
#: that records generation G records G's complete path set.** A site that
#: recorded half of G's paths under G would make the probe answer "covered" for
#: a gap it only half owns, and the write that trusted it would publish over
#: real divergence -- silently, and with no rebuild to catch it later.
#:
#: Per call site, in the style of `_DECLARED_UNBOUNDED_JOINS`: a site that moves
#: within its file keeps its identity, and a NEW one cannot inherit the claim by
#: living next to one that made it.
_DECLARED_GENERATION_RECORDING_SITES = {
    # The canonical batch's own debt, from the checkpoint it already writes.
    # Complete by construction: the checkpoint carries every changed and every
    # created path for that generation, and a batch too large to carry them
    # records a whole-vault rebuild marker instead of a partial list.
    "deferred_index.py::enqueue_graph_checkpoint": (
        "the checkpoint's complete changed + created set for its own generation"
    ),
    # Every deferral that claims durable per-path coverage: the incremental
    # bail-outs, the cold resolver, the external-pending fence. Complete because
    # the deferred scope STARTS as the checkpoint's changed and created paths
    # and only ever widens as the pass learns more; an enqueue that could not
    # admit every path escalates to whole-vault debt rather than reporting a
    # queue that does not hold the work.
    "epistemic_graph.py::EpistemicGraphIndex::_queue_graph_repair": (
        "the deferring write's own checkpoint scope, widened, never narrowed"
    ),
    # A pass-through seam, not a scope decision: it records whatever generation
    # its caller hands it. Its callers are declared below.
    "epistemic_graph.py::_record_graph_repair_demand": (
        "delegates; every caller that supplies a generation is declared here too"
    ),
    # The one site that records a PARTIAL set, and the reason it is sound: an
    # adopted residue is the paths whose bytes moved under the acknowledged
    # generation, not that generation's whole delta. It is safe because it
    # records the ACKNOWLEDGED generation, and a coverage probe only ever asks
    # about generations strictly ABOVE the acknowledgement -- so these rows can
    # never bless a skipped step. Recording it accurately is what keeps that
    # true; recording it under the required generation would not be.
    "epistemic_graph.py::EpistemicGraphIndex::apply_adopted_residue": (
        "partial by design: the residue at the ACKNOWLEDGED generation, which "
        "no gap probe ever asks about"
    ),
}

#: The functions that write a generation onto a graph receipt. `add_graph` and
#: `add_graph_receipts` are the durable writes; `_record_graph_repair_demand` is
#: the seam that forwards to one, and is enumerated so its callers have to
#: declare themselves rather than hide one level up.
_GENERATION_RECORDING_CALLEES = frozenset(
    {"add_graph", "add_graph_receipts", "_record_graph_repair_demand"}
)


def _generation_recording_sites() -> dict[str, str]:
    """Every generation-recording call in `src/exomem/`, keyed by site.

    Read out of the source rather than from a hand list, and attributed to the
    enclosing function, so the declaration above is a claim about code that
    exists rather than about code that existed once.
    """
    source_root = Path(deferred_index.__file__).parent
    found: dict[str, str] = {}

    class Visitor(ast.NodeVisitor):
        def __init__(self, module: str) -> None:
            self.module = module
            self.stack: list[str] = []

        def _scoped(self, node: Any) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = _scoped
        visit_AsyncFunctionDef = _scoped
        visit_ClassDef = _scoped

        def visit_Call(self, node: ast.Call) -> None:
            name = getattr(node.func, "attr", getattr(node.func, "id", None))
            if name in _GENERATION_RECORDING_CALLEES:
                generation = next(
                    (kw.value for kw in node.keywords if kw.arg == "generation"), None
                )
                # A site that hard-codes `generation=None` claims nothing and
                # is not a recording site; everything else is, including the
                # conditional expressions that pass None only sometimes.
                records = generation is not None and not (
                    isinstance(generation, ast.Constant) and generation.value is None
                )
                if records:
                    site = "::".join([self.module, *self.stack])
                    found[site] = ast.unparse(generation)
            self.generic_visit(node)

    for path in sorted(source_root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover - a broken tree is CI's job
            continue
        Visitor(path.name).visit(tree)
    # The definitions themselves are not call sites.
    for definition in (
        "deferred_index.py::add_graph",
        "deferred_index.py::add_graph_receipts",
    ):
        found.pop(definition, None)
    return found


def test_every_generation_recording_enqueue_declares_its_path_set() -> None:
    """A new site cannot inherit 1.13's coverage claim by staying quiet.

    The whole per-generation shortcut rests on "recording G means recording all
    of G", which is a convention, not a checked invariant -- a probe reading the
    queue cannot tell a complete path set from half of one. So the sites are
    enumerated from the source and matched against what each one declares.
    """
    assert set(_generation_recording_sites()) == set(
        _DECLARED_GENERATION_RECORDING_SITES
    ), (
        "a graph receipt is being recorded under a generation from a site that "
        "has not declared what path set it claims for it. The predecessor probe "
        "reads these rows as proof a skipped generation is already queued "
        "(`_lineage_gap_is_receipt_covered`), so a site recording a PARTIAL set "
        "under a generation a probe can ask about would bless a gap it only "
        "half owns. Declare the site and its claim, or pass generation=None -- "
        "an unknown row never counts as coverage."
    )


# --- The drain records what it derived under ------------------------------------


def _acknowledged(root: Path) -> int:
    acknowledged = graph_sync.acknowledged_checkpoint(root)
    return 0 if acknowledged is None else int(acknowledged.generation)


def _write_without_graph_repair(
    root: Path, monkeypatch: pytest.MonkeyPatch, pages: dict[str, str]
) -> int:
    """One canonical batch whose graph repair is left to the queue.

    The batch enqueues its paths and records its debt generation before it
    commits, exactly as every canonical batch does; only the dispatch that
    would repair them in-line is switched off, so the drain is what converges
    them.
    """
    with monkeypatch.context() as patch:
        patch.setattr(epistemic_graph, "graph_scheduling_enabled", lambda: False)
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(root / rel, content) for rel, content in pages.items()],
            vault_root=root,
        )
    checkpoint = graph_sync.read_checkpoint(root)
    assert checkpoint is not None
    assert _acknowledged(root) < int(checkpoint.generation)
    return int(checkpoint.generation)


def test_a_drain_records_the_topology_it_derived_under(
    vault: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A drain that repairs a created page leaves the stored topology behind it.

    The next topology-changing write rebuilds the old resolver from the stored
    entries it changed and compares its fingerprint to the stored one. A drain
    that re-derived rows under a topology it never recorded makes that compare
    fail, and the write falls back to a whole-vault rebuild.
    """
    import logging

    created = "Knowledge Base/Notes/Insights/queue-n.md"
    _write_without_graph_repair(
        vault, monkeypatch, {created: _page("N", "N is new and cites [[queue-a]].")}
    )
    index_sync.drain_graph_work(vault)
    assert deferred_index.list_graph_paths(vault) == []
    assert EpistemicGraphIndex(vault).available()

    caplog.set_level(logging.INFO, logger="exomem.epistemic_graph")
    vault_module.batch_atomic_write(
        [
            vault_module.PlannedWrite(
                vault / "Knowledge Base/Notes/Insights/queue-m.md",
                _page("M", "M is new too."),
            )
        ],
        vault_root=vault,
    )

    assert "stored_topology_fingerprint_mismatch" not in caplog.text
    assert EpistemicGraphIndex(vault).available()


def test_a_drain_under_recorded_movement_still_lands_the_rows_it_proved(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Movement elsewhere in the vault is not a reason to throw proven rows away.

    The drain proves every page it indexed against that page's own bytes at
    commit. A write landing on another page moves the vault-global projection,
    which bars the marker, lineage and acknowledgement -- not the rows. Rolling
    the whole pass back made every drain lose to the next write under a steady
    writer, so the queue never shrank while writes kept landing.
    """
    committed = _write_without_graph_repair(
        vault, monkeypatch, {PAGE_A: _page("A", "A is revised against [[queue-b]].")}
    )
    real_index_path = EpistemicGraphIndex._index_path
    landed: list[int] = []

    def index_then_write(self: Any, conn: Any, path: Path, **kwargs: Any) -> bool:
        outcome = real_index_path(self, conn, path, **kwargs)
        if Path(path).name == Path(PAGE_A).name:
            landed.append(1)
            # A steady writer: every pass over A, the batch and its isolated
            # retry alike, sees another write land elsewhere. Its post-commit
            # registry update lands outside the drain's hold, which is what
            # moves the projection under a real drain.
            (vault / PAGE_C).write_text(
                _page("C", f"C lands mid-drain, revision {len(landed)}."), encoding="utf-8"
            )
            _seed_live_freshness(vault)
            deferred_index.add_graph_receipts(vault, [PAGE_C])
        return outcome

    monkeypatch.setattr(EpistemicGraphIndex, "_index_path", index_then_write)
    index_sync.drain_graph_work(vault)
    monkeypatch.undo()

    assert landed
    assert deferred_index.list_graph_paths(vault) == [PAGE_C], (
        "the drain threw away rows it proved against their own bytes"
    )
    assert _acknowledged(vault) < committed, "a drain the vault moved under acknowledged"

    index_sync.drain_graph_work(vault)

    assert deferred_index.list_graph_paths(vault) == []
    assert _graph_contents(vault) == _graph_contents_after_full_rebuild(vault)


def test_a_late_refresh_whose_generation_a_drain_acknowledged_leaves_the_graph_readable(
    vault: Path,
) -> None:
    """A write's own refresh can arrive after a drain already converged its batch.

    The drain proved the batch's paths and acknowledged its generation, so the
    refresh finds the acknowledgement at its own generation rather than the one
    before it. That is not a lineage gap: nothing is owed. Falling back there
    withdrew the marker behind a barrier with nothing queued, and the drain
    daemon paid a whole-vault recovery for a graph that was current (probe B
    at five writers).
    """
    # The batch commits without its post-commit dispatch, which is the refresh
    # this test runs late; the registry learns of it the way the fan-out would.
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(vault / PAGE_A, _page("A", "A is revised against [[queue-b]]."))],
        vault_root=vault,
        post_commit_fanout=False,
    )
    _seed_live_freshness(vault)
    checkpoint = graph_sync.read_checkpoint(vault)
    assert checkpoint is not None
    index_sync.drain_graph_work(vault)
    assert _acknowledged(vault) == int(checkpoint.generation)
    assert EpistemicGraphIndex(vault).available()

    report = EpistemicGraphIndex(vault).refresh_paths(
        [vault / PAGE_A], graph_checkpoint=checkpoint
    )

    assert not report.get("deferred"), report
    assert EpistemicGraphIndex(vault).available(), (
        "a refresh for an already-acknowledged generation withdrew the marker"
    )
    assert not EpistemicGraphIndex(vault).reads_suspended()


def test_a_late_refresh_behind_a_late_registry_update_repairs_per_path(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-op is only for a refresh that finds the marker current.

    Here the drain acknowledges the write's generation against the registry
    as it stood, then the write's registry update lands, which leaves the
    marker describing an older projection. The late refresh then finds its
    generation acknowledged. Returning without work there left nothing queued
    and nothing able to republish the marker, so the drain daemon paid a
    whole-vault rebuild for one page (probe L2: 3.6 s, one whole-vault pass,
    against 0.9 s and none on main). It must queue the page, as main does, so
    one per-path drain makes the graph readable again.
    """
    passes: list[int] = []
    real_pass = EpistemicGraphIndex._rebuild_all_pass

    def counted(self: Any, *args: Any, **kwargs: Any) -> Any:
        passes.append(1)
        return real_pass(self, *args, **kwargs)

    monkeypatch.setattr(EpistemicGraphIndex, "_rebuild_all_pass", counted)
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(vault / PAGE_A, _page("A", "A is revised against [[queue-b]]."))],
        vault_root=vault,
        post_commit_fanout=False,
    )
    checkpoint = graph_sync.read_checkpoint(vault)
    assert checkpoint is not None
    index_sync.drain_graph_work(vault)
    assert _acknowledged(vault) == int(checkpoint.generation)
    assert EpistemicGraphIndex(vault).available()
    # The write's registry update lands after the drain acknowledged.
    freshness.on_files_changed(vault, changed=[vault / PAGE_A])
    assert not EpistemicGraphIndex(vault).available()

    report = EpistemicGraphIndex(vault).refresh_paths(
        [vault / PAGE_A], graph_checkpoint=checkpoint
    )

    assert report.get("queued"), report
    assert deferred_index.list_graph_paths(vault) == [PAGE_A]
    index_sync.drain_graph_work(vault)
    assert EpistemicGraphIndex(vault).available()
    assert passes == [], "a whole-vault pass repaired one late page"
    monkeypatch.setattr(EpistemicGraphIndex, "_rebuild_all_pass", real_pass)
    assert _graph_contents(vault) == _graph_contents_after_full_rebuild(vault)


# --- Quarantine stops hot retries and nothing else ------------------------------


def _node_rows(root: Path, rel: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(EpistemicGraphIndex(root).path)
    try:
        return conn.execute(
            "SELECT node_key, source_hash FROM graph_nodes WHERE path = ? ORDER BY node_key",
            (rel,),
        ).fetchall()
    finally:
        conn.close()


def _refuse_reads(
    monkeypatch: pytest.MonkeyPatch, root: Path, rel: str, error: Exception
) -> None:
    """The page's bytes cannot be read: a lock, a sync tool, a permission."""
    real_read = vault_module.read_bytes_without_pinning
    target = (root / rel).resolve()

    def refuse(path: Path, *args: Any, **kwargs: Any) -> Any:
        if Path(path).resolve() == target:
            raise error
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(vault_module, "read_bytes_without_pinning", refuse)


def _undecodable_reads(monkeypatch: pytest.MonkeyPatch, root: Path, rel: str) -> None:
    """The page's bytes are read, and are not UTF-8."""
    real_read = vault_module.read_bytes_without_pinning
    target = (root / rel).resolve()

    def garble(path: Path, *args: Any, **kwargs: Any) -> Any:
        if Path(path).resolve() == target:
            return b"---\ntype: insight\n---\n# \xff\xfe not utf-8\n"
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(vault_module, "read_bytes_without_pinning", garble)


def _age_graph_failures(root: Path, seconds: float) -> None:
    """Time passes: every recorded failure happened `seconds` earlier."""
    conn = sqlite3.connect(deferred_index.store_path(root))
    try:
        with conn:
            conn.execute(
                "UPDATE graph_failures SET first_failed_at = first_failed_at - ?, "
                "last_failed_at = last_failed_at - ?",
                (seconds, seconds),
            )
    finally:
        conn.close()


def _failure_attempts(root: Path, rel: str) -> int:
    conn = sqlite3.connect(deferred_index.store_path(root))
    try:
        row = conn.execute(
            "SELECT attempts FROM graph_failures WHERE rel_path = ?", (rel,)
        ).fetchone()
    finally:
        conn.close()
    return 0 if row is None else int(row[0])


def _poison_min_age() -> float:
    # Read with a default, as `GRAPH_POISON_ATTEMPTS` was, so a tree without the
    # minimum age fails these tests on behaviour rather than on a missing name.
    return float(getattr(epistemic_graph, "GRAPH_POISON_MIN_AGE_SECONDS", 600.0))


def _drain_ticks(root: Path, ticks: int) -> None:
    for _ in range(ticks):
        index_sync.drain_graph_work(root)


def _quarantine_unreadable_page_a(
    root: Path, monkeypatch: pytest.MonkeyPatch, lock: pytest.MonkeyPatch
) -> int:
    """PAGE_A's revision is queued, its bytes stay unreadable past the minimum age.

    The refusal is set on `lock`, a `monkeypatch.context()`, so leaving that
    context is the lock being released.
    """
    committed = _write_without_graph_repair(
        root,
        monkeypatch,
        {
            PAGE_A: _page("A", "A is revised against [[queue-b]]."),
            PAGE_C: _page("C", "C is new and cites nothing."),
        },
    )
    _refuse_reads(lock, root, PAGE_A, PermissionError("the page cannot be read"))
    _drain_ticks(root, epistemic_graph.GRAPH_POISON_ATTEMPTS)
    _age_graph_failures(root, _poison_min_age())
    _drain_ticks(root, 1)
    return committed


def test_a_busy_boundary_never_counts_against_a_page(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drain refused by a writer mid-batch says nothing about the page.

    `drain_paths` raising -- the canonical boundary busy (`MUTATION_BUSY`), a
    locked SQLite store -- is a readiness refusal. Counting it quarantined a
    healthy page under a steady writer and dropped its rows, leaving the graph
    unreadable until a whole-vault rebuild. Whatever raises out of the drain
    rotates, as it always did, however long it lasts.
    """
    from exomem.cli_ops import OpError

    committed = _write_without_graph_repair(
        vault, monkeypatch, {PAGE_A: _page("A", "A is revised against [[queue-b]].")}
    )
    before = _node_rows(vault, PAGE_A)
    assert before
    refusals: list[Exception] = [
        OpError("MUTATION_BUSY", "a writer holds the vault mutation boundary"),
        sqlite3.OperationalError("database is locked"),
    ]
    for refusal in refusals:

        def refuse(self: Any, paths: list[Path], error: Exception = refusal) -> Any:
            raise error

        with monkeypatch.context() as patch:
            patch.setattr(EpistemicGraphIndex, "drain_paths", refuse)
            _drain_ticks(vault, epistemic_graph.GRAPH_POISON_ATTEMPTS + 1)
            assert deferred_index.list_graph_paths(vault) == [PAGE_A], refusal
            assert epistemic_graph.graph_lag(vault)["quarantined_paths"] == 0, refusal
            # However long it lasts.
            _age_graph_failures(vault, 10 * _poison_min_age())
            _drain_ticks(vault, 1)

        assert deferred_index.list_graph_paths(vault) == [PAGE_A], refusal
        assert _failure_attempts(vault, PAGE_A) == 0, refusal
        assert epistemic_graph.graph_lag(vault)["quarantined_paths"] == 0, refusal
        assert _node_rows(vault, PAGE_A) == before, refusal

    index_sync.drain_graph_work(vault)

    assert deferred_index.list_graph_paths(vault) == []
    assert _acknowledged(vault) == committed
    assert EpistemicGraphIndex(vault).available()
    assert _graph_contents(vault) == _graph_contents_after_full_rebuild(vault)


def test_a_transient_read_lock_neither_quarantines_nor_drops_a_page(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page an editor or a sync tool holds for a moment is not poison.

    Its bytes refuse a few drain ticks in a row, then read normally. Counting
    those ticks quarantined the page and deleted its rows, so the graph stayed
    readable but without the page, and nothing repaired the drift. An `OSError`
    counts only once it has outlived `GRAPH_POISON_MIN_AGE_SECONDS`, measured
    in minutes rather than ticks; until then the receipt rotates and the rows
    of the page's last readable version stay.
    """
    committed = _write_without_graph_repair(
        vault, monkeypatch, {PAGE_A: _page("A", "A is revised against [[queue-b]].")}
    )
    before = _node_rows(vault, PAGE_A)
    assert before

    with monkeypatch.context() as patch:
        _refuse_reads(patch, vault, PAGE_A, PermissionError("the page is locked"))
        _drain_ticks(vault, epistemic_graph.GRAPH_POISON_ATTEMPTS + 2)

        assert deferred_index.list_graph_paths(vault) == [PAGE_A]
        assert epistemic_graph.graph_lag(vault)["quarantined_paths"] == 0
        assert _node_rows(vault, PAGE_A) == before

    index_sync.drain_graph_work(vault)

    assert deferred_index.list_graph_paths(vault) == []
    assert _failure_attempts(vault, PAGE_A) == 0, "a derived page kept its failures"
    assert _acknowledged(vault) == committed
    assert EpistemicGraphIndex(vault).available()
    assert epistemic_graph.graph_drift(vault) == []
    assert _graph_contents(vault) == _graph_contents_after_full_rebuild(vault)


def test_an_unreadable_page_is_quarantined_rather_than_rotated_forever(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page that stays unreadable past the minimum age stops being retried hot.

    Rotated forever, its receipt keeps the queue from ever emptying, so the
    drain never settles and the lag never clears. After
    `GRAPH_POISON_ATTEMPTS` failed isolated attempts spanning at least
    `GRAPH_POISON_MIN_AGE_SECONDS` the receipt leaves the queue by exact
    revision, and the lag and the doctor report the path. Quarantine only stops
    the hot retries: the page still exists, so the rows of its last readable
    version stay.
    """
    from exomem import doctor

    rows_before = _node_rows(vault, PAGE_A)
    assert rows_before
    with monkeypatch.context() as locked:
        committed = _quarantine_unreadable_page_a(vault, monkeypatch, locked)

    assert deferred_index.list_graph_paths(vault) == []
    assert _node_rows(vault, PAGE_A) == rows_before, "quarantine dropped an existing page's rows"
    lag = epistemic_graph.graph_lag(vault)
    assert lag["quarantined_paths"] == 1
    assert _acknowledged(vault) == committed
    assert EpistemicGraphIndex(vault).available()
    check = doctor._check_graph_sync_state(vault)
    assert check.status == "warn", check.message
    assert check.details is not None and check.details["quarantined_paths"] == 1


def test_undecodable_bytes_count_without_waiting_for_an_age(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bytes that are not UTF-8 are a property of the page, not of the moment.

    Every attempt counts; after `GRAPH_POISON_ATTEMPTS` the receipt is
    quarantined. The page exists, so its rows stay.
    """
    _write_without_graph_repair(
        vault, monkeypatch, {PAGE_A: _page("A", "A is revised against [[queue-b]].")}
    )
    before = _node_rows(vault, PAGE_A)
    assert before
    _undecodable_reads(monkeypatch, vault, PAGE_A)

    _drain_ticks(vault, epistemic_graph.GRAPH_POISON_ATTEMPTS)

    assert deferred_index.list_graph_paths(vault) == []
    assert epistemic_graph.graph_lag(vault)["quarantined_paths"] == 1
    assert _node_rows(vault, PAGE_A) == before


def test_a_quarantined_page_is_retried_when_it_changes_and_on_a_slow_timer(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quarantine is not forever: a changed page, or enough time, earns a retry.

    An out-of-band edit changes the page's stat signature, which re-queues it
    at the drain's next wake; with no change at all, it is re-queued once
    `GRAPH_QUARANTINE_RETRY_SECONDS` have passed since its last failure. A
    retry that derives the page forgets its failures.
    """
    with monkeypatch.context() as locked:
        _quarantine_unreadable_page_a(vault, monkeypatch, locked)
        assert epistemic_graph.graph_lag(vault)["quarantined_paths"] == 1

        # Unchanged, and not yet due: nothing is re-queued.
        assert index_sync.requeue_quarantined_graph_paths(vault) == 0
        assert deferred_index.list_graph_paths(vault) == []

        # Edited out of band while still locked: re-queued, fails, set aside again.
        (vault / PAGE_A).write_text(_page("A", "A is edited out of band."), encoding="utf-8")
        _seed_live_freshness(vault)
        assert index_sync.requeue_quarantined_graph_paths(vault) == 1
        assert deferred_index.list_graph_paths(vault) == [PAGE_A]
        index_sync.drain_graph_work(vault)
        assert deferred_index.list_graph_paths(vault) == []
        assert epistemic_graph.graph_lag(vault)["quarantined_paths"] == 1

    # The lock is released with no change to the page: the slow timer retries it.
    assert index_sync.requeue_quarantined_graph_paths(vault) == 0
    _age_graph_failures(vault, epistemic_graph.GRAPH_QUARANTINE_RETRY_SECONDS)
    assert index_sync.requeue_quarantined_graph_paths(vault) == 1
    index_sync.drain_graph_work(vault)

    assert deferred_index.list_graph_paths(vault) == []
    assert epistemic_graph.graph_lag(vault)["quarantined_paths"] == 0
    assert _failure_attempts(vault, PAGE_A) == 0
    assert _graph_contents(vault) == _graph_contents_after_full_rebuild(vault)


def test_a_full_rebuild_that_derives_a_quarantined_page_clears_its_record(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A whole-vault pass that reads the page answers the quarantine.

    The record outliving it left the doctor warning about a page that the
    published graph had just derived.
    """
    from exomem import doctor

    with monkeypatch.context() as locked:
        _quarantine_unreadable_page_a(vault, monkeypatch, locked)
        assert epistemic_graph.graph_lag(vault)["quarantined_paths"] == 1
    # The lock is released; the page itself is unchanged.

    EpistemicGraphIndex(vault).rebuild_all()

    assert epistemic_graph.graph_lag(vault)["quarantined_paths"] == 0
    assert _failure_attempts(vault, PAGE_A) == 0
    check = doctor._check_graph_sync_state(vault)
    assert not (check.details or {}).get("quarantined_paths"), check.message
    assert "could not be read" not in check.message
