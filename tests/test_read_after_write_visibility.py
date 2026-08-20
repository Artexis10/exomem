"""Read-after-write visibility on the production FTS5 keyword lane.

These tests exercise the governed semantic-write seam.  They intentionally do
not write through a mock catalog: the cold path builds the real FTS5 sidecar,
and the contended path drives the real publication barrier and repair worker.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from exomem import find as find_module
from exomem import graph_sync, lexstore, semantic_writes


pytestmark = pytest.mark.skipif(
    not lexstore.fts5_available(), reason="SQLite build lacks FTS5"
)

_PAGE = "Knowledge Base/Entities/Concepts/read-after-write-visibility.md"


def _source(marker: str) -> str:
    return (
        "---\n"
        "title: Read-after-write visibility\n"
        "type: entity\n"
        "status: active\n"
        "updated: 2026-08-20\n"
        "---\n\n"
        "# Read-after-write visibility\n\n"
        "## Observations\n\n"
        f"- [config] {marker} #visibility (test) ^visibility-gate\n"
    )


def _seed_page(vault_root: Path, marker: str) -> Path:
    path = vault_root / _PAGE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_source(marker), encoding="utf-8")
    return path


def _governed_transition(vault_root: Path, marker: str) -> None:
    preflight = semantic_writes.preflight_existing(
        vault_root,
        path=_PAGE,
        after_source=_source(marker),
        operation="observe",
    )
    assert preflight.contract_result.should_block is False
    committed = semantic_writes.commit_existing(vault_root, preflight=preflight)
    assert committed.mutated is True


def _keyword_paths(vault_root: Path, marker: str, *, failed: list[str] | None = None) -> list[str]:
    hits = find_module.find(
        vault_root,
        query=marker,
        scope="kb-only",
        mode="keyword",
        limit=5,
        failed_out=failed,
    )
    return [hit.path for hit in hits]


def _wait_for_repair_idle(vault_root: Path, timeout: float = 30.0) -> None:
    """Wait on the repair-flight condition; the timeout is only a deadlock valve."""
    key = vault_root.resolve()
    deadline = time.monotonic() + timeout
    wake = threading.Event()
    while True:
        with lexstore._REPAIRS_LOCK:
            if key not in lexstore._REPAIRS_IN_FLIGHT:
                return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pytest.fail("lexical repair worker did not become idle")
        wake.wait(min(0.01, remaining))


@pytest.fixture(autouse=True)
def _fts5_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "fts5")
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    find_module.clear_cache()
    find_module.reset_degradation_counts()
    lexstore.reset_memo()
    lexstore.clear_stores()
    yield
    graph_sync.drain_active_rebuilds()
    find_module.clear_cache()
    find_module.reset_degradation_counts()
    lexstore.reset_memo()
    lexstore.clear_stores()


def test_governed_write_is_visible_to_the_next_fts5_keyword_read(tmp_path: Path) -> None:
    _seed_page(tmp_path, "visibility-before-cold-catalog")

    marker = "visibility-after-cold-catalog"
    _governed_transition(tmp_path, marker)

    assert _keyword_paths(tmp_path, marker) == [_PAGE]
    assert lexstore.lexical_path(tmp_path).exists()


def test_deferred_upsert_stays_visible_while_targeted_repair_is_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _seed_page(tmp_path, "visibility-before-deferred-upsert")
    lexstore.ensure_fresh(tmp_path)
    assert lexstore.lexical_path(tmp_path).exists()
    store = lexstore.get_store(tmp_path)

    repair_started = threading.Event()
    allow_repair = threading.Event()
    repair_finished = threading.Event()
    deferred: list[list[Path]] = []
    real_retry = store.retry_deferred_upsert

    def controlled_retry(paths: list[Path]) -> bool:
        deferred.append(list(paths))
        repair_started.set()
        if not allow_repair.wait(30):
            raise AssertionError("test did not release the targeted lexical repair")
        try:
            return real_retry(paths)
        finally:
            repair_finished.set()

    monkeypatch.setattr(store, "retry_deferred_upsert", controlled_retry)

    marker = "visibility-during-deferred-upsert"
    try:
        _governed_transition(tmp_path, marker)
        assert repair_started.wait(30), "governed write did not drive the deferred-upsert path"
        assert deferred == [[page]]

        # The targeted worker is deliberately still pending.  The next reader
        # must nevertheless see the committed bytes through the catalog sync or
        # the reference-scan fallback; it may not return a false empty.
        assert _keyword_paths(tmp_path, marker) == [_PAGE]
    finally:
        allow_repair.set()

    assert repair_finished.wait(30), "targeted lexical repair did not finish"
    _wait_for_repair_idle(tmp_path)


def test_declining_catalog_falls_back_and_records_keyword_degradation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_page(tmp_path, "visibility-before-declining-catalog")
    marker = "visibility-after-declining-catalog"
    _governed_transition(tmp_path, marker)
    find_module.clear_cache()

    monkeypatch.setattr(lexstore, "search_substring", lambda *_args, **_kwargs: None)
    failed: list[str] = []

    assert _keyword_paths(tmp_path, marker, failed=failed) == [_PAGE]
    assert failed == ["keyword_lexical"]
    assert find_module.degradation_counts().get("keyword_lexical") == 1
