"""The stored answer to "do raw dependencies cover every file row".

Proving it reads the whole dependency table, which took tens of seconds on a cold
page cache at 15,000 pages. The sidecar now keeps the answer as a row that every
change to the three tables retires, and that a writer records again only after
proving the sources it changed. These tests pin the two ways it could go wrong: a
check that trusts an answer a writer left stale, and a writer that loses the answer
a reader then pays to rebuild.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

import pytest

from exomem import epistemic_graph, freshness
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.epistemic_graph import EpistemicGraphIndex

NOTES = "Knowledge Base/Notes/Insights"
SOURCE = f"{NOTES}/source.md"
OTHER = f"{NOTES}/unrelated.md"
ADDED = f"{NOTES}/added.md"


def _page(title: str, body: str) -> str:
    return f"---\ntype: insight\nstatus: active\ntitle: {title}\n---\n\n# {title}\n\n{body}\n"


def _seed_live_freshness(root: Path) -> None:
    freshness.seed(
        root,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(root)),
    )
    freshness.seed(
        root,
        "kb",
        (
            (str(path), freshness.stat_signature(path))
            for path in find_module._walk_md(root / "Knowledge Base")
        ),
    )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / SOURCE).parent.mkdir(parents=True)
    (root / SOURCE).write_text(
        _page("Source", "Links to [[unrelated]] and a later [[added]]."), encoding="utf-8"
    )
    (root / OTHER).write_text(_page("Unrelated", "No links here."), encoding="utf-8")
    _seed_live_freshness(root)
    epistemic_graph.clear_publication_memos()
    EpistemicGraphIndex(root).rebuild_all()
    yield root
    epistemic_graph.clear_publication_memos()


def _recorded(root: Path) -> bool:
    connection = sqlite3.connect(epistemic_graph.sidecar_path(root))
    try:
        return (
            connection.execute(
                "SELECT 1 FROM graph_meta WHERE key = ?", (epistemic_graph._DEPENDENCY_PROOF_KEY,)
            ).fetchone()
            is not None
        )
    finally:
        connection.close()


def _check(root: Path) -> tuple[bool, bool]:
    """What the check answers, and whether it read the dependency table to do so."""
    index = EpistemicGraphIndex(root)
    connection = index._connect_existing(readonly=True)
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    try:
        connection.execute("BEGIN")
        complete = index._dependency_index_complete(connection)
    finally:
        connection.rollback()
        connection.close()
    return complete, any("graph_dependencies" in statement for statement in statements)


def _scan(root: Path) -> bool:
    """The whole-table proof itself, whatever the sidecar records."""
    index = EpistemicGraphIndex(root)
    connection = index._connect_existing(readonly=True)
    try:
        connection.execute("BEGIN")
        return index._prove_dependency_index(connection)
    finally:
        connection.rollback()
        connection.close()


def _edit(root: Path, rel: str) -> Path:
    page = root / rel
    page.write_text(page.read_text(encoding="utf-8") + "\nAn edit.\n", encoding="utf-8")
    freshness.rebaseline(root)
    return page


def _create(root: Path, rel: str) -> Path:
    page = root / rel
    page.write_text(_page("Added", "Links back to [[source]]."), encoding="utf-8")
    freshness.rebaseline(root)
    return page


def _sql(root: Path, script: str) -> None:
    """Change rows the way a repair does: straight through SQL, outside every writer here."""
    connection = EpistemicGraphIndex(root)._connect()
    try:
        connection.executescript(script)
        connection.commit()
    finally:
        connection.close()


def test_a_rebuild_records_the_answer_and_a_check_then_reads_no_dependency_rows(
    vault: Path,
) -> None:
    """The cold-cache cost is the table read, so a recorded answer must not read it."""
    assert _recorded(vault)
    assert _check(vault) == (True, False)


_COPY_A_FILE_ROW = (
    "CREATE TEMP TABLE ghost AS SELECT * FROM graph_nodes WHERE node_key = 'file:{source}';"
    "UPDATE ghost SET node_key = 'file:ghost.md', path = 'ghost.md';"
    "INSERT INTO graph_nodes SELECT * FROM ghost;"
)


@pytest.mark.parametrize(
    "script",
    [
        pytest.param(_COPY_A_FILE_ROW.format(source=SOURCE), id="file row inserted"),
        pytest.param(
            f"UPDATE graph_nodes SET source_hash = 'other' WHERE node_key = 'file:{SOURCE}';",
            id="file row updated",
        ),
        pytest.param(
            f"DELETE FROM graph_nodes WHERE node_key = 'file:{SOURCE}';", id="file row deleted"
        ),
        pytest.param(
            f"INSERT INTO graph_dependencies VALUES ('{SOURCE}', 'extra', 'extra');",
            id="dependency inserted",
        ),
        pytest.param(
            f"UPDATE graph_dependencies SET source_path = 'ghost.md' WHERE source_path = '{SOURCE}';",
            id="dependency updated",
        ),
        pytest.param(
            f"DELETE FROM graph_dependencies WHERE source_path = '{SOURCE}';",
            id="dependency deleted",
        ),
        pytest.param(
            "INSERT INTO graph_dependency_coverage VALUES ('ghost.md', 'x', 2, 0);",
            id="coverage inserted",
        ),
        pytest.param(
            f"UPDATE graph_dependency_coverage SET expected_count = 9 WHERE source_path = '{SOURCE}';",
            id="coverage updated",
        ),
        pytest.param(
            f"DELETE FROM graph_dependency_coverage WHERE source_path = '{OTHER}';",
            id="coverage deleted",
        ),
    ],
)
def test_a_direct_row_change_is_never_answered_from_the_stored_proof(
    vault: Path, script: str
) -> None:
    """A repair or an older release edits rows with no knowledge of the record."""
    assert _recorded(vault)

    _sql(vault, script)

    assert not _recorded(vault)
    assert _check(vault) == (False, True)


def test_an_exact_purge_that_leaves_a_file_row_without_coverage_is_not_trusted(
    vault: Path,
) -> None:
    """The one writer here that drops dependency rows on purpose and keeps the file row."""
    connection = sqlite3.connect(epistemic_graph.sidecar_path(vault))
    try:
        dependency = connection.execute(
            "SELECT source_path, lookup_key, raw_target FROM graph_dependencies "
            "WHERE source_path = ? LIMIT 1",
            (SOURCE,),
        ).fetchone()
    finally:
        connection.close()

    EpistemicGraphIndex(vault).purge_exact_persisted_rows([], {}, dependency_rows=[dependency])

    assert _check(vault) == (False, True)


def test_a_schema_migration_that_drops_the_file_rows_retires_the_stored_proof(
    vault: Path,
) -> None:
    """`DROP TABLE` fires no row trigger, and coverage outlives the file rows it described."""
    _sql(vault, "ALTER TABLE graph_nodes DROP COLUMN activation_unregistered;")
    assert _recorded(vault)

    EpistemicGraphIndex(vault)._connect().close()

    assert not _recorded(vault)
    assert _check(vault) == (False, True)


def test_a_release_with_another_dependency_format_does_not_trust_the_proof(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record says what format its rows were proved in."""
    monkeypatch.setattr(
        epistemic_graph, "_DEPENDENCY_FORMAT", epistemic_graph._DEPENDENCY_FORMAT + 1
    )

    assert _check(vault) == (False, True)


def test_a_rebuild_whose_rows_disagree_records_no_proof(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rebuild proves before it records: here two file rows collapse into one key."""
    monkeypatch.setattr(epistemic_graph, "_file_key", lambda rel: "file:collapsed")

    EpistemicGraphIndex(vault).rebuild_all()

    assert not _recorded(vault)
    assert _check(vault) == (False, True)


@pytest.mark.parametrize(
    "keep",
    [
        pytest.param(
            lambda root: EpistemicGraphIndex(root).drain_paths([_edit(root, SOURCE)]),
            id="drain of an edited page",
        ),
        pytest.param(
            lambda root: EpistemicGraphIndex(root).drain_paths([_create(root, ADDED)]),
            id="drain of a created page",
        ),
        pytest.param(
            lambda root: EpistemicGraphIndex(root).delete_paths([OTHER]),
            id="deletion of a page",
        ),
    ],
)
def test_a_writer_that_keeps_the_index_complete_keeps_the_answer(vault: Path, keep) -> None:
    """Losing the record would send the next cold start back to the full table read."""
    keep(vault)

    assert _recorded(vault)
    assert _scan(vault)
    assert _check(vault) == (True, False)


def test_a_pass_over_a_sidecar_without_the_record_proves_the_table_once_and_records_it(
    vault: Path,
) -> None:
    """A sidecar written before the record existed heals on its first write, not on every read."""
    _sql(vault, f"DELETE FROM graph_meta WHERE key = '{epistemic_graph._DEPENDENCY_PROOF_KEY}';")
    assert _check(vault) == (True, True)

    EpistemicGraphIndex(vault).drain_paths([_edit(vault, SOURCE)])

    assert _check(vault) == (True, False)


def test_a_pass_whose_rows_disagree_with_their_coverage_records_nothing(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-source proof is what lets a pass skip the table; it must still refuse."""
    real_insert = epistemic_graph._insert_node

    def insert_with_other_bytes(conn: sqlite3.Connection, node: epistemic_graph.GraphNode) -> None:
        real_insert(conn, dataclasses.replace(node, source_hash="not the covered bytes"))

    monkeypatch.setattr(epistemic_graph, "_insert_node", insert_with_other_bytes)

    EpistemicGraphIndex(vault).drain_paths([_edit(vault, SOURCE)])

    assert not _recorded(vault)
    assert _check(vault) == (False, True)


def test_a_pass_that_fails_before_it_commits_leaves_the_proof_and_the_rows_it_had(
    vault: Path,
) -> None:
    """Rows and record commit together, so no crash leaves one without the other."""
    index = EpistemicGraphIndex(vault)
    page = _edit(vault, SOURCE)
    resolver = find_module.recall_resolver_snapshot(
        vault, expected_checkpoint=freshness.recall_checkpoint(vault, "vault")
    )

    def crash(_conn: sqlite3.Connection) -> None:
        raise RuntimeError("process died before the commit")

    with pytest.raises(RuntimeError):
        index._refresh_paths_pass([page], resolver=resolver, before_commit=crash)

    assert _recorded(vault)
    assert _scan(vault)
    assert _check(vault) == (True, False)


def test_rows_committed_without_the_record_are_proved_again_not_trusted(vault: Path) -> None:
    """The other half of a crash: a writer that changed rows and never reached the record."""
    index = EpistemicGraphIndex(vault)
    page = _edit(vault, SOURCE)
    resolver = find_module.recall_resolver_snapshot(
        vault, expected_checkpoint=freshness.recall_checkpoint(vault, "vault")
    )
    connection = index._connect()
    try:
        # `commit=True` opens no proof scope, as a writer that predates the record would not.
        index._index_path(connection, page, resolver=resolver, commit=True)
    finally:
        connection.close()

    assert not _recorded(vault)
    assert _check(vault) == (True, True)
