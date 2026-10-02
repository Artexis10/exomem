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
