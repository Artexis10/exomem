"""Atomic background catalog replacement + read-path hardening.

Covers OpenSpec ``restore-indexed-category-recall`` task 2.4 and the atomicity
parts of 1.4 — the exact contracts the atomic rebuild must uphold:

* a Markdown edit that lands after the background build captured its start
  checkpoint is replayed before publication, and the published catalog stores
  the exact target checkpoint (never blessed for a snapshot it lacks);
* an incomplete/overflowed delta or a mid-build projection-identity change
  discards the temp build and leaves the live sidecar untouched;
* a failed atomic publish preserves the live sidecar;
* a fatally-retired store can still publish and recover;
* a transient lock fails only the current call and schedules no whole-corpus
  repair;
* the bounded foreground delta uses an ordinary connection and never negotiates
  journal mode;
* an up-to-date catalog-readiness check performs no DDL;
* a legacy (v4) sidecar without ``pages.emitted_parent_path`` is safely replaced
  with the current emitted-parent schema, never faulting a new-column
  index/INSERT against the old shape;
* the freshness reconcile drift transition never exposes the fresh map paired
  with the pre-drift generation.

Publication is serialized against foreground deltas and single-file-safe:

* a foreground delta that advances the live catalog after this build captured
  its temp target aborts the replace under the publication lock, leaving the
  newer live rows and checkpoint intact rather than regressing them;
* a temp WAL that cannot be folded to a single self-contained file (the
  checkpoint/``journal_mode=DELETE`` proof fails, or a temp ``-wal`` survives)
  discards the temp and never publishes, preserving live;
* an unsafe live WAL state (the live ``-wal`` cannot be safely folded) declines
  publication WITHOUT replacing the live main file or unlinking live
  ``-wal``/``-shm``;
* a successful atomic replacement still works and leaves a self-contained live
  main file (no ``-wal``/``-shm`` beside it, a valid standalone SQLite DB).

Building the normal-table catalog needs no FTS5, so these run everywhere.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from exomem import find as find_module
from exomem import freshness, lexstore


@pytest.fixture(autouse=True)
def _fresh_state() -> Any:
    freshness.clear()
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()
    yield
    freshness.clear()
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _page_text(stem: str, body: str) -> str:
    page_id = uuid.uuid5(uuid.NAMESPACE_URL, f"atomic:{stem}")
    return (
        f"---\ntype: insight\ntitle: {stem}\nexomem_id: {page_id}\n"
        f"updated: 2026-01-01\n---\n# {stem}\n\n{body}\n"
    )


def _kb_file(root: Path, name: str, body: str = "plain body text") -> Path:
    path = root / "Knowledge Base" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_page_text(path.stem, body), encoding="utf-8")
    return path


def _seed(root: Path, paths: list[Path]) -> None:
    entries = [(str(p), freshness.stat_signature(p)) for p in paths]
    freshness.seed(root, "kb", entries)
    freshness.seed(root, "vault", entries)


def _touch_future(path: Path) -> None:
    future = time.time() + 10_000
    os.utime(path, (future, future))


def _assert_standalone_valid_db(path: Path) -> None:
    """The published main is a self-contained, integrity-clean SQLite catalog."""
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        conn.execute("SELECT count(*) FROM pages").fetchone()
    finally:
        conn.close()


def _units_in_category(root: Path, category: str) -> list[lexstore.SemanticUnitLexicalHit]:
    return (
        lexstore.search_semantic_units(
            root,
            "",
            k=50,
            categories=[category],
            scope="kb",
            freshness=freshness.triple(root, "kb"),
            _validate_current=False,
        )
        or []
    )


# --------------------------------------------------------------------------- #
# Replay + exact target checkpoint.
# --------------------------------------------------------------------------- #


def test_edit_after_start_is_replayed_and_target_checkpoint_is_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _kb_file(tmp_path, "a.md", "- [config] alpha ^u1")
    b = _kb_file(tmp_path, "b.md", "- [config] beforeedit ^u2")
    _seed(tmp_path, [a, b])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)
    pre_gen = freshness.consumer_checkpoint(tmp_path, "kb").generation

    real_walk = lexstore.LexicalStore._walk_entries
    state = {"injected": False}

    def hooked(self: Any) -> Any:
        snapshot = real_walk(self)  # captured BEFORE the injected edit
        if not state["injected"]:
            state["injected"] = True
            b.write_text(_page_text("b", "- [config] replayedtoken ^u2"), encoding="utf-8")
            _touch_future(b)
            freshness.on_files_changed(tmp_path, changed=[b])
        return snapshot

    monkeypatch.setattr(lexstore.LexicalStore, "_walk_entries", hooked)
    assert store.rebuild_atomic() is True
    monkeypatch.undo()

    # The edit that arrived after the start checkpoint is present, the pre-edit
    # content is gone, and the stored target checkpoint is the exact post-edit
    # snapshot (advanced past the start generation), never blessed short.
    contents = {hit.content.strip() for hit in _units_in_category(tmp_path, "config")}
    assert any("replayedtoken" in c for c in contents)
    assert not any("beforeedit" in c for c in contents)

    stored = store.catalog_checkpoint("kb")
    assert stored.triple == freshness.triple(tmp_path, "kb")
    assert stored.generation == freshness.consumer_checkpoint(tmp_path, "kb").generation
    assert stored.generation > pre_gen


# --------------------------------------------------------------------------- #
# Discard-and-preserve: incomplete delta, identity change, publish failure.
# --------------------------------------------------------------------------- #


def test_incomplete_delta_discards_temp_and_preserves_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _kb_file(tmp_path, "a.md", "- [config] livecontent ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)
    before = store.catalog_checkpoint("kb")

    real_delta = freshness.delta_since

    def incomplete(vault: Path, scope: str, checkpoint: Any) -> Any:
        real_delta(vault, scope, checkpoint)  # non-destructive read, then force incomplete
        return freshness.ConsumerDelta(
            checkpoint, checkpoint, False, frozenset(), frozenset()
        )

    monkeypatch.setattr(freshness, "delta_since", incomplete)
    assert store.rebuild_atomic() is False
    monkeypatch.undo()

    assert store.catalog_checkpoint("kb") == before
    assert not list(store.path.parent.glob("*rebuild*"))
    # The preserved live catalog still answers exactly.
    assert any(
        "livecontent" in hit.content for hit in _units_in_category(tmp_path, "config")
    )


def test_identity_change_mid_build_discards_temp_and_preserves_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _kb_file(tmp_path, "a.md", "- [config] livecontent ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)
    before = store.catalog_checkpoint("kb")

    real_identity = lexstore.catalog_semantic_identity
    calls = {"n": 0}

    def shifting(vault_root: Path) -> str:
        calls["n"] += 1
        base = real_identity(vault_root)
        # The start capture reads the true identity; a projection shift lands
        # under the scan for every subsequent read.
        return base if calls["n"] == 1 else base + "-shifted"

    monkeypatch.setattr(lexstore, "catalog_semantic_identity", shifting)
    assert store.rebuild_atomic() is False
    monkeypatch.undo()

    assert store.catalog_checkpoint("kb") == before
    assert not list(store.path.parent.glob("*rebuild*"))


def test_atomic_publish_failure_preserves_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _kb_file(tmp_path, "a.md", "- [config] livecontent ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)
    before = store.catalog_checkpoint("kb")

    def boom(src: Any, dst: Any) -> None:
        raise OSError("simulated atomic rename failure")

    monkeypatch.setattr(lexstore.os, "replace", boom)
    assert store.rebuild_atomic() is False
    monkeypatch.undo()

    assert store.path.exists()
    assert store.catalog_checkpoint("kb") == before
    assert not list(store.path.parent.glob("*rebuild*"))
    assert any(
        "livecontent" in hit.content for hit in _units_in_category(tmp_path, "config")
    )


# --------------------------------------------------------------------------- #
# Fatal recovery + transient isolation.
# --------------------------------------------------------------------------- #


def test_fatal_retired_store_can_publish_and_recover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _kb_file(tmp_path, "a.md", "- [config] recovertoken ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)

    store._failed = True
    # Inspecting fatal readiness normally launches the single-flight repair. This
    # test drives the same recovery synchronously, so suppress that independent
    # worker rather than racing two valid publishers and requiring this call to win.
    monkeypatch.setattr(lexstore, "_schedule_repair", lambda _root: None)
    assert (
        store.catalog_readiness("kb", freshness.triple(tmp_path, "kb")).status
        == "fatal_failure"
    )

    # The atomic replacement runs even for a fatally-retired store and clears it.
    assert store.rebuild_atomic() is True
    assert store._failed is False

    readiness = store.catalog_readiness("kb", freshness.triple(tmp_path, "kb"))
    assert readiness.status == "available" and readiness.complete
    assert any(
        "recovertoken" in hit.content for hit in _units_in_category(tmp_path, "config")
    )


def test_malformed_checkpoint_is_warming_and_atomic_rebuild_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _kb_file(tmp_path, "a.md", "- [config] recoverable ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)
    conn = sqlite3.connect(store.path)
    try:
        with conn:
            conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'checkpoint:kb'",
                (repr(("instance", "not-an-int", (1, 2, "digest"))),),
            )
    finally:
        conn.close()

    scheduled: list[Path] = []
    monkeypatch.setattr(lexstore, "_schedule_repair", scheduled.append)
    readiness = store.catalog_readiness("kb", freshness.triple(tmp_path, "kb"))
    assert readiness.status == "stale"
    assert readiness.complete is False
    assert scheduled == [tmp_path]

    assert store.rebuild_atomic() is True
    recovered = store.catalog_readiness("kb", freshness.triple(tmp_path, "kb"))
    assert recovered.status == "available" and recovered.complete is True


def test_foreign_instance_publication_aborts_incomparable_older_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkpoint generation from another process cannot be ordered against
    this process's temp target. Any foreign publication after the baseline wins;
    the older build must abort and leave its rows/checkpoint untouched."""
    a = _kb_file(tmp_path, "a.md", "- [config] stablecontent ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)
    triple = freshness.triple(tmp_path, "kb")
    foreign = freshness.FreshnessCheckpoint("foreign-process", 1, triple)

    # Isolate the cross-instance checkpoint proof from the DB-set token defense.
    monkeypatch.setattr(store, "_db_set_generation_token", lambda: ("stable",))
    real_fold = store._fold_to_single_file
    injected = False

    def fold_then_publish_foreign(conn: sqlite3.Connection) -> bool:
        nonlocal injected
        folded = real_fold(conn)
        if not injected:
            injected = True
            with store._publication_lock():
                live = store._connect()
                try:
                    with live:
                        store._write_checkpoint(live, "kb", foreign)
                        live.execute(
                            "INSERT INTO meta(key, value) VALUES('foreign_marker', 'newer')"
                        )
                finally:
                    live.close()
        return folded

    monkeypatch.setattr(store, "_fold_to_single_file", fold_then_publish_foreign)
    assert store.rebuild_atomic() is False

    live = store._connect()
    try:
        assert store._meta_checkpoint(live, "kb") == foreign
        assert live.execute(
            "SELECT value FROM meta WHERE key = 'foreign_marker'"
        ).fetchone() == ("newer",)
    finally:
        live.close()


def test_live_db_set_token_change_aborts_even_when_checkpoints_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A foreign writer can change the live DB set without publishing a useful
    comparable checkpoint. The private bounded DB token still makes the stale
    temp ineligible, preserving the writer's committed marker."""
    a = _kb_file(tmp_path, "a.md", "- [config] stablecontent ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)
    before = store.catalog_checkpoint("kb")
    real_fold = store._fold_to_single_file
    injected = False

    def fold_then_mutate_live_set(conn: sqlite3.Connection) -> bool:
        nonlocal injected
        folded = real_fold(conn)
        if not injected:
            injected = True
            with store._publication_lock():
                live = store._connect()
                try:
                    with live:
                        live.execute(
                            "INSERT INTO meta(key, value) VALUES('db_set_marker', 'committed')"
                        )
                finally:
                    live.close()
        return folded

    monkeypatch.setattr(store, "_fold_to_single_file", fold_then_mutate_live_set)
    assert store.rebuild_atomic() is False

    live = store._connect()
    try:
        assert store._meta_checkpoint(live, "kb") == before
        assert live.execute(
            "SELECT value FROM meta WHERE key = 'db_set_marker'"
        ).fetchone() == ("committed",)
    finally:
        live.close()


def test_no_live_source_drift_after_target_aborts_then_stable_retry_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a watcher registry, a second source snapshot must prove that the
    temp target still describes disk. Drift aborts; an unchanged retry publishes."""
    a = _kb_file(tmp_path, "a.md", "- [config] beforedrift ^u1")
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)
    freshness.clear()  # explicit no-live mode for both scopes
    real_fold = store._fold_to_single_file
    injected = False

    def fold_then_change_unobserved_source(conn: sqlite3.Connection) -> bool:
        nonlocal injected
        folded = real_fold(conn)
        if not injected:
            injected = True
            a.write_text(_page_text("a", "- [config] afterdrift ^u1"), encoding="utf-8")
            _touch_future(a)  # deliberately no freshness event
        return folded

    monkeypatch.setattr(store, "_fold_to_single_file", fold_then_change_unobserved_source)
    assert store.rebuild_atomic() is False

    # Inspect the disposable live file directly; a public query would correctly
    # notice disk drift and schedule/heal it, obscuring what publication preserved.
    live = sqlite3.connect(store.path)
    try:
        contents = {row[0] for row in live.execute("SELECT content FROM semantic_units")}
        assert any("beforedrift" in content for content in contents)
        assert not any("afterdrift" in content for content in contents)
    finally:
        live.close()

    monkeypatch.setattr(store, "_fold_to_single_file", real_fold)
    assert store.rebuild_atomic() is True
    live = sqlite3.connect(store.path)
    try:
        contents = {row[0] for row in live.execute("SELECT content FROM semantic_units")}
        assert any("afterdrift" in content for content in contents)
        assert not any("beforedrift" in content for content in contents)
    finally:
        live.close()


def test_transient_lock_schedules_no_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _kb_file(tmp_path, "a.md", "- [config] livecontent ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)

    scheduled = {"n": 0}
    monkeypatch.setattr(
        lexstore, "_schedule_repair", lambda _vr: scheduled.__setitem__("n", scheduled["n"] + 1)
    )

    def locked(_conn: Any) -> bool:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_schema_is_current", locked)

    readiness = store.catalog_readiness("kb", freshness.triple(tmp_path, "kb"))
    assert readiness.status == "transient_failure"
    assert readiness.complete is False
    assert scheduled["n"] == 0
    assert store._failed is False  # a lock never retires the store


# --------------------------------------------------------------------------- #
# Read-path hardening: no journal negotiation, no readiness DDL.
# --------------------------------------------------------------------------- #


def test_foreground_delta_never_negotiates_journal_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _kb_file(tmp_path, "a.md", "- [config] original ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)
    before = store.catalog_checkpoint("kb")

    a.write_text(_page_text("a", "- [config] patched ^u1"), encoding="utf-8")
    _touch_future(a)
    freshness.on_files_changed(tmp_path, changed=[a])
    delta = freshness.delta_since(tmp_path, "kb", before)
    assert delta.complete is True

    def forbidden(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the foreground delta path must not open a setup connection")

    monkeypatch.setattr(store, "_connect_setup", forbidden)

    statements: list[str] = []
    real_connect = store._connect

    def tracing(path: Path | None = None) -> sqlite3.Connection:
        conn = real_connect(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(store, "_connect", tracing)
    store.apply_catalog_delta("kb", delta)
    monkeypatch.undo()

    assert store.catalog_checkpoint("kb").triple == freshness.triple(tmp_path, "kb")
    assert not any("journal_mode" in s.lower() for s in statements)


def test_current_catalog_readiness_performs_no_ddl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _kb_file(tmp_path, "a.md", "- [config] livecontent ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)
    current = freshness.triple(tmp_path, "kb")

    def forbidden(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("readiness must not open a setup connection")

    monkeypatch.setattr(store, "_connect_setup", forbidden)

    statements: list[str] = []
    real_connect = store._connect

    def tracing(path: Path | None = None) -> sqlite3.Connection:
        conn = real_connect(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(store, "_connect", tracing)
    readiness = store.catalog_readiness("kb", current)
    monkeypatch.undo()

    assert readiness.status == "available" and readiness.complete
    mutating = ("create", "alter", "drop", "insert", "update", "delete", "replace")
    for statement in statements:
        head = statement.strip().lower()
        assert not any(head.startswith(keyword) for keyword in mutating), statement


# --------------------------------------------------------------------------- #
# Legacy (v4) sidecar replacement.
# --------------------------------------------------------------------------- #


def _write_v4_sidecar(side: Path) -> None:
    """A pre-emitted-parent (v4) sidecar: `pages` lacks emitted_parent_path."""
    side.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(side)
    try:
        conn.execute(
            "CREATE TABLE pages("
            " path TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL,"
            " updated TEXT NOT NULL DEFAULT '0000-00-00',"
            " in_kb INTEGER NOT NULL DEFAULT 0, in_vault INTEGER NOT NULL DEFAULT 0,"
            " is_nav INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', '4')")
        conn.execute(
            "INSERT INTO pages(path, mtime_ns, in_kb, in_vault, is_nav) "
            "VALUES('Knowledge Base/stale.md', 1, 1, 1, 0)"
        )
        conn.commit()
    finally:
        conn.close()


def test_v4_sidecar_reads_as_not_current_then_atomic_rebuild_replaces_it(
    tmp_path: Path,
) -> None:
    a = _kb_file(tmp_path, "a.md", "- [config] freshtoken ^u1")
    _seed(tmp_path, [a])
    side = lexstore.lexical_path(tmp_path)
    _write_v4_sidecar(side)
    store = lexstore.get_store(tmp_path)

    # The read-only probe recognizes the legacy shape without any DDL.
    conn = sqlite3.connect(side)
    try:
        assert store._schema_is_current(conn) is False
    finally:
        conn.close()

    assert store.rebuild_atomic() is True

    conn = sqlite3.connect(side)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(pages)")}
        assert "emitted_parent_path" in cols
        version = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        assert version == str(lexstore.SCHEMA_VERSION)
        paths = {row[0] for row in conn.execute("SELECT path FROM pages")}
        assert "Knowledge Base/a.md" in paths
        assert "Knowledge Base/stale.md" not in paths
    finally:
        conn.close()

    readiness = store.catalog_readiness("kb", freshness.triple(tmp_path, "kb"))
    assert readiness.status == "available" and readiness.complete


def test_v4_sidecar_ensure_fresh_migrates_in_place_without_new_column_fault(
    tmp_path: Path,
) -> None:
    a = _kb_file(tmp_path, "a.md", "- [config] freshtoken ^u1")
    _seed(tmp_path, [a])
    side = lexstore.lexical_path(tmp_path)
    _write_v4_sidecar(side)
    store = lexstore.get_store(tmp_path)

    # Must not raise "no such column: emitted_parent_path" on the legacy shape.
    store.ensure_fresh()
    assert store._failed is False

    conn = sqlite3.connect(side)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(pages)")}
        assert "emitted_parent_path" in cols
        version = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        assert version == str(lexstore.SCHEMA_VERSION)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Freshness reconcile drift transition is atomic.
# --------------------------------------------------------------------------- #


def test_reconcile_drift_transition_cannot_expose_new_map_with_old_generation(
    tmp_path: Path,
) -> None:
    """A reader that captures a checkpoint and immediately asks for its delta must
    never see a complete no-change delta while the derived triple has already
    moved on — that would mean the fresh, drifted map was published under the
    pre-drift generation, letting a missed event read as "no change" and bless a
    stale checkpoint. The map swap and drift generation bump are one atomic
    section, so a complete empty delta always implies a matching triple."""
    base = _kb_file(tmp_path, "base.md")
    _seed(tmp_path, [base])

    violations: list[tuple] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            checkpoint = freshness.consumer_checkpoint(tmp_path, "kb")
            delta = freshness.delta_since(tmp_path, "kb", checkpoint)
            # `delta.complete` and `delta.to.triple` are decided together under the
            # registry lock, so a complete no-change delta whose target triple has
            # already moved past the checkpoint's could only mean the fresh map was
            # published under the pre-drift generation.
            if delta.complete and not delta.changed and not delta.deleted:
                if delta.to.triple != checkpoint.triple:
                    violations.append((checkpoint, delta))

    readers = [threading.Thread(target=reader, daemon=True) for _ in range(2)]
    for thread in readers:
        thread.start()
    try:
        for index in range(200):
            _kb_file(tmp_path, f"drift-{index:04d}.md")
            entries = [
                (str(path), freshness.stat_signature(path))
                for path in find_module._walk_md(tmp_path / "Knowledge Base")
            ]
            drift = freshness.reconcile(tmp_path, "kb", entries)
            assert drift.drifted is True
    finally:
        stop.set()
        for thread in readers:
            thread.join()

    assert not violations


# --------------------------------------------------------------------------- #
# Hardening after adversarial review: no checkpoint regression, snapshot-consistent
# reads, non-live initialization, stable no-live mode, WAL-safe publish, fatal retry.
# --------------------------------------------------------------------------- #


def test_temp_fold_rejects_busy_checkpoint_even_if_mode_could_switch() -> None:
    """`wal_checkpoint(TRUNCATE)` reports busy in-band; absence of an exception is
    not proof that committed frames reached the main file."""

    class Cursor:
        def __init__(self, row: tuple[Any, ...]) -> None:
            self.row = row

        def fetchone(self) -> tuple[Any, ...]:
            return self.row

    class BusyCheckpointConnection:
        def execute(self, sql: str) -> Cursor:
            if "wal_checkpoint" in sql:
                return Cursor((1, 4, 3))  # busy, frames in WAL, frames checkpointed
            if "journal_mode=DELETE" in sql:
                return Cursor(("delete",))
            raise AssertionError(sql)

    assert (
        lexstore.LexicalStore._fold_to_single_file(BusyCheckpointConnection()) is False
    )


def test_wal_mode_main_without_sidecars_is_persistently_switched_to_delete(
    tmp_path: Path,
) -> None:
    """Closed WAL-mode databases often have no visible sidecars, but a later
    reader can recreate them. Absence alone is not publication proof: quiescence
    must persistently switch the existing main to DELETE mode first."""
    a = _kb_file(tmp_path, "a.md", "- [config] stablecontent ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)
    wal, shm = store._wal_shm_paths(store.path)

    # Establish the exact dangerous state: persisted WAL mode, no current
    # sidecars because the last connection closed/checkpointed them away.
    conn = sqlite3.connect(store.path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    finally:
        conn.close()
    assert not wal.exists() and not shm.exists()

    assert store._quiesce_live_wal() is True
    conn = sqlite3.connect(store.path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        conn.execute("SELECT count(*) FROM pages").fetchone()
    finally:
        conn.close()
    assert not wal.exists() and not shm.exists()


def test_foreground_delta_after_temp_target_aborts_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A background rebuild must never overwrite a NEWER live catalog. If a
    foreground delta advances the live sidecar past the temp build's captured
    target while the build is finishing, publication aborts under the publication
    lock and the newer live rows/checkpoint are preserved."""
    a = _kb_file(tmp_path, "a.md", "- [config] startcontent ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)

    real_fold = lexstore.LexicalStore._fold_to_single_file
    state: dict[str, Any] = {"injected": False, "newer": None}

    def fold_then_advance_live(conn: Any) -> bool:
        # Runs AFTER the temp target checkpoint was captured/written, so the temp
        # is now BEHIND the live catalog we advance here via a foreground delta.
        if not state["injected"]:
            state["injected"] = True
            a.write_text(_page_text("a", "- [config] newercontent ^u1"), encoding="utf-8")
            _touch_future(a)
            freshness.on_files_changed(tmp_path, changed=[a])
            newer = freshness.triple(tmp_path, "kb")
            state["newer"] = newer
            readiness = store.catalog_readiness("kb", newer)
            assert readiness.status == "available" and readiness.complete
        return real_fold(conn)

    monkeypatch.setattr(store, "_fold_to_single_file", fold_then_advance_live)
    published = store.rebuild_atomic()
    monkeypatch.undo()

    # The publish aborted rather than regress the newer live catalog.
    assert published is False
    assert store.catalog_checkpoint("kb").triple == state["newer"]
    contents = {hit.content.strip() for hit in _units_in_category(tmp_path, "config")}
    assert any("newercontent" in c for c in contents)
    assert not any("startcontent" in c for c in contents)


def test_readiness_and_query_stay_snapshot_consistent_across_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request that validated readiness at checkpoint N must never then query a
    replacement at a different generation. If a publication lands between the
    readiness proof and the query, the snapshot-bound serve path re-proves the
    pinned catalog and defers rather than returning a false empty from an
    `available` verdict whose file was swapped underneath it."""
    a = _kb_file(tmp_path, "a.md", "- [config] livecontent ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)
    fresh = freshness.triple(tmp_path, "kb")

    def _regress() -> None:
        # Simulate a concurrent publication that swaps in a DIFFERENT (regressed)
        # catalog: rows gone, stored checkpoint/triple moved off `fresh`.
        conn = sqlite3.connect(store.path)
        try:
            conn.execute("DELETE FROM semantic_units")
            conn.execute("DELETE FROM pages")
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('checkpoint:kb', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (repr((freshness._instance_id, 10**9, (0, 0, "regressed"))),),
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('triple:kb', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (repr((0, 0, "regressed")),),
            )
            conn.commit()
        finally:
            conn.close()

    real_readiness = store.catalog_readiness
    fired = {"done": False}

    def readiness_then_regress(scope: str, freshness_arg: Any, *, allow_delta: bool = True) -> Any:
        verdict = real_readiness(scope, freshness_arg, allow_delta=allow_delta)
        if verdict.complete and not fired["done"]:
            fired["done"] = True
            _regress()  # a publication lands right after the readiness proof
        return verdict

    monkeypatch.setattr(store, "catalog_readiness", readiness_then_regress)
    result = lexstore.search_semantic_units(
        tmp_path,
        "",
        k=50,
        categories=["config"],
        scope="kb",
        freshness=fresh,
        _validate_current=False,
    )
    monkeypatch.undo()

    # Snapshot-consistent: serve the validated rows OR defer — never a false empty.
    assert result != []
    assert result is None or any("livecontent" in hit.content for hit in result)


def test_pre_initialization_checkpoint_then_reconcile_is_incomplete(
    tmp_path: Path,
) -> None:
    """A checkpoint captured before a scope was ever live (generation 0, triple
    None) is not a complete empty baseline. `reconcile(old is None)` mints a fresh
    generation/history atomically, so the pre-initialization checkpoint yields an
    incomplete delta rather than a bogus complete no-change delta."""
    _kb_file(tmp_path, "a.md", "- [config] c ^u1")
    assert freshness.is_live(tmp_path, "kb") is False
    pre = freshness.consumer_checkpoint(tmp_path, "kb")
    assert pre.generation == 0 and pre.triple is None

    entries = [
        (str(p), freshness.stat_signature(p))
        for p in find_module._walk_md(tmp_path / "Knowledge Base")
    ]
    drift = freshness.reconcile(tmp_path, "kb", entries)  # old is None → first init
    assert drift.drifted is False

    delta = freshness.delta_since(tmp_path, "kb", pre)
    assert delta.complete is False
    assert not delta.changed and not delta.deleted

    # Initialization minted a real generation + derivable triple.
    post = freshness.consumer_checkpoint(tmp_path, "kb")
    assert post.generation > 0 and post.triple is not None


def test_stable_no_live_mode_converges_without_rebuild_storm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A watcher-disabled / unseeded CLI rebuild must not publish a triple-None
    checkpoint and claim success while readiness can never become available. It
    binds the exact WALK checkpoint to the built rows, so background repair
    converges: the next request is `available`, never permanent warming, and
    nothing schedules a repair storm."""
    monkeypatch.setenv("EXOMEM_DISABLE_EVENT_INDEXES", "1")
    _kb_file(tmp_path, "a.md", "- [config] stablecontent ^u1")
    # Deliberately unseeded and non-live.
    store = lexstore.get_store(tmp_path)
    assert freshness.is_live(tmp_path, "kb") is False

    scheduled = {"n": 0}
    monkeypatch.setattr(
        lexstore, "_schedule_repair", lambda _vr: scheduled.__setitem__("n", scheduled["n"] + 1)
    )

    assert store.rebuild_atomic() is True

    walk_triple = store._scope_triple("kb")
    stored = store.catalog_checkpoint("kb")
    # A legitimate walk checkpoint (NOT triple=None) was bound to the built rows.
    assert stored is not None and stored.triple == walk_triple

    r1 = store.catalog_readiness("kb", walk_triple)
    assert r1.status == "available" and r1.complete
    r2 = store.catalog_readiness("kb", walk_triple)
    assert r2.status == "available" and r2.complete
    assert scheduled["n"] == 0  # converged: no warming loop / rebuild storm

    hits = (
        lexstore.search_semantic_units(
            tmp_path,
            "",
            k=50,
            categories=["config"],
            scope="kb",
            freshness=walk_triple,
            _validate_current=False,
        )
        or []
    )
    assert any("stablecontent" in hit.content for hit in hits)


def test_temp_wal_fold_failure_preserves_live_and_does_not_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Publication is forbidden unless the temp DB is provably self-contained in
    its main file. If the WAL fold fails, the temp is discarded and the live
    sidecar is preserved — never a main-file replace that strands committed data
    in an un-folded temp -wal."""
    a = _kb_file(tmp_path, "a.md", "- [config] livecontent ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)
    before = store.catalog_checkpoint("kb")

    monkeypatch.setattr(store, "_fold_to_single_file", lambda conn: False)
    assert store.rebuild_atomic() is False
    monkeypatch.undo()

    assert store.path.exists()
    assert store.catalog_checkpoint("kb") == before
    assert not list(store.path.parent.glob("*rebuild*"))  # temp discarded
    assert any("livecontent" in hit.content for hit in _units_in_category(tmp_path, "config"))


def test_live_wal_not_blindly_deleted_and_unsafe_publish_declines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live sidecar's WAL must be safely folded before a single-file replace,
    never blindly deleted after it. When the live WAL cannot be folded safely the
    publish declines and leaves live untouched; a real publish folds it away and
    leaves no orphan -wal/-shm beside a valid self-contained catalog."""
    a = _kb_file(tmp_path, "a.md", "- [config] livecontent ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)
    before = store.catalog_checkpoint("kb")

    # (a) Unsafe: the live WAL cannot be safely folded → decline WITHOUT replacing
    # the live main file (and thus without ever reaching a post-replace unlink of
    # the live -wal/-shm). An os.replace spy proves no main-file swap happened.
    replaced = {"n": 0}
    real_replace = lexstore.os.replace

    def _spy_replace(src: Any, dst: Any) -> Any:
        replaced["n"] += 1
        return real_replace(src, dst)

    monkeypatch.setattr(lexstore.os, "replace", _spy_replace)
    monkeypatch.setattr(store, "_quiesce_live_wal", lambda: False)
    assert store.rebuild_atomic() is False
    assert replaced["n"] == 0  # live main never replaced; live -wal/-shm never touched
    monkeypatch.undo()
    assert store.catalog_checkpoint("kb") == before
    assert any("livecontent" in hit.content for hit in _units_in_category(tmp_path, "config"))

    # (b) A real publish folds live WAL state into one self-contained main file.
    b = _kb_file(tmp_path, "b.md", "- [config] secondcontent ^u2")
    freshness.on_files_changed(tmp_path, changed=[b])
    assert store.rebuild_atomic() is True
    wal, shm = store._wal_shm_paths(store.path)
    assert not wal.exists() and not shm.exists()
    conn = sqlite3.connect(store.path)
    try:
        count = conn.execute("SELECT count(*) FROM semantic_units").fetchone()[0]
    finally:
        conn.close()
    assert count >= 2
    contents = {hit.content.strip() for hit in _units_in_category(tmp_path, "config")}
    assert any("secondcontent" in c for c in contents)


def test_failed_fatal_recovery_schedules_again_on_later_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fatal recovery is not one-shot. If the first repair attempt fails, the
    store stays `_failed`; every later readiness call must schedule the
    single-flight repair AGAIN, so recovery keeps being retried instead of
    stranding the store as fatal for the process."""
    a = _kb_file(tmp_path, "a.md", "- [config] recovertoken ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)
    store._failed = True

    scheduled = {"n": 0}
    monkeypatch.setattr(
        lexstore, "_schedule_repair", lambda _vr: scheduled.__setitem__("n", scheduled["n"] + 1)
    )

    fresh = freshness.triple(tmp_path, "kb")
    r1 = store.catalog_readiness("kb", fresh)
    assert r1.status == "fatal_failure" and r1.complete is False
    assert scheduled["n"] == 1  # first readiness scheduled a repair

    # The (single-flight) repair "failed" — store is still `_failed`. A LATER
    # readiness must schedule again rather than give up.
    r2 = store.catalog_readiness("kb", fresh)
    assert r2.status == "fatal_failure" and r2.complete is False
    assert scheduled["n"] == 2


# --------------------------------------------------------------------------- #
# Every live catalog writer shares the publication barrier.
#
# The final rebuild publish and the foreground delta already serialize under
# `lexical-catalog-publication`. These prove the ORDINARY live writers
# (`upsert_paths`, the search-path reconcile/heal, `ensure_fresh`) take the same
# barrier, so none can commit into the window a publish's live-WAL quiesce +
# `os.replace` is racing; and that the request-path foreground delta declines
# fast on contention (short bound) instead of stalling behind a background
# publish, while the barrier-delegating normal paths never nested-lock/deadlock.
# --------------------------------------------------------------------------- #


def test_upsert_declines_fast_on_publication_contention_then_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inline writer/request repair never waits behind a background publication.
    It declines within the foreground bound, schedules repair, and a later retry
    applies after the barrier is free."""
    from exomem.vault import vault_creation_lock

    a = _kb_file(tmp_path, "a.md", "- [config] original ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)

    b = _kb_file(tmp_path, "b.md", "- [config] upsertadded ^u2")
    freshness.on_files_changed(tmp_path, changed=[b])
    scheduled = {"n": 0}
    monkeypatch.setattr(
        lexstore, "_schedule_repair", lambda _root: scheduled.__setitem__("n", scheduled["n"] + 1)
    )

    started = threading.Event()
    finished = threading.Event()
    outcome: dict[str, Any] = {}

    def do_upsert() -> None:
        started.set()
        before = time.perf_counter()
        outcome["applied"] = store.upsert_paths([b])
        outcome["elapsed"] = time.perf_counter() - before
        finished.set()

    worker = threading.Thread(target=do_upsert, daemon=True)
    with vault_creation_lock(tmp_path, "lexical-catalog-publication", timeout=5):
        worker.start()
        assert started.wait(5)
        assert finished.wait(0.5)
        assert outcome["applied"] is False
        assert outcome["elapsed"] < 0.5
        assert scheduled["n"] == 1

    worker.join(5)
    assert store.upsert_paths([b]) is True
    contents = {hit.content.strip() for hit in _units_in_category(tmp_path, "config")}
    assert any("upsertadded" in c for c in contents)


def test_inline_upsert_on_old_schema_never_walks_and_schedules_atomic_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _kb_file(tmp_path, "a.md", "- [config] original ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)
    conn = sqlite3.connect(store.path)
    try:
        with conn:
            conn.execute("UPDATE meta SET value = '4' WHERE key = 'schema_version'")
    finally:
        conn.close()

    b = _kb_file(tmp_path, "b.md", "- [config] shoulddefer ^u2")
    scheduled = {"n": 0}
    monkeypatch.setattr(
        store,
        "_walk_entries",
        lambda: (_ for _ in ()).throw(AssertionError("inline upsert walked corpus")),
    )
    monkeypatch.setattr(
        lexstore, "_schedule_repair", lambda _root: scheduled.__setitem__("n", scheduled["n"] + 1)
    )

    assert store.upsert_paths([b]) is False
    assert scheduled["n"] == 1
    conn = sqlite3.connect(store.path)
    try:
        assert conn.execute(
            "SELECT count(*) FROM semantic_units WHERE content LIKE '%shoulddefer%'"
        ).fetchone() == (0,)
    finally:
        conn.close()


def test_exact_stale_parent_under_publication_contention_returns_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.vault import vault_creation_lock

    a = _kb_file(tmp_path, "a.md", "- [config] stale_index_row ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    old_triple = freshness.triple(tmp_path, "kb")

    # Change the parent without notifying the registry. Exact catalog readiness
    # still reflects the old snapshot, but parent validation detects the stale row.
    a.write_text(_page_text("a", "- [rule] changed_on_disk ^u1"), encoding="utf-8")
    _touch_future(a)
    monkeypatch.setattr(lexstore, "_schedule_repair", lambda _root: None)
    result: dict[str, Any] = {}
    finished = threading.Event()

    def query() -> None:
        before = time.perf_counter()
        result["hits"] = lexstore.search_semantic_units(
            tmp_path,
            "",
            10,
            categories=["config"],
            scope="kb",
            freshness=old_triple,
            _repair_stale=True,
        )
        result["elapsed"] = time.perf_counter() - before
        finished.set()

    worker = threading.Thread(target=query, daemon=True)
    with vault_creation_lock(tmp_path, "lexical-catalog-publication", timeout=5):
        worker.start()
        assert finished.wait(0.5)
        assert result["hits"] is None
        assert result["elapsed"] < 0.5
    worker.join(5)


def test_upsert_source_disappearing_during_insert_rolls_back_and_defers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _kb_file(tmp_path, "a.md", "- [config] original ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)

    a.write_text(_page_text("a", "- [rule] candidate ^u1"), encoding="utf-8")
    _touch_future(a)
    real_insert = store._insert_page
    removed = False

    def insert_then_remove(*args: Any, **kwargs: Any) -> Any:
        nonlocal removed
        value = real_insert(*args, **kwargs)
        if not removed:
            removed = True
            a.unlink()
        return value

    scheduled: list[Path] = []
    monkeypatch.setattr(store, "_insert_page", insert_then_remove)
    monkeypatch.setattr(lexstore, "_schedule_repair", scheduled.append)
    assert store.upsert_paths([a]) is False
    assert scheduled == [tmp_path]

    conn = sqlite3.connect(store.path)
    try:
        contents = {row[0] for row in conn.execute("SELECT content FROM semantic_units")}
        assert any("original" in content for content in contents)
        assert not any("candidate" in content for content in contents)
    finally:
        conn.close()


def test_foreground_delta_rolls_back_when_semantic_identity_changes_mid_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _kb_file(tmp_path, "a.md", "- [config] original ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)
    before = store.catalog_checkpoint("kb")
    conn = sqlite3.connect(store.path)
    try:
        before_identity = conn.execute(
            "SELECT value FROM meta WHERE key = 'catalog_identity'"
        ).fetchone()[0]
    finally:
        conn.close()

    a.write_text(_page_text("a", "- [rule] candidate ^u1"), encoding="utf-8")
    _touch_future(a)
    freshness.on_files_changed(tmp_path, changed=[a])
    delta = freshness.delta_since(tmp_path, "kb", before)
    real_apply_rows = store._apply_delta_rows
    state = {"flipped": False}

    def apply_then_flip(conn: sqlite3.Connection, captured: Any) -> None:
        real_apply_rows(conn, captured)
        state["flipped"] = True

    monkeypatch.setattr(store, "_apply_delta_rows", apply_then_flip)
    monkeypatch.setattr(
        lexstore,
        "catalog_semantic_identity",
        lambda _root: f"{before_identity}-changed" if state["flipped"] else before_identity,
    )

    with pytest.raises(ValueError, match="identity"):
        store.apply_catalog_delta("kb", delta)
    assert store.catalog_checkpoint("kb") == before
    conn = sqlite3.connect(store.path)
    try:
        assert conn.execute(
            "SELECT value FROM meta WHERE key = 'catalog_identity'"
        ).fetchone() == (before_identity,)
        contents = {row[0] for row in conn.execute("SELECT content FROM semantic_units")}
        assert any("original" in content for content in contents)
        assert not any("candidate" in content for content in contents)
    finally:
        conn.close()


def test_foreground_delta_rejects_later_registered_generation(
    tmp_path: Path,
) -> None:
    a = _kb_file(tmp_path, "a.md", "- [config] original ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)
    before = store.catalog_checkpoint("kb")

    a.write_text(_page_text("a", "- [rule] target_b ^u1"), encoding="utf-8")
    _touch_future(a)
    freshness.on_files_changed(tmp_path, changed=[a])
    delta_b = freshness.delta_since(tmp_path, "kb", before)

    a.write_text(_page_text("a", "- [design] later_c ^u1"), encoding="utf-8")
    _touch_future(a)
    freshness.on_files_changed(tmp_path, changed=[a])
    assert freshness.consumer_checkpoint(tmp_path, "kb") != delta_b.to

    with pytest.raises(ValueError, match="target"):
        store.apply_catalog_delta("kb", delta_b)
    assert store.catalog_checkpoint("kb") == before
    conn = sqlite3.connect(store.path)
    try:
        contents = {row[0] for row in conn.execute("SELECT content FROM semantic_units")}
        assert any("original" in content for content in contents)
        assert not any("target_b" in content or "later_c" in content for content in contents)
    finally:
        conn.close()


@pytest.mark.parametrize("unobserved_change", ["edit", "delete"])
def test_foreground_delta_rejects_unobserved_source_drift_after_target(
    tmp_path: Path, unobserved_change: str
) -> None:
    a = _kb_file(tmp_path, "a.md", "- [config] original ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)
    before = store.catalog_checkpoint("kb")

    a.write_text(_page_text("a", "- [rule] target_b ^u1"), encoding="utf-8")
    _touch_future(a)
    freshness.on_files_changed(tmp_path, changed=[a])
    delta_b = freshness.delta_since(tmp_path, "kb", before)
    assert freshness.consumer_checkpoint(tmp_path, "kb") == delta_b.to

    if unobserved_change == "edit":
        a.write_text(_page_text("a", "- [design] unobserved_c ^u1"), encoding="utf-8")
        _touch_future(a)
    else:
        a.unlink()

    with pytest.raises(ValueError, match="source"):
        store.apply_catalog_delta("kb", delta_b)
    assert store.catalog_checkpoint("kb") == before
    conn = sqlite3.connect(store.path)
    try:
        contents = {row[0] for row in conn.execute("SELECT content FROM semantic_units")}
        assert any("original" in content for content in contents)
        assert not any(
            "target_b" in content or "unobserved_c" in content for content in contents
        )
    finally:
        conn.close()


def test_repair_heal_reconcile_serializes_behind_publication_barrier(
    tmp_path: Path,
) -> None:
    """A drift heal via `_ensure_synced` (rebuild/heal/bless) mutates the live
    sidecar, so it must serialize behind the publication barrier too. With the
    barrier held from another thread the heal blocks until release, then applies —
    it can never interleave with a publish's replace."""
    from exomem.vault import vault_creation_lock

    a = _kb_file(tmp_path, "a.md", "- [config] original ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)

    # Drift: a new registered file makes the scope triple diverge from stored rows,
    # so `_ensure_synced` must heal (a live mutation).
    b = _kb_file(tmp_path, "b.md", "- [config] healedtoken ^u2")
    freshness.on_files_changed(tmp_path, changed=[b])
    drifted = freshness.triple(tmp_path, "kb")
    store._synced.clear()

    healed = threading.Event()

    def do_heal() -> None:
        conn = store._connect()
        try:
            store._ensure_synced(conn, "kb", drifted, repair=True)
        finally:
            conn.close()
        healed.set()

    worker = threading.Thread(target=do_heal, daemon=True)
    with vault_creation_lock(tmp_path, "lexical-catalog-publication", timeout=5):
        worker.start()
        # The heal must block on the barrier we hold. (Pre-fix, the reconcile
        # rebuilt/healed the live sidecar with no barrier, racing the replace.)
        assert not healed.wait(1.0)

    assert healed.wait(5)
    worker.join(5)
    contents = {hit.content.strip() for hit in _units_in_category(tmp_path, "config")}
    assert any("healedtoken" in c for c in contents)


def test_foreground_delta_declines_fast_on_contention_and_schedules_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request-path foreground delta must use the SHORT publication-barrier
    bound: on contention it declines almost immediately and lets readiness
    schedule a single-flight repair — it must NOT block on the 30s background
    wait, false-empty, or scan."""
    from exomem.vault import vault_creation_lock

    a = _kb_file(tmp_path, "a.md", "- [config] original ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)

    b = _kb_file(tmp_path, "b.md", "- [config] added ^u2")
    freshness.on_files_changed(tmp_path, changed=[b])
    drifted = freshness.triple(tmp_path, "kb")

    scheduled = {"n": 0}
    monkeypatch.setattr(
        lexstore,
        "_schedule_repair",
        lambda _vr: scheduled.__setitem__("n", scheduled["n"] + 1),
    )

    holding = threading.Event()
    release = threading.Event()

    def hold_barrier() -> None:
        with vault_creation_lock(tmp_path, "lexical-catalog-publication", timeout=5):
            holding.set()
            release.wait(10)

    worker = threading.Thread(target=hold_barrier, daemon=True)
    worker.start()
    assert holding.wait(5)

    start = time.monotonic()
    readiness = store.catalog_readiness("kb", drifted)
    elapsed = time.monotonic() - start
    release.set()
    worker.join(5)

    # Declined within the short foreground bound (not the 30s background wait),
    # deferring to a scheduled single-flight repair rather than false-emptying.
    assert elapsed < 2.0
    assert readiness.status == "stale"
    assert readiness.complete is False
    assert scheduled["n"] >= 1


def test_repair_search_opens_connection_only_after_publication_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repair searches must not pin a pre-publication SQLite inode while waiting."""
    page = _kb_file(tmp_path, "connection-order.md", "- [config] policy ^u1")
    _seed(tmp_path, [page])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)
    current = freshness.triple(tmp_path, "kb")

    real_connect = store._connect
    connected = threading.Event()

    def observed_connect(path: Path | None = None) -> sqlite3.Connection:
        connected.set()
        return real_connect(path)

    monkeypatch.setattr(store, "_connect", observed_connect)
    calls = (
        lambda: store.search_bm25(["policy"], 10, "kb", current, repair=True),
        lambda: store.search_semantic_units(
            [], 10, (), (), "kb", current, repair=True
        ),
        lambda: store.search_substring(["policy"], "kb", current, repair=True),
    )

    for call in calls:
        connected.clear()
        result: list[Any] = []
        with store._publication_lock():
            worker = threading.Thread(
                target=lambda call=call, result=result: result.append(call()),
                daemon=True,
            )
            worker.start()
            time.sleep(0.1)
            assert not connected.is_set(), "repair opened the pre-publication DB inode"
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert connected.is_set()
        assert result and result[0] is not None


# --------------------------------------------------------------------------- #
# Orphan / proven-fatal live DB-set recovery via quarantine.
#
# The live main + `-wal` + `-shm` are one disposable set. Two states cannot be
# folded in place and would let a stale WAL attach to a freshly published main of
# the same name: a MISSING main with orphan sidecars, and a proven-fatal
# (NOTADB/CORRUPT) main+WAL that can never checkpoint itself. Both recover by
# moving the whole live-name set aside (unique quarantine names) before installing
# the temp. An ordinary busy/healthy live WAL is NOT quarantined — it still
# declines rather than evict a valid open DB.
# --------------------------------------------------------------------------- #


def test_missing_main_orphan_sidecars_recover_to_standalone_db(tmp_path: Path) -> None:
    """A crash can leave orphan `-wal`/`-shm` beside a vanished main. Publishing a
    new main at that name must NOT let the stale orphan WAL attach to a new
    generation: the orphans are quarantined first, so the published main is a
    standalone valid DB with no live-name sidecars carrying stale bytes."""
    a = _kb_file(tmp_path, "a.md", "- [config] orphanrecover ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)

    wal, shm = store._wal_shm_paths(store.path)
    store.path.unlink()
    wal.write_bytes(b"stale orphan wal from an old generation")
    shm.write_bytes(b"stale orphan shm from an old generation")
    assert not store.path.exists()

    assert store.rebuild_atomic() is True

    wal, shm = store._wal_shm_paths(store.path)
    assert store.path.exists()
    # The stale orphan sidecars never attached to the new main.
    assert not wal.exists() and not shm.exists()
    assert not list(store.path.parent.glob("*.quarantine-*"))
    _assert_standalone_valid_db(store.path)
    assert any(
        "orphanrecover" in hit.content for hit in _units_in_category(tmp_path, "config")
    )


def test_fatal_corrupt_main_with_wal_recovers_by_whole_set_replacement(
    tmp_path: Path,
) -> None:
    """A proven-fatal NOTADB/CORRUPT main with a `-wal` beside it can never
    checkpoint itself, so the pre-fix fold-then-replace declined forever. The
    whole disposable set is quarantined and replaced instead; recovery yields a
    standalone valid DB and clears the sticky `_failed` retirement flag."""
    a = _kb_file(tmp_path, "a.md", "- [config] corruptrecover ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)

    wal, shm = store._wal_shm_paths(store.path)
    store.path.write_bytes(b"this is definitely not a sqlite database")
    wal.write_bytes(b"stale wal that must never attach to the new main")
    store._failed = True  # a prior query proved it fatal and retired the store

    assert store.rebuild_atomic() is True
    assert store._failed is False

    wal, shm = store._wal_shm_paths(store.path)
    assert not wal.exists() and not shm.exists()
    assert not list(store.path.parent.glob("*.quarantine-*"))
    _assert_standalone_valid_db(store.path)
    readiness = store.catalog_readiness("kb", freshness.triple(tmp_path, "kb"))
    assert readiness.status == "available" and readiness.complete
    assert any(
        "corruptrecover" in hit.content for hit in _units_in_category(tmp_path, "config")
    )


def test_quarantine_install_failure_restores_or_isolates_without_mixing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If installing the temp over the quarantined set fails, the prior set must
    never end up as a mixed generation: either it is fully restored to its live
    names, or it is fully isolated under quarantine names with the live names left
    empty (fail closed — never a fresh main beside the stale quarantined WAL)."""
    a = _kb_file(tmp_path, "a.md", "- [config] recover ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)

    # A proven-fatal corrupt main + WAL: a set that routes to quarantine.
    wal, shm = store._wal_shm_paths(store.path)
    store.path.write_bytes(b"not a database at all")
    wal.write_bytes(b"old wal bytes")
    corrupt_main = store.path.read_bytes()
    old_wal = wal.read_bytes()

    quarantined = {"n": 0}
    real_quarantine = store._quarantine_live_set
    monkeypatch.setattr(
        store,
        "_quarantine_live_set",
        lambda: (quarantined.__setitem__("n", quarantined["n"] + 1), real_quarantine())[1],
    )

    installs = {"n": 0}
    real_replace = lexstore.os.replace

    def fail_live_install(src: Any, dst: Any) -> Any:
        # Fail ONLY the temp -> live-main install; quarantine/restore moves (which
        # target quarantine names, not the live main) still succeed.
        if Path(dst) == store.path:
            installs["n"] += 1
            raise OSError("simulated temp install failure")
        return real_replace(src, dst)

    monkeypatch.setattr(lexstore.os, "replace", fail_live_install)
    assert store.rebuild_atomic() is False
    monkeypatch.undo()

    # The recovery path actually ran: the whole set was quarantined and the temp
    # install over the live main was attempted (and forced to fail).
    assert quarantined["n"] == 1
    assert installs["n"] >= 1

    wal, shm = store._wal_shm_paths(store.path)
    q_files = list(store.path.parent.glob("*.quarantine-*"))
    if store.path.exists():
        # Fully restored to the live names; nothing left isolated.
        assert store.path.read_bytes() == corrupt_main
        assert wal.exists() and wal.read_bytes() == old_wal
        assert not q_files
    else:
        # Fail-closed isolation: live names empty, the old disposable set retained
        # under quarantine names, never attached to a new main.
        assert not wal.exists() and not shm.exists()
        assert q_files
    assert not list(store.path.parent.glob("*rebuild*"))  # temp discarded


def test_healthy_busy_wal_declines_without_quarantine_or_eviction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ordinary busy/healthy live WAL must NOT take the quarantine/eviction
    path. When the live WAL cannot be folded this attempt, publication declines
    and leaves the valid live main + `-wal` untouched — never renamed to
    quarantine, never blindly deleted."""
    a = _kb_file(tmp_path, "a.md", "- [config] livecontent ^u1")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)
    before = store.catalog_checkpoint("kb")

    # A genuine, healthy live WAL held open by a reader (the "busy" condition).
    holder = sqlite3.connect(store.path)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute(
        "INSERT INTO meta(key, value) VALUES('probe', 'x') "
        "ON CONFLICT(key) DO UPDATE SET value = 'x'"
    )
    holder.commit()
    wal, _shm = store._wal_shm_paths(store.path)
    try:
        assert wal.exists()  # a real live WAL is present and valid

        quarantined = {"n": 0}
        real_quarantine = store._quarantine_live_set
        monkeypatch.setattr(
            store,
            "_quarantine_live_set",
            lambda: (quarantined.__setitem__("n", quarantined["n"] + 1), real_quarantine())[1],
        )
        # Simulate a busy WAL that will not fold on this attempt.
        monkeypatch.setattr(store, "_quiesce_live_wal", lambda: False)

        replaced = {"n": 0}
        real_replace = lexstore.os.replace
        monkeypatch.setattr(
            lexstore.os,
            "replace",
            lambda s, d: (replaced.__setitem__("n", replaced["n"] + 1), real_replace(s, d))[1],
        )

        assert store.rebuild_atomic() is False
        monkeypatch.undo()

        # The healthy busy WAL declined without ever quarantining/evicting.
        assert quarantined["n"] == 0
        assert replaced["n"] == 0  # live main never replaced
        assert not list(store.path.parent.glob("*.quarantine-*"))
        assert store.path.exists() and wal.exists()  # valid live state untouched
        assert store.catalog_checkpoint("kb") == before
    finally:
        holder.close()

    assert any(
        "livecontent" in hit.content for hit in _units_in_category(tmp_path, "config")
    )


def test_normal_reconcile_paths_do_not_nested_lock_or_deadlock(
    tmp_path: Path,
) -> None:
    """`ensure_fresh` acquires the (non-reentrant) barrier once and hands ownership
    to `_ensure_synced` via `barrier_held=True`. A naive double-acquire would
    nested-lock and silently abandon the reconcile, so the built catalog would be
    missing rows. This proves the normal writer + reconcile paths complete under
    the barrier without nesting."""
    a = _kb_file(tmp_path, "a.md", "- [config] one ^u1")
    _seed(tmp_path, [a])
    store = lexstore.get_store(tmp_path)

    store.ensure_fresh()  # acquires the barrier, reconciles both scopes with it held
    assert store._failed is False

    b = _kb_file(tmp_path, "b.md", "- [config] two ^u2")
    freshness.on_files_changed(tmp_path, changed=[b])
    store.upsert_paths([b])  # public locked writer
    store.ensure_fresh()  # reconcile again — must not nested-lock/deadlock
    assert store._failed is False

    contents = {hit.content.strip() for hit in _units_in_category(tmp_path, "config")}
    assert any("one" in c for c in contents)
    assert any("two" in c for c in contents)
