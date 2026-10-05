"""Service recovery must leave a small-save caller beside one bulk parent."""
from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import deferred_index, embeddings, index_sync, runtime_resources


def test_small_receipt_publishes_while_bulk_parent_is_still_running(vault: Path, monkeypatch) -> None:
    """The former whole-parent replay owner left every new small save behind it."""
    from exomem import semantic_drain

    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_RESOURCE_POLICY", "service-v1")
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    bulk = vault / "Knowledge Base/bulk.md"
    small = vault / "Knowledge Base/small.md"
    bulk.write_text("# Bulk\n\n" + "paragraph\n\n" * 300, encoding="utf-8")
    small.write_text("# Small\n\nA new fact.\n", encoding="utf-8")
    bulk_started = threading.Event()
    finish_bulk = threading.Event()
    small_published = threading.Event()
    gate = runtime_resources.ModelAdmissionGate(2)

    def execute(root, paths, **kwargs):
        with gate.admission():
            with gate.execution():
                if paths == [bulk]:
                    bulk_started.set()
            if paths == [bulk]:
                assert finish_bulk.wait(timeout=5)
            with gate.execution():
                if paths == [small]:
                    assert not finish_bulk.is_set()
                    small_published.set()
        return SimpleNamespace(status="completed", code="embedding_upsert_completed", publication=SimpleNamespace(current=lambda _root: True))

    monkeypatch.setattr(embeddings, "upsert_after_write_status", execute)
    deferred_index.add_receipts(vault, [bulk.relative_to(vault).as_posix()])
    owner = semantic_drain.start(vault)
    try:
        assert owner is not None and bulk_started.wait(timeout=5)
        semantic_drain.request(vault, [small], edited=True)
        assert small_published.wait(timeout=5)
        assert not finish_bulk.is_set()
        assert deferred_index.snapshot(vault, paths={bulk.relative_to(vault).as_posix()})
    finally:
        finish_bulk.set()
        if owner is not None:
            owner.stop(timeout=5)


def test_durable_small_receipt_wakes_without_waiting_for_terminal(vault: Path, monkeypatch) -> None:
    """Slow terminal/advisory work must not leave published debt unhinted."""
    from exomem import semantic_drain, writer_lease

    target = vault / "Knowledge Base/small.md"
    target.write_text("# Small\n\nA new fact.\n", encoding="utf-8")
    rel = target.relative_to(vault).as_posix()
    notified = []

    def signal(root, paths):
        # Wake-up carries only a hint for rows already durably committed.
        assert [row.rel_path for row in deferred_index.snapshot(root)] == [rel]
        notified.extend(paths)

    monkeypatch.setattr(semantic_drain, "signal", signal)
    monkeypatch.setattr(embeddings, "upsert_after_write_status", lambda *a, **kw: pytest.fail("inline encode"))
    token = writer_lease._ACTIVE_POST_TERMINAL_HOUSEKEEPING.set([])
    try:
        assert semantic_drain.request(vault, [target], edited=True) == 1
        assert notified == [rel]
    finally:
        writer_lease._ACTIVE_POST_TERMINAL_HOUSEKEEPING.reset(token)


def test_stopped_attempt_cannot_retire_or_delay_a_superseding_edit(vault: Path, monkeypatch) -> None:
    """Shutdown during encoding leaves exact newer custody intact for restart."""
    from exomem import semantic_drain

    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_RESOURCE_POLICY", "service-v1")
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    target = vault / "Knowledge Base/bulk.md"
    target.write_text("# Bulk\n\n" + "paragraph\n\n" * 300, encoding="utf-8")
    rel = target.relative_to(vault).as_posix()
    begun = threading.Event()
    release = threading.Event()

    def execute(*args, **kwargs):
        begun.set()
        assert release.wait(timeout=5)
        return SimpleNamespace(status="completed", code="embedding_upsert_completed", publication=SimpleNamespace(current=lambda _root: False))

    monkeypatch.setattr(embeddings, "upsert_after_write_status", execute)
    deferred_index.add_receipts(vault, [rel])
    owner = semantic_drain.start(vault)
    try:
        assert owner is not None and begun.wait(timeout=5)
        target.write_text("# Changed\n", encoding="utf-8")
        [newer] = deferred_index.add_receipts(vault, [rel])
        with pytest.raises(RuntimeError, match="still running"):
            owner.stop(timeout=0.01)
        release.set()
        owner.stop(timeout=5)
        [retained] = deferred_index.snapshot(vault)
        assert retained.revision == newer.revision
        assert retained.attempt_count == 0
    finally:
        release.set()
        if owner is not None:
            owner.stop(timeout=5)


def test_service_replay_leaves_exact_custody_to_the_single_owner(vault: Path, monkeypatch) -> None:
    """Warmup and reconcile must neither encode nor retire debt from mtime."""
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_RESOURCE_POLICY", "service-v1")
    target = vault / "Knowledge Base/replay.md"
    target.write_text("# Replay\n", encoding="utf-8")
    rel = target.relative_to(vault).as_posix()
    [receipt] = deferred_index.add_receipts(vault, [rel])
    monkeypatch.setattr(embeddings, "upsert_after_write_status", lambda *a, **kw: pytest.fail("competing encoder"))
    monkeypatch.setattr(deferred_index, "inspect_embedding_freshness", lambda *a, **kw: {rel: deferred_index.EmbeddingFreshness.CURRENT})
    assert index_sync.replay_deferred_embedding(vault, [target], [receipt]).status == "deferred"
    assert index_sync.drain_deferred_work(vault, limit=4) == 0
    assert deferred_index.snapshot(vault) == [receipt]


def test_service_derived_custody_waits_for_exact_publication(vault: Path, monkeypatch) -> None:
    """Queue handoff and component memo cannot stand in for indexed after-state."""
    from exomem import semantic_drain
    from exomem.derived_receipts import DerivedComponent

    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_RESOURCE_POLICY", "service-v1")
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    target = vault / "Knowledge Base/derived.md"
    target.write_text("# Derived\n", encoding="utf-8")
    rel = target.relative_to(vault).as_posix()
    receipt = SimpleNamespace(batch_id="test-batch", canonical_generation="test-generation", paths=[SimpleNamespace(rel_path=rel, before_hash="before", after_hash="after")])
    monkeypatch.setattr(index_sync, "upsert_after_write", lambda *a, **kw: pytest.fail("component replay re-encoded"))
    monkeypatch.setattr(semantic_drain, "publication_ready", lambda *a, **kw: False)
    assert index_sync.converge_derived_component(vault, receipt, DerivedComponent.EMBEDDINGS) == "pending"
    [original] = deferred_index.snapshot(vault)
    assert index_sync.converge_derived_component(vault, receipt, DerivedComponent.EMBEDDINGS) == "pending"
    assert deferred_index.snapshot(vault) == [original]
    monkeypatch.setattr(semantic_drain, "publication_ready", lambda *a, **kw: kw.get("expected_hash") == "after")
    assert index_sync.converge_derived_component(vault, receipt, DerivedComponent.EMBEDDINGS) is True


def test_occupied_bulk_receipts_do_not_consume_small_execution_slots(vault: Path, monkeypatch) -> None:
    """A lost/coalesced hint must not leave small debt behind skipped imports."""
    from exomem import semantic_drain

    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_RESOURCE_POLICY", "service-v1")
    paths = []
    for number in range(semantic_drain.TURN_LIMIT + 2):
        path = vault / f"Knowledge Base/import-{number:02d}.md"
        path.write_text("# Import\n\n" + "bulk text\n" * 200, encoding="utf-8")
        paths.append(path.relative_to(vault).as_posix())
    small = vault / "Knowledge Base/z-small.md"
    small.write_text("# Small\n\nCurrent fact.\n", encoding="utf-8")
    paths.append(small.relative_to(vault).as_posix())
    deferred_index.add_receipts(vault, paths)
    owner = semantic_drain.SemanticDrain(vault)
    owner._bulk_path = paths[0]
    executed = []
    monkeypatch.setattr(owner, "_execute", lambda receipt, *_args: executed.append(receipt.rel_path))
    owner._turn()
    assert executed == [paths[-1]]


@pytest.mark.parametrize("bulk_name, save_prefix", [("a-import", "z-save"), ("z-import", "a-save")])
def test_continuous_small_hints_cannot_starve_older_durable_bulk(vault: Path, monkeypatch, bulk_name, save_prefix) -> None:
    """Fresh saves must leave an admission opportunity for unhinted import debt."""
    from exomem import semantic_drain

    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_RESOURCE_POLICY", "service-v1")
    bulk = vault / f"Knowledge Base/{bulk_name}.md"
    bulk.write_text("# Import\n\n" + "bulk text\n" * 200, encoding="utf-8")
    bulk_rel = bulk.relative_to(vault).as_posix()
    deferred_index.add_receipts(vault, [bulk_rel])
    owner = semantic_drain.SemanticDrain(vault)
    completed = set()

    def execute(_root, paths, **_kwargs):
        completed.add(paths[0].relative_to(vault).as_posix())
        return SimpleNamespace(status="completed", code="embedding_upsert_completed", publication=SimpleNamespace(current=lambda _root: True))

    monkeypatch.setattr(embeddings, "upsert_after_write_status", execute)
    try:
        for turn in range(3):
            rels = []
            for number in range(semantic_drain.TURN_LIMIT):
                path = vault / f"Knowledge Base/{save_prefix}-{turn}-{number}.md"
                path.write_text("# Save\n\nA new fact.\n", encoding="utf-8")
                rels.append(path.relative_to(vault).as_posix())
            deferred_index.add_receipts(vault, rels)
            owner.signal(rels)
            owner._turn()
            if owner._bulk_thread is not None:
                owner._bulk_thread.join(timeout=5)
        assert bulk_rel in completed
        assert any(rel != bulk_rel for rel in completed)
        assert not deferred_index.snapshot(vault, paths={bulk_rel})
    finally:
        owner.stop(timeout=5)


def test_scan_revisits_skipped_bulk_despite_refusals_and_continuous_arrivals(vault: Path, monkeypatch) -> None:
    """Hint overflow and a tied clock must not strand bulk behind a moving tail."""
    from exomem import semantic_drain

    monkeypatch.setattr(deferred_index.time, "time", lambda: 1000.0)
    monkeypatch.setattr(semantic_drain, "preparation_policy", lambda _root: "policy")
    owner = semantic_drain.SemanticDrain(vault)
    owner._bulk_path = "occupied"
    bulk_rel = "Knowledge Base/z-bulk.md"
    bulk_examined = threading.Event()
    completed = []
    arrivals = set()

    def queue(rel):
        path = vault / rel
        path.write_text("# Input\n\nA fact.\n", encoding="utf-8")
        [receipt] = deferred_index.add_receipts(vault, [rel])
        return receipt

    for number in range(semantic_drain.SCAN_LIMIT):
        receipt = queue(f"Knowledge Base/a-refused-{number:03d}.md")
        deferred_index.retry_semantic(
            vault, receipt, failure_code="resource_budget_exceeded",
            input_signature=semantic_drain._input_signature(vault / receipt.rel_path),
            policy="policy",
        )
    initial = [f"Knowledge Base/a-save-0000-{number:03d}.md" for number in range(semantic_drain.SCAN_LIMIT)]
    for rel in initial:
        queue(rel)
    queue(bulk_rel)
    owner.signal(initial)

    def classify(_root, path):
        if path.relative_to(vault).as_posix() == bulk_rel:
            bulk_examined.set()
            return False
        return True

    def execute(receipt, *_args):
        completed.append(receipt.rel_path)
        assert deferred_index.clear_receipts(vault, [receipt]) == 1

    monkeypatch.setattr(semantic_drain, "_small_parent", classify)
    monkeypatch.setattr(owner, "_execute", execute)
    try:
        for turn in range(1, 65):
            fresh = [f"Knowledge Base/a-save-{turn:04d}-{number:03d}.md" for number in range(semantic_drain.TURN_LIMIT)]
            arrivals.update(fresh)
            for rel in fresh:
                queue(rel)
            owner.signal(fresh)
            before = len(completed)
            owner._turn()
            if owner._bulk_thread is not None:
                owner._bulk_thread.join(timeout=5)
            assert len(completed) - before <= semantic_drain.TURN_LIMIT
            assert any(rel != bulk_rel for rel in completed[before:])
            if bulk_examined.is_set():
                owner._bulk_path = None
            if bulk_rel in completed:
                break
        assert bulk_examined.is_set()
        assert bulk_rel in completed
        assert arrivals.intersection(completed)
        assert not deferred_index.snapshot(vault, paths={bulk_rel})
    finally:
        owner.stop(timeout=5)


def _loop_sleeps(owner, turns: int) -> list[float]:
    """How long the real `_run` loop sleeps after each of its first `turns` turns."""
    sleeps: list[float] = []

    class Wake:
        def wait(self, timeout=None):
            sleeps.append(timeout)
            if len(sleeps) > turns:
                owner._stop.set()

        def clear(self):
            pass

        def set(self):
            pass

    owner._wake = Wake()
    owner._run()
    return sleeps[1:]  # the first wait is the startup sleep, before any turn


def _queue_refused(vault: Path, count: int, *, policy: str = "policy") -> None:
    from exomem import semantic_drain

    for number in range(count):
        rel = f"Knowledge Base/refused-{number:03d}.md"
        (vault / rel).write_text("# Input\n\nA fact.\n", encoding="utf-8")
        [receipt] = deferred_index.add_receipts(vault, [rel])
        deferred_index.retry_semantic(
            vault, receipt, failure_code="resource_budget_exceeded",
            input_signature=semantic_drain._input_signature(vault / rel), policy=policy,
        )


def test_input_gated_refusal_does_not_drive_the_one_second_cadence(vault: Path, monkeypatch) -> None:
    """A parent refused for budget waits for changed input; rescanning it every second is idle CPU."""
    from exomem import semantic_drain

    monkeypatch.setattr(semantic_drain, "preparation_policy", lambda _root: "policy")
    _queue_refused(vault, 1)
    assert _loop_sleeps(semantic_drain.SemanticDrain(vault), turns=2) == [semantic_drain.IDLE_POLL_SECONDS] * 2


def test_refused_backlog_filling_exactly_one_page_settles_to_the_idle_poll(vault: Path, monkeypatch) -> None:
    """A last page that is exactly full is still the end of the sweep, not a reason to poll at 1 Hz."""
    from exomem import semantic_drain

    monkeypatch.setattr(semantic_drain, "preparation_policy", lambda _root: "policy")
    _queue_refused(vault, semantic_drain.SCAN_LIMIT)
    assert _loop_sleeps(semantic_drain.SemanticDrain(vault), turns=2) == [semantic_drain.IDLE_POLL_SECONDS] * 2


def test_retry_written_behind_the_sweep_cursor_wakes_the_loop_when_due(vault: Path, monkeypatch) -> None:
    """A retry on an already-swept row is still due on time; a backlog past one page keeps the fast cadence."""
    import time

    from exomem import semantic_drain

    monkeypatch.setattr(semantic_drain, "preparation_policy", lambda _root: "policy")
    _queue_refused(vault, semantic_drain.SCAN_LIMIT + 1)
    owner = semantic_drain.SemanticDrain(vault)
    assert owner._turn() == (False, semantic_drain.RETRY_POLL_SECONDS)
    [first] = deferred_index.snapshot(vault, paths={"Knowledge Base/refused-000.md"})
    deferred_index.retry_semantic(vault, first, failure_code="preparation_busy", now=time.time())
    progressed, wait = owner._turn()
    assert not progressed
    assert wait < semantic_drain.IDLE_POLL_SECONDS / 2


def test_time_gated_retry_wakes_the_loop_when_it_falls_due(vault: Path, monkeypatch) -> None:
    """Dropping the cadence must not park a due retry behind a full idle poll."""
    import time

    from exomem import semantic_drain

    monkeypatch.setattr(semantic_drain, "preparation_policy", lambda _root: "policy")
    rel = "Knowledge Base/retry.md"
    (vault / rel).write_text("# Input\n\nA fact.\n", encoding="utf-8")
    [receipt] = deferred_index.add_receipts(vault, [rel])
    deferred_index.retry_semantic(vault, receipt, failure_code="embedding_failed", now=time.time() + 4.0)
    [sleep] = _loop_sleeps(semantic_drain.SemanticDrain(vault), turns=1)
    assert 3.0 < sleep <= 5.0


def test_receipt_held_back_by_the_occupied_bulk_slot_idles_and_the_bulk_finishing_wakes_the_drain(
    vault: Path, monkeypatch
) -> None:
    """Polling cannot free the slot; the bulk thread's own wake is what resumes the sweep."""
    from exomem import semantic_drain

    monkeypatch.setattr(semantic_drain, "preparation_policy", lambda _root: "policy")
    monkeypatch.setattr(semantic_drain, "_small_parent", lambda *_a: False)
    monkeypatch.setattr(semantic_drain.SemanticDrain, "_execute", lambda *_a: None)
    rel = "Knowledge Base/bulk.md"
    (vault / rel).write_text("# Bulk\n\nA fact.\n", encoding="utf-8")
    [receipt] = deferred_index.add_receipts(vault, [rel])
    owner = semantic_drain.SemanticDrain(vault)
    owner._bulk_path = "Knowledge Base/other.md"
    assert _loop_sleeps(owner, turns=1) == [semantic_drain.IDLE_POLL_SECONDS]

    owner = semantic_drain.SemanticDrain(vault)
    owner._bulk_path = "Knowledge Base/other.md"
    owner._bulk(receipt, "signature", "policy")
    assert owner._bulk_path is None
    assert owner._wake.is_set()
