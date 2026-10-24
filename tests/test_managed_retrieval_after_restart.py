"""Managed retrieval admission must survive the first write after a restart.

The production shape (Exomem Cloud P3 rehearsal, #1378): a cell's pod is
replaced (an upgrade, a pod kill, or leaving read-only mode, which is a
rollout), the new process inherits a lexical catalog whose persisted recall
checkpoints carry the previous process's registry instance id, and warm-up
admits retrieval because the inherited catalog describes exactly the projected
corpus. The first governed write then applies its rows but cannot bless the
`kb` scope: `recall_delta_since` refuses a foreign origin nobody adopted. The
graph adopts the `vault` scope's origin for its own reasons, so only `kb` was
stranded. The next `/health/ready` probe proves the catalog behind the live
projection and revokes admission, and because that probe is side-effect free
and the write reported its rows applied, no repair owner ever ran. The cell
answered `retrieval_unavailable` until the next restart.

These tests drive the real warm-up, write and readiness seams against a real
vault in temporary state. A process restart is a fresh `freshness` registry
(new instance id, reseeded from disk) and fresh lexical store handles.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from exomem import find as find_module
from exomem import file_watcher, freshness, lexstore, readiness, warmup
from exomem import reconcile as reconcile_module
from exomem import vault as vault_module

NOTE = "Knowledge Base/Notes/Insights/after-restart-probe.md"
NOTE_TEXT = (
    "---\n"
    "type: insight\n"
    "status: draft\n"
    "created: 2026-09-25\n"
    "updated: 2026-09-25\n"
    "sources: []\n"
    "---\n\n"
    "# After restart probe\n\n"
    "The first write after a worker replacement keeps retrieval admitted "
    "(quokkarestartmarker).\n"
)


def _seed_live_scopes(root: Path) -> None:
    kb = root / "Knowledge Base"
    freshness.seed(
        root,
        "kb",
        ((str(path), freshness.stat_signature(path)) for path in find_module._walk_md(kb)),
    )
    freshness.seed(
        root,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(root)),
    )


def _start_managed_process(root: Path, *, seed_before_warmup: bool = True) -> None:
    """Start a serving process the way activation does, against inherited state."""
    lexstore.clear_stores()
    freshness.clear()
    readiness.reset()
    if seed_before_warmup:
        _seed_live_scopes(root)
    readiness.manage_runtime()
    readiness.begin_warm()
    try:
        warmup.warm_retrieval_catalog(root)
    finally:
        readiness.finish_warm()


def _governed_write(root: Path) -> None:
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(path=root / NOTE, content=NOTE_TEXT)],
        vault_root=root,
    )


def _scopes_published_by(root: Path, instance_id: str) -> set[str]:
    store = lexstore.get_store(root)
    return {
        scope
        for scope in ("kb", "vault")
        if getattr(store.published_recall_checkpoint(scope), "instance_id", None) == instance_id
    }


@pytest.fixture
def inherited_catalog(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A vault whose lexical catalog an earlier process published."""
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "auto")
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    freshness.clear()
    _seed_live_scopes(vault)
    lexstore.ensure_fresh(vault)
    previous_instance = freshness.instance_id()
    assert _scopes_published_by(vault, previous_instance) == {"kb", "vault"}
    try:
        yield vault
    finally:
        lexstore.await_repairs_idle(vault)
        readiness.reset()
        lexstore.clear_stores()
        freshness.clear()


@pytest.mark.parametrize("seed_before_warmup", [True, False])
def test_first_write_after_restart_keeps_retrieval_admitted(
    inherited_catalog: Path, seed_before_warmup: bool
) -> None:
    root = inherited_catalog
    _start_managed_process(root, seed_before_warmup=seed_before_warmup)
    assert readiness.retrieval_admission(root)["admitted"], (
        "the inherited catalog is exactly current, so warm-up admits it"
    )

    _governed_write(root)
    assert lexstore.await_repairs_idle(root)

    # The health probe's own read-only proof, exactly as /health/ready runs it.
    assert readiness.retrieval_admission(root) == {"state": "ready", "admitted": True}
    # And the write stayed O(delta): both scopes were blessed at this process's
    # live checkpoint by the write itself, with no repair owner involved.
    store = lexstore.get_store(root)
    assert all(
        store.published_recall_checkpoint(scope) == freshness.recall_checkpoint(root, scope)
        for scope in freshness.SCOPES
    )
    assert lexstore.repair_progress(root) is None, "no catalog repair was needed"
    assert lexstore.search_bm25(root, "quokkarestartmarker", k=3, scope="kb")


def test_watcher_seed_completes_while_catalog_publication_is_busy(
    inherited_catalog: Path,
) -> None:
    """A busy catalog publisher must not hold the watcher seed open."""
    root = inherited_catalog
    store = lexstore.get_store(root)
    before = {scope: store.published_recall_checkpoint(scope) for scope in freshness.SCOPES}
    watcher = file_watcher.FileWatcher(root)
    completed = threading.Event()
    seeded: list[bool] = []

    def seed() -> None:
        try:
            seeded.append(watcher._reconcile_once(seed=True))
        finally:
            completed.set()

    worker = threading.Thread(target=seed)
    try:
        with store._publication_lock():
            worker.start()
            assert completed.wait(timeout=1.0)
            assert seeded == [True]
            assert all(freshness.recall_is_live(root, scope) for scope in freshness.SCOPES)
            assert {
                scope: store.published_recall_checkpoint(scope) for scope in freshness.SCOPES
            } == before
    finally:
        worker.join(timeout=1.0)
    assert not worker.is_alive()


def test_first_write_after_watcher_reseed_needs_no_catalog_repair(
    inherited_catalog: Path,
) -> None:
    root = inherited_catalog
    _start_managed_process(root)
    store = lexstore.get_store(root)
    before = {scope: store.published_recall_checkpoint(scope) for scope in freshness.SCOPES}

    assert file_watcher.FileWatcher(root)._reconcile_once(seed=True)
    assert all(
        store.published_recall_checkpoint(scope) != before[scope] for scope in freshness.SCOPES
    )
    _governed_write(root)
    assert lexstore.await_repairs_idle(root)

    assert all(
        store.published_recall_checkpoint(scope) == freshness.recall_checkpoint(root, scope)
        for scope in freshness.SCOPES
    )
    assert readiness.retrieval_admission(root) == {"state": "ready", "admitted": True}
    assert lexstore.repair_progress(root) is None, "no catalog repair was needed"


def test_first_write_after_reconcile_reseed_needs_no_catalog_repair(
    inherited_catalog: Path,
) -> None:
    root = inherited_catalog
    _start_managed_process(root)
    store = lexstore.get_store(root)

    reconcile_module.reconcile(root)
    _governed_write(root)
    assert lexstore.await_repairs_idle(root)

    assert all(
        store.published_recall_checkpoint(scope) == freshness.recall_checkpoint(root, scope)
        for scope in freshness.SCOPES
    )
    assert readiness.retrieval_admission(root) == {"state": "ready", "admitted": True}
    assert lexstore.repair_progress(root) is None, "no catalog repair was needed"


def test_a_rebaseline_with_unindexed_rows_still_needs_full_repair(
    inherited_catalog: Path,
) -> None:
    """A fresh registry cannot attest rows that the catalogue never indexed."""
    root = inherited_catalog
    _start_managed_process(root)
    store = lexstore.get_store(root)
    before = {scope: store.published_recall_checkpoint(scope) for scope in freshness.SCOPES}
    outside = root / "Knowledge Base/Notes/Insights/outside-editor.md"
    outside.write_text(NOTE_TEXT.replace("quokkarestartmarker", "unindexedmarker"), encoding="utf-8")

    assert all(freshness.rebaseline(root).values())
    for scope in freshness.SCOPES:
        assert store.published_recall_checkpoint(scope) == before[scope]
        assert freshness.recall_checkpoint(root, scope).triple != before[scope].triple

    _governed_write(root)
    assert lexstore.await_repairs_idle(root)

    assert (lexstore.repair_progress(root) or {}).get("last_result") == "published"
    assert readiness.retrieval_admission(root) == {"state": "ready", "admitted": True}
    assert lexstore.search_bm25(root, "unindexedmarker", k=3, scope="kb")
    assert lexstore.search_bm25(root, "quokkarestartmarker", k=3, scope="kb")


def test_a_write_that_cannot_bless_a_scope_hands_it_to_the_repair_owner(
    inherited_catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever strands a scope, readiness must converge rather than stay stuck.

    Without the warm-up lineage rebase the write reproduces the rehearsal's
    stranded `kb` scope exactly. Readiness may honestly drop while the catalog
    is behind the live projection, but the repair owner must then publish a
    current catalog and readmission must follow, with no restart.
    """
    root = inherited_catalog
    monkeypatch.setattr(lexstore, "rebase_current_catalog_lineage", lambda _root: ())
    _start_managed_process(root)
    assert readiness.retrieval_admission(root)["admitted"]

    _governed_write(root)
    assert lexstore.await_repairs_idle(root)

    assert readiness.retrieval_admission(root) == {"state": "ready", "admitted": True}
    assert (lexstore.repair_progress(root) or {}).get("last_result") == "published"
    assert _scopes_published_by(root, freshness.instance_id()) == {"kb", "vault"}
    assert lexstore.search_bm25(root, "quokkarestartmarker", k=3, scope="kb")


def test_a_healed_scope_is_not_handed_to_repair_again(
    inherited_catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stranding the repair owner already healed must not cost another rebuild.

    The repair worker's targeted retry of a contended foreground upsert passes
    the same witness step and can record a stranded scope without handing it
    off; its own promotion proof repairs it. The next ordinary write, which
    blesses both scopes cleanly, must then neither revoke admission nor
    rebuild the whole catalogue on that stale record.
    """
    root = inherited_catalog
    monkeypatch.setattr(lexstore, "rebase_current_catalog_lineage", lambda _root: ())
    _start_managed_process(root)

    real_lock = lexstore.LexicalStore._publication_lock
    contended = {"foreground": True}

    def lock(self, timeout=lexstore._PUBLICATION_TIMEOUT_BACKGROUND):
        if contended["foreground"] and timeout == lexstore._PUBLICATION_TIMEOUT_FOREGROUND:
            raise vault_module.VaultLockTimeout("VAULT_LOCK_TIMEOUT", "contended")
        return real_lock(self, timeout=timeout)

    monkeypatch.setattr(lexstore.LexicalStore, "_publication_lock", lock)
    _governed_write(root)
    contended["foreground"] = False
    assert lexstore.await_repairs_idle(root)
    assert readiness.retrieval_admission(root) == {"state": "ready", "admitted": True}

    rebuilds: list[int] = []
    real_rebuild = lexstore.LexicalStore.rebuild_atomic

    def counted(self):
        rebuilds.append(1)
        return real_rebuild(self)

    monkeypatch.setattr(lexstore.LexicalStore, "rebuild_atomic", counted)
    vault_module.batch_atomic_write(
        [
            vault_module.PlannedWrite(
                path=root / "Knowledge Base/Notes/Insights/second-probe.md",
                content=NOTE_TEXT.replace("After restart probe", "Second probe"),
            )
        ],
        vault_root=root,
    )
    assert lexstore.await_repairs_idle(root)

    assert rebuilds == []
    assert readiness.retrieval_admission(root) == {"state": "ready", "admitted": True}
