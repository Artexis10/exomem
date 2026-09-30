"""A busy lexical sidecar never buys a whole-corpus Python BM25 index (bound-cell-memory D6).

`bm25.warm` runs at start-up and on every drifted reconcile. When FTS5 owns
the lexical lane but cannot serve this pass -- another connection holds the
sidecar's write lock, or it is not yet synced -- building the in-process
BM25Okapi corpus instead holds every page's tokens resident: the memory the
sidecar exists to avoid, paid for a transient. The warm declines and the next
pass retries through FTS5. Only an absent (unsupported or retired) sidecar, or
the explicit python backend, warms the Python rung.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from exomem import bm25, lexstore


def _write_page(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    title = Path(rel).stem
    p.write_text(
        f"---\ntype: insight\ntitle: {title}\nupdated: 2026-01-01\n---\n# {title}\n\n{body}\n",
        encoding="utf-8",
    )


def _wait_for_lexical_repair_idle(timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    wake = threading.Event()
    while True:
        with lexstore._REPAIRS_LOCK:
            if not lexstore._REPAIRS_IN_FLIGHT:
                return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pytest.fail("lexical repair worker did not become idle")
        wake.wait(min(0.01, remaining))


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch):
    lexstore.reset_memo()
    lexstore.clear_stores()
    bm25.clear_cache()
    monkeypatch.delenv("EXOMEM_LEXICAL_BACKEND", raising=False)
    yield
    _wait_for_lexical_repair_idle()
    lexstore.reset_memo()
    lexstore.clear_stores()
    bm25.clear_cache()


needs_fts5 = pytest.mark.skipif(
    not lexstore.fts5_available(), reason="SQLite build lacks FTS5"
)


@pytest.fixture
def python_builds(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every whole-corpus Python BM25 build."""
    builds: list[str] = []
    real_build = bm25.BM25Index._build

    def spy(self, vault_root, scope):  # noqa: ANN001, ANN202
        builds.append(scope)
        return real_build(self, vault_root, scope)

    monkeypatch.setattr(bm25.BM25Index, "_build", spy)
    return builds


def _synced_vault(root: Path) -> None:
    _write_page(root, "Knowledge Base/a.md", "kubernetes ingress configuration")
    _write_page(root, "Knowledge Base/b.md", "sourdough hydration ratios")
    assert bm25.warm(root, "kb") == "fts5"
    assert lexstore.lexical_path(root).exists()
    lexstore.clear_stores()


@needs_fts5
def test_a_locked_sidecar_declines_the_warm_and_the_next_warm_uses_fts5(
    tmp_path: Path, python_builds: list[str]
) -> None:
    _synced_vault(tmp_path)

    holder = sqlite3.connect(lexstore.lexical_path(tmp_path), isolation_level=None)
    try:
        # Exclusive locking mode keeps the lock past the statement and out of
        # WAL's shared-memory reader path, so no other connection may read.
        holder.execute("PRAGMA locking_mode=EXCLUSIVE")
        holder.execute("BEGIN EXCLUSIVE")
        assert bm25.warm(tmp_path, "kb") == "declined"
        assert bm25.cache_status()["loaded"] is False
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert lexstore.get_store(tmp_path)._failed is False
    assert bm25.warm(tmp_path, "kb") == "fts5"
    assert python_builds == []
    assert bm25.cache_status()["loaded"] is False


@needs_fts5
def test_an_unsynced_sidecar_declines_the_warm_and_the_next_warm_uses_fts5(
    tmp_path: Path, python_builds: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _synced_vault(tmp_path)
    store = lexstore.get_store(tmp_path)
    real_ensure_synced = store._ensure_synced
    scheduled: list[Path] = []
    monkeypatch.setattr(store, "_ensure_synced", lambda *_a, **_k: False)
    monkeypatch.setattr(lexstore, "_schedule_repair", lambda root, **_k: scheduled.append(root))

    assert bm25.warm(tmp_path, "kb") == "declined"
    assert scheduled, "an unsynced sidecar schedules its own repair"
    assert python_builds == []

    monkeypatch.setattr(store, "_ensure_synced", real_ensure_synced)
    assert bm25.warm(tmp_path, "kb") == "fts5"
    assert python_builds == []
    assert bm25.cache_status()["loaded"] is False


def test_the_explicit_python_backend_still_warms_the_python_rung(
    tmp_path: Path, python_builds: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    _write_page(tmp_path, "Knowledge Base/a.md", "kubernetes ingress configuration")
    assert bm25.warm(tmp_path, "kb") == "python"
    assert python_builds == ["kb"]


@needs_fts5
def test_a_retired_sidecar_warms_the_python_rung(
    tmp_path: Path, python_builds: list[str]
) -> None:
    """A fatally failed sidecar is retired for the process: it is absent, not busy."""
    _synced_vault(tmp_path)
    lexstore.get_store(tmp_path)._failed = True
    assert bm25.warm(tmp_path, "kb") == "python"
    assert python_builds == ["kb"]
