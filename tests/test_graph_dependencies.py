"""Durable raw body-link dependencies for proportional graph discovery."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from exomem import audit, epistemic_graph, reconcile
from exomem.epistemic_graph import EpistemicGraphIndex

SOURCE = "Knowledge Base/Notes/Insights/source.md"
OTHER = "Knowledge Base/Notes/Insights/unrelated.md"
TARGET = "Knowledge Base/Notes/Insights/future.md"


def _page(title: str, body: str) -> str:
    return f"---\ntype: insight\nstatus: active\n---\n# {title}\n\n## Claim\n\n{body}\n"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / SOURCE).parent.mkdir(parents=True)
    (root / SOURCE).write_text(
        _page("Source", "A forward link [[future|later]] and [[missing#part]]."),
        encoding="utf-8",
    )
    (root / OTHER).write_text(_page("Unrelated", "No links here."), encoding="utf-8")
    EpistemicGraphIndex(root).rebuild_all()
    return root


def test_rebuild_persists_unresolved_raw_targets_and_zero_link_coverage(vault: Path) -> None:
    """Unresolved authored targets remain internal and linkless pages are covered."""
    conn = sqlite3.connect(EpistemicGraphIndex(vault).path)
    try:
        rows = conn.execute(
            "SELECT lookup_key, raw_target FROM graph_dependencies WHERE source_path = ? "
            "ORDER BY raw_target, lookup_key",
            (SOURCE,),
        ).fetchall()
        coverage = dict(
            conn.execute(
                "SELECT source_path, expected_count FROM graph_dependency_coverage"
            ).fetchall()
        )
        coverage_columns = {
            name: not_null
            for _cid, name, _kind, not_null, _default, _primary_key in conn.execute(
                "PRAGMA table_info(graph_dependency_coverage)"
            )
        }
    finally:
        conn.close()

    assert any(raw == "future|later" for _key, raw in rows)
    assert any(raw == "missing#part" for _key, raw in rows)
    assert coverage[SOURCE] == len(rows)
    assert coverage[OTHER] == 0
    assert coverage_columns["source_path"] == 1


def test_topology_lookup_does_not_read_unrelated_bodies(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forward-reference repair queries dependencies instead of scanning every page."""
    target = vault / TARGET
    target.write_text(_page("Future", "The target now exists."), encoding="utf-8")
    resolver = epistemic_graph.vault_module.WikilinkResolver(vault)
    read: list[str] = []
    real_read = epistemic_graph.vault_module.read_bytes_without_pinning

    def record(path: Path) -> bytes:
        read.append(str(path.relative_to(vault)))
        return real_read(path)

    monkeypatch.setattr(epistemic_graph.vault_module, "read_bytes_without_pinning", record)
    index = EpistemicGraphIndex(vault)
    conn = index._connect()
    try:
        affected = index._topology_affected_sources(conn, {TARGET}, resolver=resolver)
    finally:
        conn.close()

    assert affected == {SOURCE}
    assert OTHER not in read


def test_incomplete_dependency_coverage_refuses_topology_repair(vault: Path) -> None:
    """A positive hit cannot bless an index missing another source's coverage."""
    index = EpistemicGraphIndex(vault)
    target = vault / TARGET
    target.write_text(_page("Future", "The target now exists."), encoding="utf-8")
    conn = index._connect()
    try:
        conn.execute("DELETE FROM graph_dependency_coverage WHERE source_path = ?", (OTHER,))
        conn.commit()
        resolver = epistemic_graph.vault_module.WikilinkResolver(vault)
        assert index._topology_affected_sources(conn, {TARGET}, resolver=resolver) is None
    finally:
        conn.close()


def test_rebuild_and_exact_deletion_remove_dependency_rows(vault: Path) -> None:
    """Derived dependencies cannot outlive their admitted graph source."""
    index = EpistemicGraphIndex(vault)
    conn = index._connect()
    try:
        conn.execute(
            "INSERT INTO graph_dependencies(source_path, lookup_key, raw_target) VALUES (?, ?, ?)",
            ("Knowledge Base/Notes/Insights/orphan.md", "orphan", "orphan"),
        )
        conn.execute(
            "INSERT INTO graph_dependency_coverage("
            "source_path, source_hash, dependency_format, expected_count"
            ") "
            "VALUES (?, ?, ?, ?)",
            ("Knowledge Base/Notes/Insights/orphan.md", "x", 1, 1),
        )
        conn.commit()
    finally:
        conn.close()

    index.rebuild_all()
    index.delete_paths([SOURCE])
    conn = sqlite3.connect(index.path)
    try:
        assert conn.execute(
            "SELECT 1 FROM graph_dependencies WHERE source_path IN (?, ?) LIMIT 1",
            (SOURCE, "Knowledge Base/Notes/Insights/orphan.md"),
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM graph_dependency_coverage WHERE source_path IN (?, ?) LIMIT 1",
            (SOURCE, "Knowledge Base/Notes/Insights/orphan.md"),
        ).fetchone() is None
    finally:
        conn.close()


def test_exact_node_purge_invalidates_its_dependency_rows_and_coverage(vault: Path) -> None:
    """A quarantined graph node cannot leave its private dependency projection behind."""
    index = EpistemicGraphIndex(vault)

    assert index.purge_exact_persisted_rows([SOURCE], {}) > 0

    conn = sqlite3.connect(index.path)
    try:
        assert conn.execute(
            "SELECT 1 FROM graph_dependencies WHERE source_path = ?", (SOURCE,)
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM graph_dependency_coverage WHERE source_path = ?", (SOURCE,)
        ).fetchone() is None
    finally:
        conn.close()


def test_isolation_repair_quarantines_invalid_dependency_key_and_coverage(vault: Path) -> None:
    """Authored target data is validated without requiring the target to exist."""
    index = EpistemicGraphIndex(vault)
    conn = index._connect()
    try:
        conn.execute(
            "INSERT INTO graph_dependencies(source_path, lookup_key, raw_target) VALUES (?, ?, ?)",
            (SOURCE, "not-the-authored-target", "future"),
        )
        conn.execute(
            "UPDATE graph_dependency_coverage SET expected_count = expected_count + 1 "
            "WHERE source_path = ?",
            (SOURCE,),
        )
        conn.commit()
    finally:
        conn.close()

    census = audit.semantic_recall_isolation_census(vault)
    corrupt = tuple(row for row in census.corrupt_rows if row.component == "graph_dependencies")
    assert corrupt
    audit.purge_corrupt_semantic_recall_isolation_rows(vault, corrupt)

    conn = sqlite3.connect(index.path)
    try:
        assert conn.execute(
            "SELECT 1 FROM graph_dependencies WHERE source_path = ?", (SOURCE,)
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM graph_dependency_coverage WHERE source_path = ?", (SOURCE,)
        ).fetchone() is None
    finally:
        conn.close()


def test_dependency_census_continues_across_rows_for_one_source(vault: Path) -> None:
    """The dependency cursor uses the unique persisted row identity, not its source."""
    index = EpistemicGraphIndex(vault)
    conn = index._connect()
    try:
        conn.execute("DELETE FROM graph_dependencies WHERE source_path = ?", (SOURCE,))
        conn.executemany(
            "INSERT INTO graph_dependencies(source_path, lookup_key, raw_target) VALUES (?, ?, ?)",
            [(SOURCE, "invalid-one", "first"), (SOURCE, "invalid-two", "second")],
        )
        conn.execute(
            "UPDATE graph_dependency_coverage SET expected_count = 2 WHERE source_path = ?",
            (SOURCE,),
        )
        conn.commit()
    finally:
        conn.close()

    first = audit.semantic_recall_isolation_census(vault, limit=1)
    second = audit.semantic_recall_isolation_census(vault, limit=1, after=first.continuation)
    first_rows = [row for row in first.corrupt_rows if row.component == "graph_dependencies"]
    second_rows = [row for row in second.corrupt_rows if row.component == "graph_dependencies"]

    assert first_rows and second_rows
    assert first_rows[0].dependency_raw_target == "first"
    assert second_rows[0].dependency_raw_target == "second"

    conn = index._connect()
    try:
        conn.execute(
            "UPDATE graph_dependencies SET raw_target = ? WHERE source_path = ? AND lookup_key = ?",
            ("revised", SOURCE, "invalid-one"),
        )
        conn.commit()
    finally:
        conn.close()
    reset = audit.semantic_recall_isolation_census(vault, limit=1, after=first.continuation)
    reset_rows = [row for row in reset.corrupt_rows if row.component == "graph_dependencies"]

    assert reset_rows[0].dependency_raw_target == "revised"


def test_dependency_cursor_signature_uses_windows_wal_size(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows file-index fields cannot stand in for a nonempty WAL size."""
    sidecar = EpistemicGraphIndex(vault).path
    windows_main = (17, 4, 2, 8192, 100)
    signatures: list[tuple[str, tuple[int, ...]]] = []

    def bind(_path: Path, *, writable: bool):
        assert not writable
        return "regular", audit._BoundSidecarRepair((), sidecar, (), tuple(signatures))

    monkeypatch.setattr(audit, "_bind_sidecar", bind)
    signatures[:] = [(sidecar.name, windows_main)]
    absent_wal = audit._graph_dependency_census_signature(vault, sidecar)
    signatures[:] = [
        (sidecar.name, windows_main),
        (f"{sidecar.name}-wal", (17, 4, 9, 0, 100)),
    ]
    assert audit._graph_dependency_census_signature(vault, sidecar) == absent_wal
    signatures[:] = [
        (sidecar.name, windows_main),
        (f"{sidecar.name}-wal", (17, 4, 0, 4096, 100)),
    ]
    nonempty_wal = audit._graph_dependency_census_signature(vault, sidecar)
    assert nonempty_wal != absent_wal
    signatures[:] = [
        (sidecar.name, windows_main),
        (f"{sidecar.name}-wal", (17, 4, 0, 4096, 101)),
    ]
    assert audit._graph_dependency_census_signature(vault, sidecar) != nonempty_wal


def test_dependency_census_refuses_continuation_without_durable_signature(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable revision cannot authorize reuse or caption a census page."""
    monkeypatch.setattr(audit, "_graph_dependency_census_signature", lambda *_args: None)

    census = audit.semantic_recall_isolation_census(
        vault,
        limit=1,
        after={
            "graph_dependency_coverage": {"cursor": "999", "signature": None},
            "graph_dependencies": {"cursor": "999", "signature": None},
        },
    )

    assert "graph_dependency_coverage" not in census.continuation
    assert "graph_dependencies" not in census.continuation
    assert census.incomplete["graph_dependency_coverage"] == "sidecar_revision_unavailable"
    assert census.incomplete["graph_dependencies"] == "sidecar_revision_unavailable"


def test_isolation_repair_removes_blob_dependency_source_and_coverage(vault: Path) -> None:
    """A BLOB source identity is retained for exact graph-sidecar cleanup."""
    index = EpistemicGraphIndex(vault)
    corrupt_source = sqlite3.Binary(b"corrupt-source.md")
    conn = index._connect()
    try:
        conn.execute(
            "INSERT INTO graph_dependencies(source_path, lookup_key, raw_target) VALUES (?, ?, ?)",
            (corrupt_source, sqlite3.Binary(b"corrupt-key"), sqlite3.Binary(b"corrupt-target")),
        )
        conn.execute(
            "INSERT INTO graph_dependency_coverage("
            "source_path, source_hash, dependency_format, expected_count"
            ") VALUES (?, ?, ?, ?)",
            (corrupt_source, "corrupt", 1, 1),
        )
        conn.commit()
    finally:
        conn.close()

    census = audit.semantic_recall_isolation_census(vault)
    corrupt = tuple(row for row in census.corrupt_rows if row.component == "graph_dependencies")
    assert corrupt
    audit.purge_corrupt_semantic_recall_isolation_rows(vault, corrupt)

    conn = sqlite3.connect(index.path)
    try:
        assert conn.execute(
            "SELECT 1 FROM graph_dependencies WHERE source_path = ?", (corrupt_source,)
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM graph_dependency_coverage WHERE source_path = ?", (corrupt_source,)
        ).fetchone() is None
    finally:
        conn.close()
    assert not [
        row
        for row in audit.semantic_recall_isolation_census(vault).corrupt_rows
        if row.component in {"graph_dependencies", "graph_dependency_coverage"}
    ]


def test_isolation_repair_removes_corrupt_coverage_without_dependency_rows(vault: Path) -> None:
    """A corrupt coverage-only source is routed through the graph purge seam."""
    index = EpistemicGraphIndex(vault)
    corrupt_source = "../../coverage-only.md"
    conn = index._connect()
    try:
        conn.execute(
            "INSERT INTO graph_dependency_coverage("
            "source_path, source_hash, dependency_format, expected_count"
            ") VALUES (?, ?, ?, ?)",
            (corrupt_source, "corrupt", 1, 0),
        )
        conn.commit()
    finally:
        conn.close()

    census = audit.semantic_recall_isolation_census(vault)
    corrupt = tuple(
        row for row in census.corrupt_rows if row.component == "graph_dependency_coverage"
    )
    assert corrupt
    audit.purge_corrupt_semantic_recall_isolation_rows(vault, corrupt)

    conn = sqlite3.connect(index.path)
    try:
        assert conn.execute(
            "SELECT 1 FROM graph_dependency_coverage WHERE source_path = ?", (corrupt_source,)
        ).fetchone() is None
    finally:
        conn.close()
    assert not [
        row
        for row in audit.semantic_recall_isolation_census(vault).corrupt_rows
        if row.component == "graph_dependency_coverage"
    ]
    assert reconcile.reconcile(vault, dry_run=True).semantic_suppressed_corrupt == []


def test_isolation_repair_enumerates_legacy_null_coverage_source(vault: Path) -> None:
    """A legacy nullable coverage row is found by its rowid and purged exactly."""
    index = EpistemicGraphIndex(vault)
    conn = index._connect()
    try:
        conn.execute("DROP TABLE graph_dependency_coverage")
        conn.execute(
            "CREATE TABLE graph_dependency_coverage ("
            "source_path TEXT PRIMARY KEY, source_hash TEXT NOT NULL, "
            "dependency_format INTEGER NOT NULL, expected_count INTEGER NOT NULL"
            ")"
        )
        conn.execute(
            "INSERT INTO graph_dependency_coverage("
            "source_path, source_hash, dependency_format, expected_count"
            ") VALUES (?, ?, ?, ?)",
            (None, "corrupt", 1, 0),
        )
        conn.commit()
    finally:
        conn.close()

    census = audit.semantic_recall_isolation_census(vault)
    corrupt = tuple(
        row for row in census.corrupt_rows if row.component == "graph_dependency_coverage"
    )
    assert corrupt
    audit.purge_corrupt_semantic_recall_isolation_rows(vault, corrupt)

    conn = sqlite3.connect(index.path)
    try:
        assert conn.execute(
            "SELECT 1 FROM graph_dependency_coverage WHERE source_path IS NULL"
        ).fetchone() is None
    finally:
        conn.close()
    assert not [
        row
        for row in audit.semantic_recall_isolation_census(vault).corrupt_rows
        if row.component == "graph_dependency_coverage"
    ]
