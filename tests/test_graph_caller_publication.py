"""Restricted correction discovery keeps a coherent, caller-equivalent publication."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from test_governance_egress import _external
from test_relation_fragment_targets import SOURCE, TARGET, _built, _write
from test_unit_correction_context import _publish
from test_working_set_currency_security import _govern

from exomem import deferred_index, file_watcher, semantic_index, unit_corrections, vault
from exomem.governance import policy
from exomem.governance.principal import RequestPrincipal, request_scope


@pytest.fixture
def published_graph(tmp_path: Path) -> SimpleNamespace:
    root, index = _built(tmp_path)
    hidden = "Knowledge Base/Notes/Patterns/withheld-maintenance.md"
    later = "Knowledge Base/Notes/Patterns/withheld-later.md"
    source = "---\ntype: note\ntitle: Private maintenance\n---\n# Private maintenance\n\n{}\n"
    _write(root, hidden, source.format("Before."))
    _write(root, later, source.format("Before."))
    _govern(root)
    _publish(index)
    reference = (
        semantic_index.current_parent_index_state(root, TARGET)
        .document.resolve_fragment("rich-1").unit.unit_ref
    )
    return SimpleNamespace(root=root, index=index, hidden=hidden, later=later, source=source, reference=reference)


def _hidden_write(state: SimpleNamespace) -> None:
    with request_scope(RequestPrincipal(audience_id="owner", surface="cli")):
        with state.index._mutation_coordinator.hold():
            vault.batch_atomic_write(
                [vault.PlannedWrite(state.root / path, state.source.format("After."))
                 for path in (state.hidden, state.later)],
                vault_root=state.root, post_commit_fanout=False,
            )
            assert file_watcher.register_self_write_and_close(
                state.root, [state.root / state.hidden, state.root / state.later], (),
            )


def _discover(state: SimpleNamespace, *, accept=lambda _row: True) -> dict:
    with request_scope(_external()):
        return unit_corrections.discover(
            state.root, [state.reference], accept=accept,
            keep=unit_corrections.reader_view(state.root),
        )


def test_partial_hidden_repair_preserves_the_restricted_correction_context(
    published_graph: SimpleNamespace,
) -> None:
    state = published_graph
    root, index, hidden, later = state.root, state.index, state.hidden, state.later
    before = _discover(state)
    assert before["status"] == "available" and before["units"]

    _hidden_write(state)
    receipts = deferred_index.add_graph_receipts(root, [hidden, later])
    partial = index.drain_paths([root / hidden])
    assert partial["published"] is False
    assert any(row.rel_path == later for row in receipts)

    assert _discover(state) == before


def test_deleted_withheld_source_keeps_corrections_without_its_text_or_edges(
    published_graph: SimpleNamespace,
) -> None:
    from test_activate_context_request_path import _seed_live_recall
    from test_coverage_flags_caller_view import _hold_verdicts

    from exomem import graph_sync, index_sync, lexstore

    state = published_graph
    hidden = "Knowledge Base/Notes/Patterns/frag-target.md"
    _write(state.root, hidden, (
        "---\ntype: insight\n---\n# Private target\n\n## Decision\n"
        f"- category: config\n- id: hidden\n- relations: answers: [[{TARGET}#rich-1]]\n\n"
        "Private correction text.\n"
    ))
    _seed_live_recall(state.root)
    _publish(state.index)
    assert lexstore.get_store(state.root).rebuild_atomic()
    assert hidden not in _hold_verdicts(state.root)
    before = _discover(state)
    assert before["status"] == "available" and before["units"]
    assert all(row["path"] != hidden for row in before["units"])

    with state.index._mutation_coordinator.hold():
        epoch = graph_sync.prepare_deletion_epoch(state.root, [hidden])
        assert epoch is not None
        (state.root / hidden).unlink()
        graph_sync.commit_deletion_epoch(epoch)
    assert index_sync.publish_corpus_delta(
        state.root, deleted=[state.root / hidden], populate_on_miss=False,
    )

    assert _discover(state) == before


def test_a_withheld_verdict_for_changed_source_bytes_cannot_hide_graph_deletion(
    published_graph: SimpleNamespace,
) -> None:
    from test_activate_context_request_path import _seed_live_recall
    from test_coverage_flags_caller_view import _hold_verdicts, _withhold_from_external

    from exomem import freshness, graph_read_publication, graph_sync, index_sync, lexstore

    state = published_graph
    _withhold_from_external(state.root, 'tags: ["private-proof"]')
    _seed_live_recall(state.root)
    _publish(state.index)
    assert lexstore.get_store(state.root).rebuild_atomic()
    before = _discover(state)
    assert before["status"] == "available" and before["units"]
    store = lexstore.get_store(state.root)
    checkpoint = store.published_recall_checkpoint("kb")
    publication = graph_read_publication.published(state.root)
    path = state.root / SOURCE
    with request_scope(RequestPrincipal(audience_id="owner", surface="cli")), state.index._mutation_coordinator.hold():
        vault.batch_atomic_write(
            [vault.PlannedWrite(path, path.read_text().replace("type: insight\n", "type: insight\ntags: [private-proof]\n"))],
            vault_root=state.root, post_commit_fanout=False,
        )
        assert file_watcher.register_self_write_and_close(state.root, [path], ())
    store.apply_catalog_delta("kb", freshness.recall_delta_since(state.root, "kb", checkpoint))
    assert store.published_recall_checkpoint("vault") == publication.recall
    assert SOURCE not in _hold_verdicts(state.root)
    with state.index._mutation_coordinator.hold():
        epoch = graph_sync.prepare_deletion_epoch(state.root, [SOURCE])
        assert epoch is not None
        path.unlink()
        graph_sync.commit_deletion_epoch(epoch)
    assert index_sync.publish_corpus_delta(state.root, deleted=[path], populate_on_miss=False)

    assert _discover(state) == {"status": "unavailable", "units": []}


@pytest.mark.parametrize("reset_all", [False, True])
def test_idle_release_retains_context_until_an_explicit_resolver_reset(
    published_graph: SimpleNamespace, reset_all: bool,
) -> None:
    from exomem import find

    state = published_graph
    before = _discover(state)
    assert before["status"] == "available" and before["units"]
    _hidden_write(state)
    assert state.index.drain_paths([state.root / state.hidden])["published"] is False
    find.release_idle_ram_caches()
    assert _discover(state) == before

    if reset_all:
        find.unload_ram_caches()
    else:
        find.evict_resolver_caches(state.root)
    assert _discover(state) == {"status": "unavailable", "units": []}


def test_partial_hidden_repair_keeps_the_compilers_served_packet_whole(
    published_graph: SimpleNamespace,
) -> None:
    from test_unit_correction_context import _target_packet

    state = published_graph
    with request_scope(_external()):
        before = _target_packet(state.root)
    assert before["generation"]["correction_context"] == "available"
    assert len(before["units"]) > 1

    _hidden_write(state)
    assert state.index.drain_paths([state.root / state.hidden])["published"] is False

    with request_scope(_external()):
        assert _target_packet(state.root) == before


def test_hidden_publication_during_hydration_keeps_the_correction_context(
    published_graph: SimpleNamespace,
) -> None:
    state = published_graph
    before = _discover(state)
    assert before["status"] == "available" and before["units"]
    published = []

    def accept(_row):
        if not published:
            _hidden_write(state)
            report = state.index.drain_paths([state.root / state.hidden, state.root / state.later])
            assert report["published"] is True
            published.append(True)
        return True

    assert _discover(state, accept=accept) == before
    assert published


@pytest.mark.parametrize("phase", ["before", "during"])
def test_foreign_resolver_commit_without_local_observation_withdraws_corrections(
    published_graph: SimpleNamespace, phase: str,
) -> None:
    state = published_graph
    baseline = _discover(state)
    assert baseline["status"] == "available" and baseline["units"]
    committed = []

    def foreign_commit():
        with request_scope(RequestPrincipal(audience_id="owner", surface="cli")):
            with state.index._mutation_coordinator.hold():
                vault.batch_atomic_write(
                    [vault.PlannedWrite(state.root / "Journal/competing-title.md", "# Frag target\n")],
                    vault_root=state.root, post_commit_fanout=False,
                )
        committed.append(True)

    def accept(_row):
        if not committed:
            foreign_commit()
        return True

    if phase == "before":
        foreign_commit()
    assert _discover(state, accept=accept) == {"status": "unavailable", "units": []}
    assert committed


def test_visible_publication_during_a_retained_read_discards_all_corrections(
    published_graph: SimpleNamespace,
) -> None:
    state = published_graph
    _hidden_write(state)
    assert _discover(state)["status"] == "available"
    committed = []

    def accept(_row):
        if not committed:
            path = state.root / SOURCE
            with request_scope(RequestPrincipal(audience_id="owner", surface="cli")):
                with state.index._mutation_coordinator.hold():
                    vault.batch_atomic_write(
                        [vault.PlannedWrite(path, path.read_text().replace("Source body.", "Changed answer."))],
                        vault_root=state.root, post_commit_fanout=False,
                    )
                    assert file_watcher.register_self_write_and_close(state.root, [path], ())
            committed.append(True)
        return True

    assert _discover(state, accept=accept) == {"status": "unavailable", "units": []}
    assert committed


def test_revocation_during_hydration_ignores_the_requests_cached_policy(
    published_graph: SimpleNamespace,
) -> None:
    from test_coverage_flags_caller_view import _withhold_from_external

    state = published_graph
    _hidden_write(state)
    revoked = []

    def accept(_row):
        if not revoked:
            _withhold_from_external(state.root, f'paths: ["{SOURCE}"]')
            revoked.append(True)
        return True

    with request_scope(_external()), policy.request_policy_snapshot():
        result = unit_corrections.discover(
            state.root, [state.reference], accept=accept,
            keep=unit_corrections.reader_view(state.root),
        )
    assert result == {"status": "unavailable", "units": []}
    assert revoked


@pytest.mark.parametrize("phase", ["before", "during", "recovered"])
@pytest.mark.parametrize("retained", [True, False], ids=["retained", "fallback"])
def test_foreign_integrity_purge_discards_retained_corrections(
    published_graph: SimpleNamespace, phase: str, retained: bool,
) -> None:
    import os
    import subprocess
    import sys

    from exomem import graph_read_publication

    state = published_graph
    if not retained:
        graph_read_publication.forget(state.root)
    baseline = _discover(state)
    assert baseline["status"] == "available" and baseline["units"]
    purged = []

    def purge():
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(Path(__file__).resolve().parents[1] / "src"), environment.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        completed = subprocess.run(
            [sys.executable, "-c", """
import sys
from pathlib import Path
from exomem import epistemic_graph, freshness, graph_publication, mutation_lock, vault
root = Path(sys.argv[1])
coordinator = mutation_lock.VaultMutationCoordinator(Path(sys.argv[2]), root)
index = epistemic_graph.EpistemicGraphIndex(root, mutation_coordinator=coordinator)
assert index.purge_exact_persisted_rows(node_paths=[sys.argv[3]], edge_values={}) > 0
with index._connect_existing(readonly=True) as conn:
    assert graph_publication.open_view(conn) is None
if sys.argv[4] == "recovered":
    freshness.seed(root, "vault", ((str(path), freshness.stat_signature(path))
                                  for path in vault.walk_vault_md(root)))
    index.rebuild_all()
    with index._connect_existing(readonly=True) as conn:
        assert graph_publication.open_view(conn) is not None
""", str(state.root), str(state.index._mutation_coordinator.state_root), SOURCE, phase],
            capture_output=True, text=True, env=environment, timeout=20, check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        purged.append(True)

    def accept(_row):
        if not purged:
            purge()
        return True

    if phase == "before":
        purge()
    assert _discover(state, accept=accept) == {"status": "unavailable", "units": []}
    assert purged


def test_one_readers_sql_deadline_does_not_cancel_an_independent_reader(
    published_graph: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from exomem import epistemic_graph

    state = published_graph
    _hidden_write(state)
    baseline = _discover(state)
    assert baseline["status"] == "available" and baseline["units"]
    local = threading.local()
    gate = threading.Barrier(2)
    first_at_scan = threading.Event()
    real_clock = time.monotonic
    scan = epistemic_graph.GraphView.neighbor_edges

    monkeypatch.setattr(time, "monotonic", lambda: real_clock() + getattr(local, "advance", 0))

    def concurrent_scan(view, *args, **kwargs):
        if local.expire:
            first_at_scan.set()
        gate.wait(timeout=10)
        if local.expire:
            local.advance = 60
            view.conn.execute(
                "WITH RECURSIVE n(x) AS (SELECT 0 UNION ALL SELECT x+1 FROM n WHERE x<100000) "
                "SELECT sum(x) FROM n"
            ).fetchone()
            pytest.fail("the expired reader's SQL work was not interrupted")
        return scan(view, *args, **kwargs)

    monkeypatch.setattr(epistemic_graph.GraphView, "neighbor_edges", concurrent_scan)

    def discover(expire):
        local.expire = expire
        with request_scope(_external()):
            return unit_corrections.discover(
                state.root, [state.reference], accept=lambda _row: True,
                keep=unit_corrections.reader_view(state.root), deadline=real_clock() + 30,
            )

    with ThreadPoolExecutor(max_workers=2) as readers:
        expired_read = readers.submit(discover, True)
        assert first_at_scan.wait(10)
        current_read = readers.submit(discover, False)
        expired, current = expired_read.result(), current_read.result()
    assert expired == {"status": "budget_exhausted", "units": []}
    assert current == baseline


def test_simultaneous_reader_admission_keeps_both_correction_contexts_available(
    published_graph: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager

    from exomem import epistemic_graph, mutation_lock

    state = published_graph
    _hidden_write(state)
    baseline = _discover(state)
    assert baseline["status"] == "available" and baseline["units"]
    local = threading.local()
    first_open = threading.Event()
    release = threading.Event()
    other_probed = threading.Event()
    connect = epistemic_graph.EpistemicGraphIndex._connect_existing
    hold = mutation_lock.VaultMutationCoordinator.try_hold

    def paused_connect(index, *args, **kwargs):
        conn = connect(index, *args, **kwargs)
        if getattr(local, "first", False) and not first_open.is_set():
            first_open.set()
            assert release.wait(10)
        return conn

    @contextmanager
    def observed_hold(coordinator, *args, **kwargs):
        with hold(coordinator, *args, **kwargs) as held:
            if not getattr(local, "first", True) and not held:
                other_probed.set()
            yield held

    monkeypatch.setattr(epistemic_graph.EpistemicGraphIndex, "_connect_existing", paused_connect)
    monkeypatch.setattr(mutation_lock.VaultMutationCoordinator, "try_hold", observed_hold)

    def discover(first):
        local.first = first
        return _discover(state)

    with ThreadPoolExecutor(max_workers=2) as readers:
        first = readers.submit(discover, True)
        assert first_open.wait(10)
        second = readers.submit(discover, False)
        try:
            other_probed.wait(0.25)
        finally:
            release.set()
        assert first.result() == baseline
        assert second.result() == baseline


def test_expiring_behind_another_readers_proof_reports_the_spent_budget(
    published_graph: SimpleNamespace,
) -> None:
    import time
    from concurrent.futures import ThreadPoolExecutor

    from exomem import graph_read_publication

    state = published_graph
    _hidden_write(state)

    def discover():
        with request_scope(_external()):
            return unit_corrections.discover(
                state.root, [state.reference], accept=lambda _row: True,
                keep=unit_corrections.reader_view(state.root), deadline=time.monotonic() + 0.05,
            )

    with ThreadPoolExecutor(max_workers=1) as reader:
        with graph_read_publication.read_boundary(
            state.root, operation="test_reader_proof", deadline=time.monotonic() + 10,
        ) as held:
            assert held
            result = reader.submit(discover).result(timeout=2)
    assert result == {"status": "budget_exhausted", "units": []}


def test_an_active_canonical_writer_still_declines_without_waiting_for_its_release(
    published_graph: SimpleNamespace,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    state = published_graph
    _hidden_write(state)
    with ThreadPoolExecutor(max_workers=1) as reader:
        with state.index._mutation_coordinator.hold():
            result = reader.submit(_discover, state).result(timeout=2)
    assert result == {"status": "unavailable", "units": []}
