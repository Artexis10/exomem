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

from collections.abc import Iterator
from pathlib import Path

import pytest

from exomem import find as find_module
from exomem import freshness, lexstore, readiness, warmup
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
        (
            (str(path), freshness.stat_signature(path))
            for path in vault_module.walk_vault_md(root)
        ),
    )


def _start_managed_process(root: Path) -> None:
    """Start a serving process the way activation does, against inherited state."""
    lexstore.clear_stores()
    freshness.clear()
    readiness.reset()
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
        if getattr(store.published_recall_checkpoint(scope), "instance_id", None)
        == instance_id
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


def test_first_write_after_restart_keeps_retrieval_admitted(inherited_catalog: Path) -> None:
    root = inherited_catalog
    _start_managed_process(root)
    assert readiness.retrieval_admission(root)["admitted"], (
        "the inherited catalog is exactly current, so warm-up admits it"
    )

    _governed_write(root)
    assert lexstore.await_repairs_idle(root)

    # The health probe's own read-only proof, exactly as /health/ready runs it.
    assert readiness.retrieval_admission(root) == {"state": "ready", "admitted": True}
    # And the write stayed O(delta): both scopes were blessed at this process's
    # live checkpoint by the write itself, with no repair owner involved.
    assert _scopes_published_by(root, freshness.instance_id()) == {"kb", "vault"}
    assert lexstore.search_bm25(root, "quokkarestartmarker", k=3, scope="kb")
