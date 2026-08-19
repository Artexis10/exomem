from __future__ import annotations

import importlib
import inspect
import os
import sqlite3
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from types import SimpleNamespace

import benchmark_capabilities
import numpy as np
import pytest

from exomem import audit as audit_module
from exomem import file_watcher, mode
from exomem import reconcile as reconcile_module


@contextmanager
def _committed(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Commit a connection and actually close it afterwards.

    `sqlite3.Connection.__exit__` ends the transaction; it does not close the
    connection, and CPython only closes it when the object is collected. On
    POSIX that is invisible, because an open descriptor does not stop a
    rename. On Windows the still-open handle makes the very next
    `Path.replace` fail with `[WinError 32]` -- which is exactly how these
    tests stage the swap they are about to census.
    """
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _sqlite(path: Path) -> AbstractContextManager[sqlite3.Connection]:
    return _committed(sqlite3.connect(path))


def _raw_record(vault: Path) -> tuple[Path, str]:
    path = vault / "Knowledge Base" / "Records" / "Health" / "items" / "raw.md"
    path.parent.mkdir(parents=True)
    path.write_text("private measurement", encoding="utf-8")
    return path, path.relative_to(vault).as_posix()


def _seed_suppressed_sidecars(vault: Path, rel: str) -> None:
    from exomem import (
        claims,
        clip_index,
        deferred_index,
        embedding_index,
        epistemic_graph,
        lexstore,
    )

    lexical = lexstore.get_store(vault)
    with _committed(lexical._connect()) as conn:
        lexical._ensure_schema(conn)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(lexstore.SCHEMA_VERSION),),
        )
        conn.execute(
            "INSERT INTO pages(path, mtime_ns, updated, in_kb, in_vault, is_nav) "
            "VALUES (?, 1, '2026-01-01', 1, 1, 0)",
            (rel,),
        )
        conn.execute(
            "INSERT INTO semantic_units(record_type, unit_ref, parent_path, parent_generation, "
            "parent_source_hash, parser_version, form, category_raw, category_key, category, "
            "kind, content, tags_json, unit_source_hash, line, end_line, source_order) "
            "VALUES ('semantic_unit', 'raw-unit', ?, 'generation', 'hash', 1, 'fact', "
            "'config', 'config', 'config', 'fact', 'private', '[]', 'unit-hash', 1, 1, 0)",
            (rel,),
        )

    embedding = embedding_index.EmbeddingIndex(vault)
    with _committed(embedding._connect()) as conn:
        conn.execute(
            "INSERT INTO chunks(file_path, chunk_idx, chunk_text, vector, file_mtime) "
            "VALUES (?, 0, 'private', X'00', 0)",
            (rel,),
        )
        conn.execute(
            "INSERT INTO semantic_unit_vectors(unit_key, record_type, unit_ref, parent_path, "
            "parent_generation, parent_source_hash, parser_version, form, category, kind, content, "
            "unit_source_hash, source_order, vector, file_mtime) "
            "VALUES ('raw-unit', 'semantic_unit', 'raw-unit', ?, 'generation', 'hash', 1, "
            "'fact', 'config', 'fact', 'private', 'unit-hash', 0, X'00', 0)",
            (rel,),
        )

    graph = epistemic_graph.EpistemicGraphIndex(vault)
    with _committed(graph._connect()) as conn:
        conn.execute(
            "INSERT INTO graph_nodes(node_key, kind, path, text, source_hash, metadata) "
            "VALUES ('raw', 'file', ?, 'private', 'hash', '{}')",
            (rel,),
        )

    claim = claims.ClaimIndex(vault)
    with _committed(claim._connect()) as conn:
        conn.execute(
            "INSERT INTO claims(file_path, claim_text, checksum, vector, file_mtime) "
            "VALUES (?, 'private', 'checksum', X'00', 0)",
            (rel,),
        )

    clip = clip_index.ClipIndex(vault)
    with _committed(clip._connect()) as conn:
        conn.execute(
            "INSERT INTO images(file_path, frame_ts, vector, file_mtime) "
            "VALUES (?, NULL, X'00', 0)",
            (rel.removesuffix(".md"),),
        )

    with deferred_index._connect(vault, create=True) as conn:
        conn.execute(
            "INSERT INTO semantic_upserts(rel_path, created_at, updated_at, revision) "
            "VALUES (?, 0, 0, 1)",
            (rel,),
        )


def test_audit_and_reconcile_purge_live_suppressed_records_semantically_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _raw, rel = _raw_record(tmp_path)
    _seed_suppressed_sidecars(tmp_path, rel)
    from exomem import deferred_index

    with deferred_index._connect(tmp_path, create=True) as conn:
        conn.execute(
            "INSERT INTO semantic_upserts(rel_path, created_at, updated_at, revision) "
            "VALUES ('../../outside.md', 0, 0, 1)"
        )

    dry = reconcile_module.reconcile(tmp_path, dry_run=True).as_dict()
    assert {item["component"] for item in dry["semantic_suppressed_drift"]} == {
        "claims",
        "clip",
        "deferred_semantic",
        "graph",
        "lexical",
        "lexical_units",
        "vector",
        "vector_units",
    }
    assert audit_module.audit(tmp_path, categories=["semantic_recall_isolation"]).summary == {
        "semantic_recall_isolation": 9
    }

    deleted: list[list[str]] = []
    from exomem import index_sync

    real_delete = index_sync.delete_after_remove
    monkeypatch.setattr(
        index_sync,
        "delete_after_remove",
        lambda _root, rels: deleted.append(list(rels)) or real_delete(_root, rels),
    )
    monkeypatch.setattr("exomem.governance.receipts.reconcile", lambda *_args, **_kwargs: {})
    applied = reconcile_module.reconcile(tmp_path).as_dict()

    assert applied["semantic_suppressed_purged"] == [rel]
    assert deleted == []
    assert reconcile_module.reconcile(tmp_path).semantic_suppressed_drift == []


def test_watcher_raw_record_burst_does_not_consume_semantic_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = []
    for index in range(3):
        path = (
            tmp_path
            / "Knowledge Base"
            / "Records"
            / "Health"
            / "items"
            / f"raw-{index}.md"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private", encoding="utf-8")
        paths.append(path)
    calls: list[tuple[list[Path], dict]] = []
    monkeypatch.setattr(
        file_watcher.index_sync,
        "upsert_after_write",
        lambda _root, batch, **kwargs: calls.append((list(batch), kwargs)),
    )
    watcher = file_watcher.FileWatcher(tmp_path)
    monkeypatch.setattr(
        watcher,
        "_watcher_policy",
        lambda: mode.WatcherPolicy(0.5, 300.0, 1, 1, False),
    )

    watcher._dispatch_batch(paths, [path.relative_to(tmp_path).as_posix() for path in paths], [], cap=False)

    assert calls == [
        (
            paths,
            {
                "defer_semantic": False,
                "publish_corpus_change": False,
            },
        )
    ]


def test_access_policy_transition_purges_stale_vector_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import embedding_index, recall_policy

    page = tmp_path / "Knowledge Base" / "Notes" / "Private" / "row.md"
    page.parent.mkdir(parents=True)
    page.write_text("formerly admitted", encoding="utf-8")
    rel = page.relative_to(tmp_path).as_posix()
    index = embedding_index.EmbeddingIndex(tmp_path)
    with _committed(index._connect()) as conn:
        conn.execute(
            "INSERT INTO chunks(file_path, chunk_idx, chunk_text, vector, file_mtime) "
            "VALUES (?, 0, 'formerly admitted', X'00', 0)",
            (rel,),
        )
    assert recall_policy.is_recall_candidate(tmp_path, page)

    (tmp_path / "Knowledge Base" / "_access.yaml").write_text(
        "excluded:\n  - Notes/Private\n", encoding="utf-8"
    )
    assert not recall_policy.is_recall_candidate(tmp_path, page)
    monkeypatch.setattr("exomem.governance.receipts.reconcile", lambda *_args, **_kwargs: {})

    assert reconcile_module.reconcile(tmp_path, dry_run=True).semantic_suppressed_drift == [
        {"component": "vector", "path": rel}
    ]
    reconcile_module.reconcile(tmp_path)
    assert reconcile_module.reconcile(tmp_path).semantic_suppressed_drift == []


def test_disabled_features_purge_unit_only_vector_without_creating_absent_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import claims, embedding_index, epistemic_graph, index_paths, lexstore

    _raw, rel = _raw_record(tmp_path)
    index = embedding_index.EmbeddingIndex(tmp_path)
    with _committed(index._connect()) as conn:
        conn.execute(
            "INSERT INTO semantic_unit_vectors(unit_key, record_type, unit_ref, parent_path, "
            "parent_generation, parent_source_hash, parser_version, form, category, kind, content, "
            "unit_source_hash, source_order, vector, file_mtime) "
            "VALUES ('raw-unit', 'semantic_unit', 'raw-unit', ?, 'generation', 'hash', 1, "
            "'fact', 'config', 'fact', 'private', 'unit-hash', 0, X'00', 0)",
            (rel,),
        )
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_INDEX", "1")
    monkeypatch.setattr("exomem.governance.receipts.reconcile", lambda *_args, **_kwargs: {})

    report = reconcile_module.reconcile(tmp_path)

    assert report.semantic_suppressed_purged == [rel]
    with _sqlite(index_paths.sidecar_path(tmp_path)) as conn:
        assert conn.execute("SELECT parent_path FROM semantic_unit_vectors").fetchall() == []
    assert not claims.sidecar_path(tmp_path).exists()
    assert not epistemic_graph.sidecar_path(tmp_path).exists()
    assert not lexstore.lexical_path(tmp_path).exists()


def test_census_ignores_symlinked_record_path_without_following_it(tmp_path: Path) -> None:
    from exomem import embedding_index

    raw, rel = _raw_record(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("must never be opened", encoding="utf-8")
    raw.unlink()
    try:
        raw.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")
    index = embedding_index.EmbeddingIndex(tmp_path)
    with _committed(index._connect()) as conn:
        conn.execute(
            "INSERT INTO chunks(file_path, chunk_idx, chunk_text, vector, file_mtime) "
            "VALUES (?, 0, 'stale', X'00', 0)",
            (rel,),
        )

    assert audit_module.semantic_recall_isolation_drift(tmp_path) == []
    assert outside.read_text(encoding="utf-8") == "must never be opened"


def test_census_rejects_symlinked_sidecar_without_mutating_external_database(
    tmp_path: Path,
) -> None:
    from exomem import embedding_index, index_paths

    sidecar = index_paths.sidecar_path(tmp_path)
    index = embedding_index.EmbeddingIndex(tmp_path)
    with _committed(index._connect()) as conn:
        conn.execute(
            "INSERT INTO chunks(file_path, chunk_idx, chunk_text, vector, file_mtime) "
            "VALUES ('../../external.md', 0, 'private', X'00', 0)"
        )
    with _sqlite(sidecar) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    external = tmp_path / "external.sqlite"
    sidecar.replace(external)
    try:
        sidecar.symlink_to(external)
    except OSError:
        pytest.skip("symlinks are unavailable")

    census = audit_module.semantic_recall_isolation_census(tmp_path)

    assert census.corrupt_rows == ()
    assert census.incomplete["vector"] == "sidecar_unreadable"
    assert audit_module.purge_corrupt_semantic_recall_isolation_rows(
        tmp_path,
        (audit_module._SemanticIsolationRow("vector", "../../external.md", None),),
    ) == {}
    with _sqlite(external) as conn:
        assert conn.execute("SELECT file_path FROM chunks").fetchall() == [
            ("../../external.md",)
        ]


def test_census_rejects_symlinked_kb_parent_without_mutating_external_database(
    tmp_path: Path,
) -> None:
    from exomem import claims

    external_root = tmp_path / "external-root"
    external = claims.ClaimIndex(external_root)
    with _committed(external._connect()) as conn:
        conn.execute(
            "INSERT INTO claims(file_path, claim_text, checksum, vector, file_mtime) "
            "VALUES ('../../external.md', 'private', 'checksum', X'00', 0)"
        )
    sidecar = claims.sidecar_path(external_root)
    with _sqlite(sidecar) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    kb = tmp_path / "Knowledge Base"
    try:
        kb.symlink_to(sidecar.parent, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    census = audit_module.semantic_recall_isolation_census(tmp_path)

    assert census.corrupt_rows == ()
    assert census.incomplete["claims"] == "sidecar_unreadable"
    assert audit_module.purge_corrupt_semantic_recall_isolation_rows(
        tmp_path,
        (audit_module._SemanticIsolationRow("claims", "../../external.md", None),),
    ) == {}
    with _sqlite(sidecar) as conn:
        assert conn.execute("SELECT file_path FROM claims").fetchall() == [
            ("../../external.md",)
        ]


def test_census_rejects_symlinked_sidecar_companion(tmp_path: Path) -> None:
    from exomem import claims

    index = claims.ClaimIndex(tmp_path)
    with _committed(index._connect()) as conn:
        conn.execute(
            "INSERT INTO claims(file_path, claim_text, checksum, vector, file_mtime) "
            "VALUES ('../../external.md', 'private', 'checksum', X'00', 0)"
        )
    sidecar = claims.sidecar_path(tmp_path)
    external = tmp_path / "external-journal"
    external.write_text("never open", encoding="utf-8")
    companion = sidecar.with_name(sidecar.name + "-journal")
    try:
        companion.symlink_to(external)
    except OSError:
        pytest.skip("symlinks are unavailable")

    census = audit_module.semantic_recall_isolation_census(tmp_path)

    assert census.corrupt_rows == ()
    assert census.incomplete["claims"] == "sidecar_unreadable"
    assert external.read_text(encoding="utf-8") == "never open"


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX dirfd walking")
def test_census_rejects_windows_reparse_kb_parent_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import claims

    index = claims.ClaimIndex(tmp_path)
    with _committed(index._connect()) as conn:
        conn.execute(
            "INSERT INTO claims(file_path, claim_text, checksum, vector, file_mtime) "
            "VALUES ('../../external.md', 'private', 'checksum', X'00', 0)"
        )
    kb = claims.sidecar_path(tmp_path).parent
    real_fstat = os.fstat
    # Recognise the KB directory by the inode the descriptor holds, not by
    # `/proc/self/fd/<n>`: that readlink is Linux-only, and on macOS it raised
    # out of the patched `fstat`, so the reparse seam under test was never
    # staged at all and the census reported nothing to assert on. Comparing
    # identity is portable and stricter than comparing a name.
    kb_identity = (kb.stat().st_dev, kb.stat().st_ino)

    def fstat(fd: int):
        info = real_fstat(fd)
        if (info.st_dev, info.st_ino) == kb_identity:
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_dev=info.st_dev,
                st_ino=info.st_ino,
                is_parent_reparse=True,
            )
        return info

    monkeypatch.setattr(audit_module.os, "fstat", fstat)
    monkeypatch.setattr(
        audit_module,
        "_is_reparse_point",
        lambda info: bool(getattr(info, "is_parent_reparse", False)),
    )

    census = audit_module.semantic_recall_isolation_census(tmp_path)

    assert census.corrupt_rows == ()
    assert census.incomplete["claims"] == "sidecar_unreadable"


def _refuse_reparse_at_the_binder_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make this platform's sidecar binder see a reparse point where it looks.

    The two binders ask different questions. `_bind_posix_sidecar` fstats the
    descriptor and asks `audit._is_reparse_point`. `_bind_windows_sidecar`
    never reaches that function: `mutation_lock._windows_open_path` refuses
    `FILE_ATTRIBUTE_REPARSE_POINT` on the handle before returning it. Patching
    only the POSIX seam left this test asserting a refusal on POSIX and
    asserting that the census read the sidecar normally on Windows. What is
    under test either way is the *classification* -- `sidecar_unreadable`, no
    claim of corruption -- not the detection, which
    `test_census_rejects_symlinked_sidecar_without_mutating_external_database`
    covers with a real symlink.
    """
    monkeypatch.setattr(audit_module, "_is_reparse_point", lambda _info: True)
    if os.name != "nt":
        return
    from exomem import mutation_lock

    def refuse(*_args: object, **_kwargs: object) -> int:
        raise OSError("reparse points are not allowed")

    monkeypatch.setattr(mutation_lock, "_windows_open_path", refuse)


def test_census_rejects_reparse_point_sidecar_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import embedding_index, index_paths

    index = embedding_index.EmbeddingIndex(tmp_path)
    with _committed(index._connect()) as conn:
        conn.execute(
            "INSERT INTO chunks(file_path, chunk_idx, chunk_text, vector, file_mtime) "
            "VALUES ('../../external.md', 0, 'private', X'00', 0)"
        )
    assert index_paths.sidecar_path(tmp_path).is_file()
    _refuse_reparse_at_the_binder_seam(monkeypatch)

    census = audit_module.semantic_recall_isolation_census(tmp_path)

    assert census.corrupt_rows == ()
    assert census.incomplete["vector"] == "sidecar_unreadable"


def test_census_reports_unsupported_sidecar_platform_without_claiming_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import embedding_index, index_paths

    index = embedding_index.EmbeddingIndex(tmp_path)
    with _committed(index._connect()) as conn:
        conn.execute(
            "INSERT INTO chunks(file_path, chunk_idx, chunk_text, vector, file_mtime) "
            "VALUES ('../../corrupt.md', 0, 'private', X'00', 0)"
        )
    monkeypatch.setattr(audit_module, "_sidecar_platform", lambda: "unsupported")

    census = audit_module.semantic_recall_isolation_census(tmp_path)

    assert census.corrupt_rows == ()
    assert census.incomplete["vector"] == "sidecar_unsupported"
    assert "sidecar_unreadable" not in census.incomplete.values()
    assert index_paths.sidecar_path(tmp_path).is_file()


def test_sidecar_rows_maps_actual_safe_open_failure_to_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import index_paths

    monkeypatch.setattr(audit_module, "_bind_sidecar", lambda *_args, **_kwargs: ("unreadable", None))

    assert audit_module._sidecar_rows(
        tmp_path,
        index_paths.sidecar_path(tmp_path),
        "SELECT file_path FROM chunks WHERE file_path > ? ORDER BY file_path",
        after="",
        limit=1,
    ) == ([], 0, None, "sidecar_unreadable")


def test_sidecar_rows_keeps_sqlite_schema_failure_distinct(tmp_path: Path) -> None:
    from exomem import index_paths

    sidecar = index_paths.sidecar_path(tmp_path)
    sidecar.parent.mkdir(parents=True)
    with _sqlite(sidecar):
        pass

    assert audit_module._sidecar_rows(
        tmp_path,
        sidecar,
        "SELECT missing_column FROM missing_table WHERE missing_column > ?",
        after="",
        limit=1,
    ) == ([], 0, None, "sidecar_schema_unreadable")


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows retained handles")
def test_windows_census_reads_healthy_regular_sidecar(tmp_path: Path) -> None:
    from exomem import embedding_index

    index = embedding_index.EmbeddingIndex(tmp_path)
    with _committed(index._connect()) as conn:
        conn.execute(
            "INSERT INTO chunks(file_path, chunk_idx, chunk_text, vector, file_mtime) "
            "VALUES ('../../corrupt.md', 0, 'private', X'00', 0)"
        )

    census = audit_module.semantic_recall_isolation_census(tmp_path)

    assert census.incomplete.get("vector") is None
    assert {row.component for row in census.corrupt_rows} == {"vector"}


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows retained handles")
def test_windows_sidecar_signature_changes_after_in_place_sqlite_write(tmp_path: Path) -> None:
    from exomem import embedding_index, index_paths

    index = embedding_index.EmbeddingIndex(tmp_path)
    conn = index._connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "INSERT INTO chunks(file_path, chunk_idx, chunk_text, vector, file_mtime) "
            "VALUES ('../../first.md', 0, 'private', X'00', 0)"
        )
        conn.commit()
    finally:
        conn.close()
    sidecar = index_paths.sidecar_path(tmp_path)
    before = audit_module._sidecar_signature(tmp_path, sidecar)

    conn = sqlite3.connect(sidecar)
    try:
        conn.execute(
            "INSERT INTO chunks(file_path, chunk_idx, chunk_text, vector, file_mtime) "
            "VALUES ('../../second.md', 0, 'private', X'00', 0)"
        )
        conn.commit()
    finally:
        conn.close()

    assert audit_module._sidecar_signature(tmp_path, sidecar) != before


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows reparse points")
@pytest.mark.parametrize("reparse_target", ["sidecar", "knowledge_base"])
def test_windows_census_rejects_reparse_before_sqlite_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reparse_target: str
) -> None:
    from exomem import embedding_index, index_paths

    index = embedding_index.EmbeddingIndex(tmp_path)
    conn = index._connect()
    try:
        conn.execute(
            "INSERT INTO chunks(file_path, chunk_idx, chunk_text, vector, file_mtime) "
            "VALUES ('../../corrupt.md', 0, 'private', X'00', 0)"
        )
        conn.commit()
    finally:
        conn.close()
    sidecar = index_paths.sidecar_path(tmp_path)
    external_root = tmp_path / "external"
    external_root.mkdir()
    if reparse_target == "sidecar":
        external = external_root / sidecar.name
        sidecar.replace(external)
        try:
            sidecar.symlink_to(external)
        except OSError:
            pytest.skip("cannot create native Windows sidecar reparse fixture")
    else:
        kb = sidecar.parent
        external_kb = external_root / "Knowledge Base"
        kb.replace(external_kb)
        try:
            kb.symlink_to(external_kb, target_is_directory=True)
        except OSError:
            pytest.skip("cannot create native Windows ancestor reparse fixture")

    monkeypatch.setattr(sqlite3, "connect", lambda *_args, **_kwargs: pytest.fail("SQLite followed reparse"))
    _rows, _truncated, _last, failure = audit_module._sidecar_rows(
        tmp_path,
        sidecar,
        "SELECT file_path FROM chunks WHERE file_path > ? ORDER BY file_path",
        after="",
        limit=1,
    )

    assert failure == "sidecar_unreadable"


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows retained handles")
def test_windows_sidecar_binding_allows_sqlite_reads_and_releases_replacement(tmp_path: Path) -> None:
    from exomem import embedding_index, index_paths

    index = embedding_index.EmbeddingIndex(tmp_path)
    conn = index._connect()
    try:
        conn.execute(
            "INSERT INTO chunks(file_path, chunk_idx, chunk_text, vector, file_mtime) "
            "VALUES ('../../corrupt.md', 0, 'private', X'00', 0)"
        )
        conn.commit()
    finally:
        conn.close()
    sidecar = index_paths.sidecar_path(tmp_path)
    replacement = tmp_path / "replacement.sqlite"
    replacement.write_bytes(sidecar.read_bytes())

    state, binding = audit_module._bind_sidecar(sidecar, writable=False)

    assert state == "regular"
    assert binding is not None
    try:
        conn = sqlite3.connect(f"{binding.path.as_uri()}?mode=ro", uri=True)
        try:
            assert conn.execute("SELECT file_path FROM chunks").fetchall() == [("../../corrupt.md",)]
        finally:
            conn.close()
        with pytest.raises(OSError):
            replacement.replace(sidecar)
    finally:
        binding.close()
    replacement.replace(sidecar)
    assert sidecar.read_bytes()


def test_corrupt_purge_suppresses_credit_after_final_identity_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import embedding_index

    index = embedding_index.EmbeddingIndex(tmp_path)
    with _committed(index._connect()) as conn:
        conn.execute(
            "INSERT INTO chunks(file_path, chunk_idx, chunk_text, vector, file_mtime) "
            "VALUES ('../../corrupt.md', 0, 'private', X'00', 0)"
        )
    closed: list[bool] = []
    binding = SimpleNamespace(
        path=index.path,
        entry_matches=lambda: False,
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(
        audit_module,
        "_bound_sidecar_repair",
        lambda path: binding if path == index.path else None,
    )
    monkeypatch.setattr(
        embedding_index.EmbeddingIndex,
        "purge_exact_persisted_rows",
        lambda self, values, **_kwargs: len(values),
    )

    assert audit_module.purge_corrupt_semantic_recall_isolation_rows(
        tmp_path,
        (audit_module._SemanticIsolationRow("vector", "../../corrupt.md", None),),
    ) == {}
    assert closed == [True]


def test_windows_binder_closes_partial_open_handles_in_reverse_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import index_paths, mutation_lock

    sidecar = index_paths.sidecar_path(tmp_path)
    opened: list[int] = []
    closed: list[int] = []

    def open_path(path: Path, **_kwargs) -> int:
        if path.name.endswith("-shm"):
            raise OSError("forced partial-open failure")
        handle = len(opened) + 1
        opened.append(handle)
        return handle

    monkeypatch.setattr(audit_module, "_sidecar_platform", lambda: "windows")
    monkeypatch.setattr(mutation_lock, "_windows_open_path", open_path)
    monkeypatch.setattr(mutation_lock, "_windows_close_handle", closed.append)
    monkeypatch.setattr(mutation_lock, "_windows_child_is_in_directory", lambda *_args: True)
    monkeypatch.setattr(mutation_lock, "_windows_handle_identity", lambda handle: (0, 0, handle))
    monkeypatch.setattr(audit_module, "_windows_handle_signature", lambda handle: (0, 0, handle, 0, 0))

    assert audit_module._bind_sidecar(sidecar, writable=False) == ("unreadable", None)
    assert opened == [1, 2, 3, 4]
    assert closed == [4, 3, 2, 1]


def _repaired_or_untouched(rows: list, corrupt: tuple) -> bool:
    """Whether a pinned-inode repair landed on the pinned file or refused.

    Which of the two happens is a property of the host, not of the code
    under test. Linux names the descriptor itself through procfs, so the
    repair always reaches the inode. macOS has only `F_GETPATH`, which
    answers from the vnode name cache: after the swap it may report the
    inode's new name or the old one, and the binding verifies identity and
    refuses rather than repair a file it cannot prove is the right one.

    An earlier version of these tests predicted which branch the host would
    take by staging the same swap on a second file and asking the resolver
    about it. That cannot work: the second probe resolves at a different
    point in the rename's lifetime than the binding does, so it answered
    `refused` on macOS runners where the real binding had reached the
    inode, and the test failed on a disagreement between two probes rather
    than on anything the product did.

    So this asserts the guarantee instead of the platform: the repair is
    all-or-nothing on the *pinned* file. The security claim -- that the
    file swapped in behind it is never touched -- is asserted separately
    and unconditionally by every caller, and the deterministic
    followed-the-descriptor case keeps its own exact assertion where
    procfs makes it deterministic.
    """
    return rows in ([], [corrupt])


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX no-follow descriptor binding")
def test_corrupt_purge_binds_repair_to_sidecar_inode_across_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import embedding_index, index_paths

    sidecar = index_paths.sidecar_path(tmp_path)
    index = embedding_index.EmbeddingIndex(tmp_path)
    with _committed(index._connect()) as conn:
        conn.execute(
            "INSERT INTO chunks(file_path, chunk_idx, chunk_text, vector, file_mtime) "
            "VALUES ('../../corrupt.md', 0, 'private', X'00', 0)"
        )
    external = tmp_path / "external.sqlite"
    # `backup()`, not `read_bytes()`. Copying the main database file alone is
    # complete only when no `-wal` sibling is still carrying committed pages,
    # and whether one exists here depends on connection lifetime rather than
    # on anything this test means to vary -- so the attacker's file could
    # arrive empty and the assertion that it stayed untouched would pass for
    # the wrong reason.
    with _sqlite(sidecar) as source, _sqlite(external) as target:
        source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        source.backup(target)
    # The attacker's file must hold the row BEFORE the purge, or the
    # "stayed untouched" assertion below cannot fail -- an empty decoy
    # passes it for the wrong reason, and an empty decoy that the purge
    # then reaches looks identical to one the copy never populated. Assert
    # the precondition so those two are never confused again: this line
    # failing means the setup is broken, the later one failing means the
    # repair reached a file it must never touch.
    with _sqlite(external) as conn:
        assert conn.execute("SELECT file_path FROM chunks").fetchall() == [
            ("../../corrupt.md",)
        ], "the decoy database was not populated; the swap assertion below is vacuous"
    moved = tmp_path / "moved.sqlite"
    real_purge = embedding_index.EmbeddingIndex.purge_exact_persisted_rows

    def swap_before_connect(self, values: list[str], **kwargs) -> int:
        sidecar.replace(moved)
        sidecar.symlink_to(external)
        return real_purge(self, values, **kwargs)

    monkeypatch.setattr(
        embedding_index.EmbeddingIndex,
        "purge_exact_persisted_rows",
        swap_before_connect,
    )

    deleted = audit_module.purge_corrupt_semantic_recall_isolation_rows(
        tmp_path,
        (audit_module._SemanticIsolationRow("vector", "../../corrupt.md", None),),
    )

    assert deleted == {}
    with _sqlite(external) as conn:
        assert conn.execute("SELECT file_path FROM chunks").fetchall() == [
            ("../../corrupt.md",)
        ]
    with _sqlite(moved) as conn:
        rows = conn.execute("SELECT file_path FROM chunks").fetchall()
    assert _repaired_or_untouched(rows, ("../../corrupt.md",)), rows
    if benchmark_capabilities.has_procfs_descriptor_paths():
        # procfs names the descriptor itself, so the repair reaches the
        # pinned inode whatever happened to its name. No cache, no
        # ambiguity, and therefore an exact expectation.
        assert rows == []


def test_corrupt_purge_refuses_without_a_bound_sidecar_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import embedding_index

    index = embedding_index.EmbeddingIndex(tmp_path)
    with _committed(index._connect()) as conn:
        conn.execute(
            "INSERT INTO chunks(file_path, chunk_idx, chunk_text, vector, file_mtime) "
            "VALUES ('../../corrupt.md', 0, 'private', X'00', 0)"
        )
    monkeypatch.setattr(audit_module, "_bound_sidecar_repair", lambda _path: None)

    assert audit_module.purge_corrupt_semantic_recall_isolation_rows(
        tmp_path,
        (audit_module._SemanticIsolationRow("vector", "../../corrupt.md", None),),
    ) == {}
    with _sqlite(index.path) as conn:
        assert conn.execute("SELECT file_path FROM chunks").fetchall() == [
            ("../../corrupt.md",)
        ]


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX no-follow descriptor binding")
def test_corrupt_purge_binds_claim_repair_to_sidecar_inode_across_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import claims

    sidecar = claims.sidecar_path(tmp_path)
    index = claims.ClaimIndex(tmp_path)
    with _committed(index._connect()) as conn:
        conn.execute(
            "INSERT INTO claims(file_path, claim_text, checksum, vector, file_mtime) "
            "VALUES ('../../corrupt.md', 'private', 'checksum', X'00', 0)"
        )
    external = tmp_path / "external-claims.sqlite"
    # `backup()`, not `read_bytes()`. Copying the main database file alone is
    # complete only when no `-wal` sibling is still carrying committed pages,
    # and whether one exists here depends on connection lifetime rather than
    # on anything this test means to vary -- so the attacker's file could
    # arrive empty and the assertion that it stayed untouched would pass for
    # the wrong reason.
    with _sqlite(sidecar) as source, _sqlite(external) as target:
        source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        source.backup(target)
    # The attacker's file must hold the row BEFORE the purge, or the
    # "stayed untouched" assertion below cannot fail -- an empty decoy
    # passes it for the wrong reason, and an empty decoy that the purge
    # then reaches looks identical to one the copy never populated. Assert
    # the precondition so those two are never confused again: this line
    # failing means the setup is broken, the later one failing means the
    # repair reached a file it must never touch.
    with _sqlite(external) as conn:
        assert conn.execute("SELECT file_path FROM claims").fetchall() == [
            ("../../corrupt.md",)
        ], "the decoy database was not populated; the swap assertion below is vacuous"
    moved = tmp_path / "moved-claims.sqlite"
    real_purge = claims.ClaimIndex.purge_exact_persisted_rows

    def swap_before_connect(self, values: list[str], **kwargs) -> int:
        sidecar.replace(moved)
        sidecar.symlink_to(external)
        return real_purge(self, values, **kwargs)

    monkeypatch.setattr(claims.ClaimIndex, "purge_exact_persisted_rows", swap_before_connect)

    deleted = audit_module.purge_corrupt_semantic_recall_isolation_rows(
        tmp_path,
        (audit_module._SemanticIsolationRow("claims", "../../corrupt.md", None),),
    )

    assert deleted == {}
    with _sqlite(external) as conn:
        assert conn.execute("SELECT file_path FROM claims").fetchall() == [
            ("../../corrupt.md",)
        ]
    with _sqlite(moved) as conn:
        rows = conn.execute("SELECT file_path FROM claims").fetchall()
    assert _repaired_or_untouched(rows, ("../../corrupt.md",)), rows
    if benchmark_capabilities.has_procfs_descriptor_paths():
        # procfs names the descriptor itself, so the repair reaches the
        # pinned inode whatever happened to its name. No cache, no
        # ambiguity, and therefore an exact expectation.
        assert rows == []


@pytest.mark.parametrize(
    "component",
    ["lexical", "vector", "claims", "deferred_semantic", "clip", "graph"],
)
def test_corrupt_purge_model_seams_accept_bound_connection_paths(component: str) -> None:
    from exomem import claims, deferred_index, embedding_index, epistemic_graph, lexstore
    from exomem.clip_index import ClipIndex

    seams = {
        "lexical": lexstore.purge_exact_persisted_rows,
        "vector": embedding_index.EmbeddingIndex.purge_exact_persisted_rows,
        "claims": claims.ClaimIndex.purge_exact_persisted_rows,
        "deferred_semantic": deferred_index.purge_exact_persisted_semantic_rows,
        "clip": ClipIndex.purge_exact_persisted_rows,
        "graph": epistemic_graph.EpistemicGraphIndex.purge_exact_persisted_rows,
    }

    assert "connection_path" in inspect.signature(seams[component]).parameters


def test_census_purges_graph_edge_placeholders_and_corrupt_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import deferred_index, epistemic_graph

    _raw, rel = _raw_record(tmp_path)
    graph = epistemic_graph.EpistemicGraphIndex(tmp_path)
    with _committed(graph._connect()) as conn:
        conn.execute(
            "INSERT INTO graph_edges(edge_key, src_key, dst_key, raw_relation, registry_status, "
            "registry_version, registry_hash, origin, source_path, metadata) "
            "VALUES ('edge', ?, 'file:Knowledge Base/Notes/target.md', 'links_to', 'known', 1, "
            "'registry', 'wikilink', ?, '{}')",
            (f"file:{rel}", rel),
        )
        conn.execute(
            "INSERT INTO graph_edges(edge_key, src_key, dst_key, raw_relation, registry_status, "
            "registry_version, registry_hash, origin, source_path, metadata) "
            "VALUES ('edge-target', 'file:Knowledge Base/Notes/source.md', ?, 'links_to', 'known', 1, "
            "'registry', 'wikilink', 'Knowledge Base/Notes/source.md', '{}')",
            (f"file:{rel}",),
        )
    with deferred_index._connect(tmp_path, create=True) as conn:
        for index in range(2):
            conn.execute(
                "INSERT INTO semantic_upserts(rel_path, created_at, updated_at, revision) "
                "VALUES (?, 0, 0, 1)",
                (f"../../corrupt-{index}.md",),
            )
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_INDEX", "1")
    census = audit_module.semantic_recall_isolation_census(tmp_path)
    assert {item["component"] for item in census.safe_dicts()} == {"graph_edges"}
    assert len(census.safe_dicts()) == 3
    limited = audit_module.semantic_recall_isolation_census(tmp_path, limit=1)
    assert limited.truncation["deferred_semantic"] > 0
    assert limited.corrupt_dicts()

    assert audit_module.purge_corrupt_semantic_recall_isolation_rows(
        tmp_path, limited.corrupt_rows
    )
    assert audit_module.semantic_recall_isolation_census(tmp_path).truncation == {}


def test_corrupt_purge_uses_sidecar_model_cleanup_and_invalidates_warm_state(
    tmp_path: Path,
) -> None:
    from exomem import claims, embeddings, epistemic_graph, lexstore

    bad_markdown = "../../corrupt.md"
    bad_clip = "../../corrupt-image"
    embedding = embeddings.get_embedding_index(tmp_path)
    embedding.upsert_file(
        bad_markdown,
        ["private"],
        np.zeros((1, 768), dtype=np.float32),
        0,
    )
    assert embedding.all_vectors()[0] == [(bad_markdown, 0)]

    clip = embeddings.get_clip_index(tmp_path)
    clip.upsert(bad_clip, np.zeros(512, dtype=np.float32), 0)
    assert clip.all_vectors()[0] == [bad_clip]

    claim = claims.get_claim_index(tmp_path)
    claim.upsert_many(
        [(bad_markdown, "private", "checksum", np.zeros(768, dtype=np.float32), None, None, 0)]
    )
    assert claim._all_claims_unchecked()[0][0][0] == bad_markdown

    graph = epistemic_graph.EpistemicGraphIndex(tmp_path)
    with _committed(graph._connect()) as conn:
        conn.execute(
            "INSERT INTO graph_nodes(node_key, kind, path, text, source_hash, metadata) "
            "VALUES ('corrupt', 'file', ?, 'private', 'hash', '{}')",
            (bad_markdown,),
        )
        epistemic_graph._bump_generation(conn)
        generation_before = conn.execute(
            "SELECT value FROM graph_meta WHERE key = 'generation'"
        ).fetchone()[0]

    lexical = lexstore.get_store(tmp_path)
    with _committed(lexical._connect()) as conn:
        lexical._ensure_schema(conn)
        conn.execute(
            "INSERT INTO pages(path, mtime_ns, updated, in_kb, in_vault, is_nav) "
            "VALUES (?, 1, '2026-01-01', 1, 1, 0)",
            (bad_markdown,),
        )
        rowid = conn.execute("SELECT rowid FROM pages WHERE path = ?", (bad_markdown,)).fetchone()[0]
        if lexstore.fts5_available():
            conn.execute("INSERT INTO fts(rowid, stemmed) VALUES (?, 'private')", (rowid,))
            conn.execute(
                "INSERT INTO tri(rowid, title_lower, body_lower) VALUES (?, 'private', 'private')",
                (rowid,),
            )

    census = audit_module.semantic_recall_isolation_census(tmp_path)
    assert audit_module.purge_corrupt_semantic_recall_isolation_rows(
        tmp_path, census.corrupt_rows
    )

    assert embedding.all_vectors()[0] == []
    assert clip.all_vectors()[0] == []
    assert claim._all_claims_unchecked()[0] == []
    with _committed(graph._connect()) as conn:
        assert conn.execute("SELECT path FROM graph_nodes").fetchall() == []
        assert conn.execute(
            "SELECT value FROM graph_meta WHERE key = 'generation'"
        ).fetchone()[0] != generation_before
    with _committed(lexical._connect()) as conn:
        assert conn.execute("SELECT path FROM pages").fetchall() == []
        if lexstore.fts5_available():
            assert conn.execute("SELECT rowid FROM fts").fetchall() == []
            assert conn.execute("SELECT rowid FROM tri").fetchall() == []


def test_reconcile_routes_safe_missing_rows_through_generic_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw, rel = _raw_record(tmp_path)
    _seed_suppressed_sidecars(tmp_path, rel)
    raw.unlink()
    monkeypatch.setattr("exomem.governance.receipts.reconcile", lambda *_args, **_kwargs: {})

    findings = audit_module.audit(
        tmp_path, categories=["semantic_recall_isolation"]
    ).findings
    assert any(
        finding.path == rel
        and finding.meta == {"component": "claims", "state": "missing"}
        for finding in findings
    )
    assert reconcile_module.reconcile(tmp_path, dry_run=True).semantic_missing_drift == [rel]
    report = reconcile_module.reconcile(tmp_path).as_dict()

    assert report["semantic_missing_purged"] == [rel]
    from exomem import claims, deferred_index

    with _sqlite(claims.sidecar_path(tmp_path)) as conn:
        assert conn.execute("SELECT file_path FROM claims").fetchall() == []
    with _sqlite(deferred_index.store_path(tmp_path)) as conn:
        assert conn.execute("SELECT rel_path FROM semantic_upserts").fetchall() == []


def test_reconcile_resumes_census_past_admitted_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import deferred_index

    for index in range(256):
        page = tmp_path / "Knowledge Base" / "Notes" / f"admitted-{index:03d}.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("admitted", encoding="utf-8")
        with deferred_index._connect(tmp_path, create=True) as conn:
            conn.execute(
                "INSERT INTO semantic_upserts(rel_path, created_at, updated_at, revision) "
                "VALUES (?, 0, 0, 1)",
                (page.relative_to(tmp_path).as_posix(),),
            )
    _raw, rel = _raw_record(tmp_path)
    with deferred_index._connect(tmp_path, create=True) as conn:
        conn.execute(
            "INSERT INTO semantic_upserts(rel_path, created_at, updated_at, revision) "
            "VALUES (?, 0, 0, 1)",
            (rel,),
        )
    monkeypatch.setattr("exomem.governance.receipts.reconcile", lambda *_args, **_kwargs: {})

    assert reconcile_module.reconcile(tmp_path).semantic_suppressed_drift == []
    # A separate CLI invocation imports fresh module state. The continuation
    # must be durable, not an in-process cursor.
    importlib.reload(reconcile_module)
    assert reconcile_module.reconcile(tmp_path).semantic_suppressed_purged == [rel]


def test_isolation_dry_run_does_not_persist_census_cursor(tmp_path: Path) -> None:
    from exomem import deferred_index

    for index in range(257):
        page = tmp_path / "Knowledge Base" / "Notes" / f"admitted-{index:03d}.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("admitted", encoding="utf-8")
        with deferred_index._connect(tmp_path, create=True) as conn:
            conn.execute(
                "INSERT INTO semantic_upserts(rel_path, created_at, updated_at, revision) "
                "VALUES (?, 0, 0, 1)",
                (page.relative_to(tmp_path).as_posix(),),
            )

    report = reconcile_module.reconcile(tmp_path, dry_run=True).as_dict()

    assert report["semantic_suppressed_truncation"]["deferred_semantic"] > 0
    assert deferred_index.semantic_isolation_cursors(tmp_path) == {}


def test_isolation_dry_run_does_not_mutate_a_legacy_deferred_sidecar(
    tmp_path: Path,
) -> None:
    from exomem import deferred_index

    path = deferred_index.store_path(tmp_path)
    path.parent.mkdir(parents=True)
    with _sqlite(path) as conn:
        conn.execute(
            "CREATE TABLE semantic_upserts ("
            "rel_path TEXT PRIMARY KEY, created_at REAL, updated_at REAL, revision INTEGER)"
        )
    before_bytes = path.read_bytes()
    before_stat = path.stat()

    reconcile_module.reconcile(tmp_path, dry_run=True)

    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime_ns == before_stat.st_mtime_ns
    with _sqlite(path) as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'maintenance_state'"
        ).fetchone() is None


def test_clearing_absent_isolation_cursor_does_not_create_deferred_sidecar(
    tmp_path: Path,
) -> None:
    from exomem import deferred_index

    assert deferred_index.set_semantic_isolation_cursors(tmp_path, {})
    assert not deferred_index.store_path(tmp_path).exists()


def test_unreadable_isolation_sidecar_is_reported_in_audit_and_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import deferred_index

    path = deferred_index.store_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not sqlite")
    monkeypatch.setattr("exomem.governance.receipts.reconcile", lambda *_args, **_kwargs: {})

    findings = audit_module.audit(
        tmp_path, categories=["semantic_recall_isolation"]
    ).findings
    assert any(
        finding.meta
        and finding.meta.get("component") == "deferred_semantic"
        and finding.meta.get("state") == "incomplete"
        for finding in findings
    )
    assert reconcile_module.reconcile(tmp_path, dry_run=True).as_dict()[
        "semantic_suppressed_incomplete"
    ] == {"deferred_semantic": "sidecar_schema_unreadable"}


def test_no_follow_markdown_path_rejects_reparse_point_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = tmp_path / "Knowledge Base" / "Notes" / "ordinary.md"
    page.parent.mkdir(parents=True)
    page.write_text("ordinary", encoding="utf-8")
    monkeypatch.setattr(audit_module, "_is_reparse_point", lambda _info: True)

    assert audit_module._no_follow_regular_markdown_path(
        tmp_path, "Knowledge Base/Notes/ordinary.md"
    ) is None
