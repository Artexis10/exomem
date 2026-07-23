"""Task 1.3 (reliability) — RED: narrow SQLite classification + read policy.

Pins OpenSpec change ``restore-indexed-category-recall`` decision 6 and specs
*Transient SQLite Failure Is Recoverable* and *Ordinary Reads Do Not Negotiate
Journal Mode*:

* ``SQLITE_BUSY`` / ``SQLITE_LOCKED`` / ``SQLITE_INTERRUPT`` and their canonical
  busy/locked messages fail only the current operation and never set a sticky
  process-lifetime retirement — the next call recovers without a restart;
* ``SQLITE_CORRUPT`` / ``SQLITE_NOTADB`` may mark the disposable sidecar fatal;
* ordinary read connections set bounded busy/synchronous policy but MUST NOT run
  ``PRAGMA journal_mode=WAL`` — journal negotiation belongs to setup/rebuild.

RED until ``lexstore.classify_sqlite_error`` exists, transient failures stop
retiring the store, and ordinary reads no longer negotiate journal mode.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from exomem import find as find_module
from exomem import lexstore

needs_fts5 = pytest.mark.skipif(
    not lexstore.fts5_available(), reason="this SQLite build lacks FTS5"
)


@pytest.fixture(autouse=True)
def _fresh_state() -> Any:
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()
    yield
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()


def _write_page(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    page_id = uuid.uuid5(uuid.NAMESPACE_URL, f"reliability:{rel}")
    path.write_text(
        f"---\ntype: insight\ntitle: {path.stem}\nexomem_id: {page_id}\n"
        f"updated: 2026-01-01\n---\n# {path.stem}\n\n{body}\n",
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------- #
# Explicit, narrow error classification.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "error",
    [
        sqlite3.OperationalError("database is locked"),
        sqlite3.OperationalError("database table is locked"),
        sqlite3.OperationalError("interrupted"),
    ],
)
def test_busy_locked_interrupt_are_transient(error: sqlite3.Error) -> None:
    assert lexstore.classify_sqlite_error(error) == "transient"


@pytest.mark.parametrize(
    "error",
    [
        sqlite3.DatabaseError("database disk image is malformed"),
        sqlite3.DatabaseError("file is not a database"),
    ],
)
def test_corrupt_and_notadb_are_fatal(error: sqlite3.Error) -> None:
    assert lexstore.classify_sqlite_error(error) == "fatal"


def test_other_operational_errors_are_not_fatal() -> None:
    # A generic operational error degrades the current call but does not prove a
    # fatal code, so it must not be classified fatal (no sticky retirement).
    assert (
        lexstore.classify_sqlite_error(sqlite3.OperationalError("no such column: x")) != "fatal"
    )


# --------------------------------------------------------------------------- #
# Transient failures are recoverable; fatal failures retire the sidecar.
# --------------------------------------------------------------------------- #


@needs_fts5
def test_transient_lock_does_not_retire_store_and_next_call_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_page(tmp_path, "Knowledge Base/a.md", "recoverable searchable payload")
    assert lexstore.search_bm25(tmp_path, "searchable", 5)  # warm build
    store = lexstore.get_store(tmp_path)
    real_query = store._bm25_query
    calls = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_query(*args, **kwargs)

    monkeypatch.setattr(store, "_bm25_query", flaky)

    # The locked call fails transiently for the current operation only...
    assert lexstore.search_bm25(tmp_path, "searchable", 5) is None
    assert getattr(store, "_failed", False) is False
    # ...and the very next call, same process, opens a fresh connection and works.
    recovered = lexstore.search_bm25(tmp_path, "searchable", 5)
    assert recovered and recovered[0][0] == "Knowledge Base/a.md"


@needs_fts5
def test_corrupt_error_retires_the_disposable_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_page(tmp_path, "Knowledge Base/a.md", "corruptible searchable payload")
    assert lexstore.search_bm25(tmp_path, "searchable", 5)  # warm build
    store = lexstore.get_store(tmp_path)
    real_query = store._bm25_query

    def corrupt(*_args: Any, **_kwargs: Any) -> Any:
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(store, "_bm25_query", corrupt)
    assert lexstore.search_bm25(tmp_path, "searchable", 5) is None

    # A proven-fatal code retires the sidecar: even after the fault clears the
    # retired store keeps declining rather than reusing the corrupt file.
    monkeypatch.setattr(store, "_bm25_query", real_query)
    assert lexstore.search_bm25(tmp_path, "searchable", 5) is None


# --------------------------------------------------------------------------- #
# Ordinary reads do not negotiate journal mode.
# --------------------------------------------------------------------------- #


@needs_fts5
def test_ordinary_read_does_not_negotiate_journal_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_page(tmp_path, "Knowledge Base/a.md", "journal policy searchable payload")
    from exomem import bm25

    snapshot = find_module.FreshnessSnapshot(tmp_path)
    # Warm build (setup/rebuild MAY negotiate journal mode) before we start
    # recording, so only the ordinary-read connection is under test.
    assert lexstore.search_bm25(
        tmp_path, "searchable", 5, scope="kb", freshness=snapshot.kb()
    )

    recorded: list[str] = []
    real_connect = sqlite3.connect

    class RecordingConnection(sqlite3.Connection):
        def execute(self, sql: str, *args: Any) -> Any:  # type: ignore[override]
            recorded.append(sql)
            return super().execute(sql, *args)

    def spy_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs.setdefault("factory", RecordingConnection)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(lexstore.sqlite3, "connect", spy_connect)

    # An ordinary read against an already-fresh sidecar.
    hits = lexstore.search_bm25(
        tmp_path, "searchable", 5, scope="kb", freshness=bm25.corpus_key(tmp_path, "kb")
    )
    assert hits and hits[0][0] == "Knowledge Base/a.md"
    assert not any("journal_mode" in sql.lower() for sql in recorded), recorded


def test_no_fts_writer_hooks_maintain_normal_table_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Optional FTS absence must not disable category catalog dual-writes."""
    _write_page(tmp_path, "Knowledge Base/existing.md", "- [rule] existing ^existing")
    monkeypatch.setattr(lexstore, "fts5_available", lambda: False)
    lexstore.ensure_fresh(tmp_path)

    added = _write_page(
        tmp_path,
        "Knowledge Base/added.md",
        "- [constraint] maintained without FTS ^added",
    )
    lexstore.upsert_after_write(tmp_path, [added])

    sidecar = lexstore.lexical_path(tmp_path)
    with sqlite3.connect(sidecar) as conn:
        row = conn.execute(
            "SELECT category FROM semantic_units WHERE parent_path = ?",
            ("Knowledge Base/added.md",),
        ).fetchone()
    assert row == ("constraint",)

    added.unlink()
    lexstore.delete_after_remove(tmp_path, ["Knowledge Base/added.md"])
    with sqlite3.connect(sidecar) as conn:
        remaining = conn.execute(
            "SELECT count(*) FROM semantic_units WHERE parent_path = ?",
            ("Knowledge Base/added.md",),
        ).fetchone()
    assert remaining == (0,)
