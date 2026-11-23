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
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from exomem import find as find_module
from exomem import freshness, lexstore, path_mentions, request_budget

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


@pytest.mark.parametrize("expired", [False, True])
def test_budgeted_sqlite_contention_fails_promptly_and_restores_the_reader(
    tmp_path: Path, expired: bool
) -> None:
    path = tmp_path / "catalogue.sqlite"
    with closing(sqlite3.connect(path)) as writer, closing(sqlite3.connect(path)) as reader:
        writer.execute("CREATE TABLE pages (title TEXT)")
        writer.execute("INSERT INTO pages VALUES ('example')")
        writer.commit()
        reader.execute("PRAGMA busy_timeout=2300")
        readiness = lexstore.CatalogReadiness("available", True, "fts5")
        budget = request_budget.RequestBudget(seconds=30)

        def read(conn: sqlite3.Connection):
            conn.execute("SELECT 1").fetchone()
            writer.execute("BEGIN EXCLUSIVE")
            if expired:
                budget.deadline = time.monotonic()
            return conn.execute("SELECT title FROM pages").fetchall()

        token = request_budget.set_current(budget)
        started = time.monotonic()
        try:
            with lexstore.stopping_at_budget() as stop:
                if expired:
                    result = lexstore.LexicalStore._query_within_budget(reader, read, readiness)
                    assert result.readiness == lexstore.CatalogReadiness(lexstore.DEADLINE_STATUS, False, "fts5")
                    assert result.value is None
                else:
                    with pytest.raises(sqlite3.OperationalError) as raised:
                        lexstore.LexicalStore._query_within_budget(reader, read, readiness)
                    assert raised.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
            assert stop.hit == expired
        finally:
            request_budget.reset_current(token)
            writer.rollback()

        assert time.monotonic() - started < 1.0
        assert reader.execute("PRAGMA busy_timeout").fetchone() == (2300,)
        assert reader.execute("SELECT title FROM pages").fetchall() == [("example",)]


@needs_fts5
def test_catalogue_budget_preserves_the_admitted_bm25_answer(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    rel = "Knowledge Base/Notes/budget-marker.md"
    _write_page(vault, rel, "budgetmarker")
    warm_managed_cell(vault)
    store = lexstore.get_store(vault)

    def read():
        return lexstore.search_bm25_result(vault, "budgetmarker", 5, allow_delta=False)

    with lexstore.admitted_catalog_scope(vault), lexstore.catalog_selection_scope(vault) as admitted:
        assert admitted is not None
        with monkeypatch.context() as patch:
            patch.setattr(store, "_connect", lambda: pytest.fail("query must reuse the admitted reader"))
            expected = read()
            assert expected.readiness.complete and [path for path, _ in expected.value] == [rel]
            budget = request_budget.RequestBudget(seconds=30)
            token = request_budget.set_current(budget)
            try:
                with lexstore.stopping_at_budget() as stop:
                    assert read() == expected
                assert not stop.hit
                budget.deadline = time.monotonic()
                with patch.context() as expired:
                    expired.setattr(store, "_bm25_query", lambda *a, **kw: pytest.fail("expired query ran"))
                    with lexstore.stopping_at_budget() as stop:
                        result = read()
                assert result.readiness.status == lexstore.DEADLINE_STATUS
                assert result.value is None and stop.hit
                assert read() == expected
            finally:
                request_budget.reset_current(token)


def test_catalogue_read_discards_a_result_built_past_its_deadline() -> None:
    readiness = lexstore.CatalogReadiness("available", True, "fts5")
    budget = request_budget.RequestBudget(seconds=30)

    def query(conn: sqlite3.Connection):
        value = conn.execute("SELECT 1").fetchone()
        budget.deadline = time.monotonic()
        return value

    with closing(sqlite3.connect(":memory:")) as reader:
        token = request_budget.set_current(budget)
        try:
            with lexstore.stopping_at_budget() as stop:
                result = lexstore.LexicalStore._query_within_budget(reader, query, readiness)
        finally:
            request_budget.reset_current(token)
    assert result.readiness == lexstore.CatalogReadiness(lexstore.DEADLINE_STATUS, False, "fts5")
    assert result.value is None and stop.hit


def test_nested_catalogue_read_restores_the_outer_interrupt() -> None:
    readiness = lexstore.CatalogReadiness("available", True, "fts5")
    budget = request_budget.RequestBudget(seconds=30)
    finished = False

    def read_outer(conn: sqlite3.Connection):
        nonlocal finished
        with request_budget.capped(5), lexstore.stopping_at_budget():
            inner = lexstore.LexicalStore._query_within_budget(
                conn, lambda reader: reader.execute("SELECT 1").fetchone(), readiness
            )
        assert inner.value == (1,)
        budget.deadline = time.monotonic()
        result = conn.execute(
            "WITH RECURSIVE x(n) AS (VALUES(1) UNION ALL SELECT n + 1 FROM x WHERE n < 1000000) "
            "SELECT count(*) FROM x"
        ).fetchone()
        finished = True
        return result

    with closing(sqlite3.connect(":memory:")) as reader:
        token = request_budget.set_current(budget)
        try:
            with lexstore.stopping_at_budget() as stop:
                result = lexstore.LexicalStore._query_within_budget(reader, read_outer, readiness)
                assert stop.hit
        finally:
            request_budget.reset_current(token)
    assert result.readiness.status == lexstore.DEADLINE_STATUS
    assert result.value is None
    assert not finished


def test_unrelated_sqlite_error_keeps_its_meaning_at_the_deadline() -> None:
    readiness = lexstore.CatalogReadiness("available", True, "fts5")
    budget = request_budget.RequestBudget(seconds=30)

    def query(conn: sqlite3.Connection):
        budget.deadline = time.monotonic()
        return conn.execute("SELECT missing_column")

    with closing(sqlite3.connect(":memory:")) as reader:
        token = request_budget.set_current(budget)
        try:
            with lexstore.stopping_at_budget() as stop, pytest.raises(sqlite3.OperationalError) as raised:
                lexstore.LexicalStore._query_within_budget(reader, query, readiness)
            assert raised.value.sqlite_errorcode == sqlite3.SQLITE_ERROR
            assert not stop.hit
        finally:
            request_budget.reset_current(token)


@needs_fts5
def test_published_substrings_answer_existing_wal_rows(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    rel = "Knowledge Base/Notes/referrer.md"
    _write_page(vault, rel, "Sources/Documents/a.csv")
    warm_managed_cell(vault, prebuild_refs=False)
    store = lexstore.get_store(vault)
    # A published checkpoint in the WAL must be read even when the main file
    # has a different stamp.
    real_connect = lexstore._sqlite_connect_owned
    with closing(real_connect(store.path)) as writer:
        assert writer.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE meta SET value = 'old' WHERE key = 'recall_checkpoint:kb'")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        target = freshness.live_recall_checkpoint(vault, "kb")
        assert target is not None
        writer.execute(
            "UPDATE meta SET value = ? WHERE key = 'recall_checkpoint:kb'", (repr(tuple(target)),)
        )
        writer.commit()
        monkeypatch.setattr(store, "_connect", lambda: pytest.fail("ordinary reader opened"))

        result = lexstore.search_current_published_substrings(
            vault, ["sources/documents/a.csv", "sources/documents/missing.csv"]
        )

    assert result.readiness.complete
    assert result.value == {"sources/documents/a.csv": [rel], "sources/documents/missing.csv": []}


@needs_fts5
def test_published_prose_candidate_limit_applies_to_the_whole_query_batch(
    vault: Path, warm_managed_cell,
) -> None:
    first, second = "Sources/Documents/first.csv", "Sources/Documents/second.csv"
    a, b = "Knowledge Base/Notes/a.md", "Knowledge Base/Notes/b.md"
    _write_page(vault, a, first)
    _write_page(vault, b, second)
    warm_managed_cell(vault, prebuild_refs=False)

    complete = path_mentions.find_pages(vault, [first, second], candidate_limit=2)
    assert complete.complete and complete.pages == (a, b)
    assert not complete.overflow

    overflow = path_mentions.find_pages(vault, [first, second], candidate_limit=1)
    assert overflow.overflow and not overflow.complete
    assert not overflow.pages


@needs_fts5
@pytest.mark.parametrize("digest_cached", [False, True], ids=["cold-digest", "lagged-sidecar"])
def test_published_substrings_never_derive_or_catch_up_on_request(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch, digest_cached: bool
) -> None:
    _write_page(vault, "Knowledge Base/Notes/referrer.md", "old-needle")
    warm_managed_cell(vault, prebuild_refs=False)
    changed = _write_page(vault, "Knowledge Base/Notes/referrer.md", "replacement-needle")
    freshness.on_files_changed(vault, changed=[changed])
    if digest_cached:
        assert freshness.live_recall_checkpoint(vault, "kb") is not None

    def unexpected(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("published lookup started foreground index work")

    monkeypatch.setattr(freshness, "triple_from_entries", unexpected)
    monkeypatch.setattr(freshness, "live_recall_entries", unexpected)
    monkeypatch.setattr(freshness, "recall_checkpoint", unexpected)
    monkeypatch.setattr(freshness, "recall_delta_since_live", unexpected)
    monkeypatch.setattr(lexstore.get_store(vault), "catalog_readiness", unexpected)
    monkeypatch.setattr(lexstore, "_schedule_repair", unexpected)

    result = lexstore.search_current_published_substrings(vault, ["old-needle"])

    assert result.readiness.status == "stale"
    assert not result.readiness.complete
    assert result.value is None


@needs_fts5
def test_missing_published_catalogue_recovers_with_one_background_repair(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    rel = "Knowledge Base/Notes/referrer.md"
    _write_page(vault, rel, "recovery-needle")
    warm_managed_cell(vault, prebuild_refs=False)
    store = lexstore.get_store(vault)
    store.path.unlink()
    caller = threading.current_thread()
    attempts: list[threading.Thread] = []
    started = threading.Event()
    release = threading.Event()
    real_rebuild = store.rebuild_atomic
    real_generation = lexstore._live_repair_generation

    def rebuild() -> bool:
        attempts.append(threading.current_thread())
        started.set()
        assert release.wait(5)
        return real_rebuild()

    def generation(root: Path) -> tuple[object, ...] | None:
        if threading.current_thread() is caller:
            pytest.fail("the foreground request sampled a repair generation")
        return real_generation(root)

    monkeypatch.setattr(store, "rebuild_atomic", rebuild)
    monkeypatch.setattr(lexstore, "_live_repair_generation", generation)
    check = request_budget.ReadCheckpoint(request_budget.RequestBudget(seconds=20))
    try:
        with request_budget.checking(check):
            first = lexstore.search_current_published_substrings(vault, ["recovery-needle"])
            assert not first.readiness.complete and first.value is None
            assert started.wait(2), "the missing sidecar did not start repair"
            second = lexstore.search_current_published_substrings(vault, ["recovery-needle"])
            assert not second.readiness.complete and second.value is None
    finally:
        release.set()
        assert lexstore.await_repairs_idle(vault, timeout=10)

    recovered = lexstore.search_current_published_substrings(vault, ["recovery-needle"])
    assert recovered.readiness.complete
    assert recovered.value == {"recovery-needle": [rel]}
    assert len(attempts) == 1 and attempts[0] is not caller


@needs_fts5
@pytest.mark.parametrize("expire_at_open", [False, True])
def test_published_substrings_decline_a_sqlite_lock_during_proof(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch, expire_at_open: bool
) -> None:
    rel = "Knowledge Base/Notes/referrer.md"
    _write_page(vault, rel, "published-needle")
    warm_managed_cell(vault, prebuild_refs=False)
    path = lexstore.get_store(vault).path
    real_connect = lexstore._sqlite_connect_owned
    check = request_budget.ReadCheckpoint(request_budget.RequestBudget(seconds=30))
    with closing(real_connect(path, timeout=0)) as writer, monkeypatch.context() as patch:
        assert writer.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone() == ("exclusive",)

        def lock_after_open(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            conn = real_connect(*args, **kwargs)
            writer.execute("BEGIN EXCLUSIVE")
            if expire_at_open:
                check.budget.deadline = time.monotonic()
            return conn

        patch.setattr(lexstore, "_sqlite_connect_owned", lock_after_open)
        patch.setattr(lexstore, "_schedule_repair", lambda *a, **kw: pytest.fail("a busy reader queued repair"))
        started = time.monotonic()
        with request_budget.checking(check):
            result = lexstore.search_current_published_substrings(vault, ["published-needle"])
        assert time.monotonic() - started < 1.0
        assert result.readiness.status == (
            lexstore.DEADLINE_STATUS if expire_at_open else "transient_failure"
        )
        assert result.value is None

    recovered = lexstore.search_current_published_substrings(vault, ["published-needle"])
    assert recovered.readiness.complete
    assert recovered.value == {"published-needle": [rel]}


@needs_fts5
@pytest.mark.parametrize("candidate_limit", [None, 0])
def test_published_substrings_drop_rows_if_a_watcher_event_arrives_during_query(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch, candidate_limit: int | None,
) -> None:
    referrer = _write_page(vault, "Knowledge Base/Notes/referrer.md", "published-needle")
    warm_managed_cell(vault, prebuild_refs=False)
    store = lexstore.get_store(vault)
    query = store._matched_substring_paths

    def read_then_observe_change(*args: Any, **kwargs: Any) -> list[str]:
        rows = query(*args, **kwargs)
        assert rows == ["Knowledge Base/Notes/referrer.md"]
        freshness.mark_external_pending(vault, paths=[referrer])
        return rows

    monkeypatch.setattr(store, "_matched_substring_paths", read_then_observe_change)
    result = lexstore.search_current_published_substrings(
        vault, ["published-needle"], candidate_limit=candidate_limit,
    )

    assert not result.readiness.complete
    assert result.readiness.status == "stale"
    assert result.value is None


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
