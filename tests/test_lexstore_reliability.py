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
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest

from exomem import find as find_module
from exomem import freshness, lexstore, request_budget, reserved_paths, writer_lease
from exomem.cli_ops import OpError

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


def test_budgeted_sqlite_contention_fails_promptly_and_restores_the_retained_reader(tmp_path: Path) -> None:
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
            return conn.execute("SELECT title FROM pages").fetchall()

        token = request_budget.set_current(budget)
        started = time.monotonic()
        try:
            with lexstore.stopping_at_budget() as stop, pytest.raises(sqlite3.OperationalError) as raised:
                lexstore.LexicalStore._query_within_budget(reader, read, readiness)
            assert raised.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
            assert not stop.hit
        finally:
            request_budget.reset_current(token)
            writer.rollback()

        assert time.monotonic() - started < 1.0
        assert reader.execute("PRAGMA busy_timeout").fetchone() == (2300,)
        assert reader.execute("SELECT title FROM pages").fetchall() == [("example",)]


def test_an_expired_catalogue_budget_declines_even_a_short_query() -> None:
    readiness = lexstore.CatalogReadiness("available", True, "fts5")
    with closing(sqlite3.connect(":memory:")) as reader:
        statements: list[str] = []
        reader.set_trace_callback(statements.append)

        def query(conn: sqlite3.Connection):
            return conn.execute("SELECT 1").fetchone()

        token = request_budget.set_current(request_budget.RequestBudget(seconds=0))
        try:
            with lexstore.stopping_at_budget() as stop:
                result = lexstore.LexicalStore._query_within_budget(reader, query, readiness)
            assert result.readiness == lexstore.CatalogReadiness(lexstore.DEADLINE_STATUS, False, "fts5")
            assert result.value is None and stop.hit
            assert statements == []
            assert lexstore.LexicalStore._query_within_budget(reader, query, readiness).value == (1,)
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


def test_nested_busy_read_returns_its_own_expired_deadline(tmp_path: Path) -> None:
    path = tmp_path / "catalogue.sqlite"
    readiness = lexstore.CatalogReadiness("available", True, "fts5")
    with closing(sqlite3.connect(path)) as writer, closing(sqlite3.connect(path)) as reader:
        writer.execute("CREATE TABLE pages (title TEXT)")
        reader.execute("PRAGMA busy_timeout=2300")
        token = request_budget.set_current(request_budget.RequestBudget(seconds=30))

        def inner(conn: sqlite3.Connection):
            writer.execute("BEGIN EXCLUSIVE")
            request_budget.current().deadline = time.monotonic()
            return conn.execute("SELECT title FROM pages").fetchall()

        def outer(conn: sqlite3.Connection):
            with request_budget.capped(5), lexstore.stopping_at_budget() as stop:
                result = lexstore.LexicalStore._query_within_budget(conn, inner, readiness)
                assert result.readiness.status == lexstore.DEADLINE_STATUS
                assert result.value is None and stop.hit
            writer.rollback()
            return conn.execute("SELECT 1").fetchone()

        try:
            with lexstore.stopping_at_budget() as stop:
                result = lexstore.LexicalStore._query_within_budget(reader, outer, readiness)
                assert not stop.hit
        finally:
            request_budget.reset_current(token)
        assert result.value == (1,)


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
@pytest.mark.parametrize("lock", ["sqlite_setup", "sqlite_schema", "identity", "identity_manager"])
def test_request_budget_bounds_catalogue_read_before_the_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock: str
) -> None:
    _write_page(tmp_path, "Knowledge Base/a.md", "contention searchable payload")
    assert lexstore.search_bm25(tmp_path, "searchable", 5)

    def read():
        return lexstore.search_bm25_result(tmp_path, "searchable", 5, allow_delta=False)

    expected = read()
    assert expected.readiness.complete and expected.value
    repair_requests = []
    monkeypatch.setattr(lexstore, "_schedule_runtime_catalog_repair", lambda *a, **k: repair_requests.append(a))

    @contextmanager
    def hold_sqlite():
        # In rollback-journal mode an exclusive writer prevents the first read.
        with closing(sqlite3.connect(lexstore.lexical_path(tmp_path))) as writer:
            assert writer.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
            if lock == "sqlite_setup":
                writer.execute("BEGIN EXCLUSIVE")
                yield
                return
            store = lexstore.get_store(tmp_path)
            probe = store._schema_is_current

            def contended_schema(conn):
                writer.execute("BEGIN EXCLUSIVE")
                return probe(conn)

            with monkeypatch.context() as patch:
                patch.setattr(store, "_schema_is_current", contended_schema)
                yield

    @contextmanager
    def hold_identity():
        entered, release = Event(), Event()

        def hold():
            with reserved_paths._subsystem_authority_scope("lexstore"), reserved_paths._identity_coordination_scope(
                tmp_path, descriptor_ids=("lexical-store",), identity_may_change=False
            ):
                entered.set()
                assert release.wait(10)

        with ThreadPoolExecutor(max_workers=1) as worker:
            holder = worker.submit(hold)
            try:
                assert entered.wait(5)
                yield
            finally:
                release.set()
                holder.result()

    with hold_identity() if lock in {"identity", "identity_manager"} else hold_sqlite():
        if lock == "identity_manager":
            monkeypatch.setattr(writer_lease.active_manager(), "_mutation_timeout_seconds", 0.05)
            with pytest.raises(OpError) as raised:
                read()
            assert raised.value.code == "MUTATION_BUSY"
        token = request_budget.set_current(
            request_budget.RequestBudget(seconds=0.05 if lock == "identity" else 30)
        )
        started = time.monotonic()
        try:
            with lexstore.stopping_at_budget() as stop:
                result = read()
        finally:
            request_budget.reset_current(token)
        assert time.monotonic() - started < 1.0
        assert result.readiness.status == (lexstore.DEADLINE_STATUS if lock == "identity" else "transient_failure")
        assert result.value is None
        assert stop.hit == (lock == "identity")

    assert not repair_requests
    assert read() == expected


@needs_fts5
@pytest.mark.parametrize(
    ("open_to_interrupt", "expired"),
    [(1, False), (1, True), (2, False)],
    ids=["readiness", "expired", "proof"],
)
def test_budgeted_catalogue_open_never_recreates_a_disappearing_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, open_to_interrupt: int, expired: bool
) -> None:
    _write_page(tmp_path, "Knowledge Base/a.md", "searchable payload")
    assert lexstore.search_bm25(tmp_path, "searchable", 5)
    path = lexstore.lexical_path(tmp_path)
    manager = writer_lease.active_manager()
    generation = writer_lease._reserved_identity_generation_path(manager.config.state_dir, tmp_path)
    original_generation = generation.read_bytes()
    guard = reserved_paths._identity_coordination_scope
    budget = request_budget.RequestBudget(seconds=30)
    repairs = []
    read_opens = 0

    @contextmanager
    def remove_at_admission(*args, **kwargs):
        nonlocal read_opens
        with guard(*args, **kwargs) as held:
            if not kwargs.get("identity_may_change", True):
                read_opens += 1
                if read_opens == open_to_interrupt:
                    path.unlink()
                    if expired:
                        budget.deadline = time.monotonic()
            yield held

    monkeypatch.setattr(reserved_paths, "_identity_coordination_scope", remove_at_admission)
    monkeypatch.setattr(lexstore, "_schedule_runtime_catalog_repair", lambda *a, **k: repairs.append(a[0]))
    token = request_budget.set_current(budget)
    try:
        with lexstore.stopping_at_budget() as stop:
            result = lexstore.search_bm25_result(tmp_path, "searchable", 5, allow_delta=False)
    finally:
        request_budget.reset_current(token)

    assert not path.exists()
    assert generation.read_bytes() == original_generation
    assert result.readiness.status == (lexstore.DEADLINE_STATUS if expired else "stale")
    assert result.value is None and not result.readiness.complete
    assert stop.hit == expired
    assert repairs == ([] if expired else [tmp_path])


@needs_fts5
@pytest.mark.parametrize("stage", ["policy", "delta"])
def test_expired_readiness_does_not_schedule_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    _write_page(tmp_path, "Knowledge Base/a.md", "searchable payload")
    assert lexstore.search_bm25(tmp_path, "searchable", 5)
    store = lexstore.get_store(tmp_path)
    checkpoint = freshness.recall_checkpoint(tmp_path, "kb")
    budget = request_budget.RequestBudget(seconds=30)
    repairs = []
    monkeypatch.setattr(lexstore, "_schedule_runtime_catalog_repair", lambda *a, **k: repairs.append(a))

    if stage == "policy":
        def stale_policy(*args):
            budget.deadline = time.monotonic()
            return False

        monkeypatch.setattr(lexstore, "_checkpoint_policy_current", stale_policy)
    else:
        def incomplete_delta(*args):
            budget.deadline = time.monotonic()
            return SimpleNamespace(complete=False)

        count, max_mtime, digest = checkpoint.triple
        checkpoint = checkpoint._replace(triple=(count + 1, max_mtime, digest))
        monkeypatch.setattr(freshness, "live_recall_entries", lambda *a: ())
        monkeypatch.setattr(freshness, "recall_delta_since", incomplete_delta)

    token = request_budget.set_current(budget)
    try:
        with lexstore.stopping_at_budget() as stop:
            verdict = store.catalog_readiness("kb", None, recall_checkpoint=checkpoint)
        assert verdict.status == lexstore.DEADLINE_STATUS and stop.hit
        assert not repairs
    finally:
        request_budget.reset_current(token)


@needs_fts5
def test_sqlite_failure_keeps_its_verdict_without_repair_after_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_page(tmp_path, "Knowledge Base/a.md", "searchable payload")
    assert lexstore.search_bm25(tmp_path, "searchable", 5)
    store = lexstore.get_store(tmp_path)
    budget = request_budget.RequestBudget(seconds=30)
    repairs = []

    def failing_checkpoint(conn: sqlite3.Connection, scope: str):
        budget.deadline = time.monotonic()
        return conn.execute("SELECT missing_column")

    monkeypatch.setattr(store, "_meta_checkpoint", failing_checkpoint)
    monkeypatch.setattr(lexstore, "_schedule_runtime_catalog_repair", lambda *a, **k: repairs.append(a))
    token = request_budget.set_current(budget)
    try:
        with lexstore.stopping_at_budget() as stop:
            verdict = store.catalog_readiness("kb", None)
        assert verdict.status == "stale" and not stop.hit
        assert not repairs
    finally:
        request_budget.reset_current(token)


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
