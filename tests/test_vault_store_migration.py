"""Store mode's migration and rollback, driven through the owner's CLI against a real loopback coordinator.

OpenSpec make-vault-store-canonical tasks 1.3 to 1.5. The vault is generated: the collection-store
fixtures' legacy vault, moved into the collection store by ``collections migrate``, plus two linked notes.
Every case runs the offline-owner route on the real lease, as an operator at a terminal would. The
interpreter that dies in the crash case is a real child process, killed by the operating system.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import signal
import sqlite3
import sys
import time
from contextlib import closing

import pytest
from test_collection_store_migration import (
    MIGRATE,
    OPERATOR,
    legacy_vault,
    printed,
    stop_writer,
    vault_bytes,
)
from test_collection_store_s1_gate import _until
from test_managed_collections_migration import OPERATOR_SECRET, coordinator_url  # noqa: F401

from exomem import __main__ as cli
from exomem import (
    epistemic_graph,
    find,
    graph_sync,
    lexstore,
    records,
    state_migration,
    state_paths,
    writer_lease,
)
from exomem.collection_store import capability, connection, custody, replica, schema
from exomem.governance.principal import library_scope
from exomem.vault_store import migrate, mode, ownership

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="The offline-owner lease is Linux-only"
)

ALPHA = "Knowledge Base/Notes/Alpha.md"
BETA = "Knowledge Base/Notes/Beta.md"
DIAGRAM = "Knowledge Base/Notes/diagram.bin"
DIAGRAM_BYTES = bytes(range(256))
RELEASED = MIGRATE | {capability.VAULT_STORE_V1}
#: The lease handback republishes the collection replica here, so its bytes move even when nothing of the vault does.
REPLICA_DIRECTORY = "Knowledge Base/_Collections"


def _lease_environment(tmp_path, url):
    return {
        "EXOMEM_VAULT_PATH": str(tmp_path / "vault"),
        "EXOMEM_WRITER_LEASE_URL": url,
        "EXOMEM_WRITER_LEASE_TOPOLOGY": "single-host",
        "EXOMEM_WRITER_LEASE_VAULT_ID": "store-mode",
        "EXOMEM_WRITER_LEASE_REPLICA_ID": "service",
        "EXOMEM_WRITER_LEASE_TOKEN": "lease",
        "EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp_path / "lease-service"),
        "EXOMEM_WRITER_LEASE_PREFERRED": "1",
        # Short, so the crash case waits out a dead owner's lease in seconds.
        "EXOMEM_WRITER_LEASE_TTL": "3",
    }


@pytest.fixture
def vault(tmp_path, monkeypatch, coordinator_url, capsys):  # noqa: F811
    """A file-mode vault with built indexes, a collection store, and a single-host lease the owner can take."""
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv(OPERATOR, raising=False)
    monkeypatch.delenv(custody.SYNC_ROOTS_ENV, raising=False)
    monkeypatch.setattr("exomem.records._capture_sweep_carrier", lambda *a, **kw: None)
    monkeypatch.setattr("exomem.records._due_state_carrier", lambda *a, **kw: None)
    for name, value in _lease_environment(tmp_path, coordinator_url).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(capability, "RELEASED", RELEASED)
    with library_scope():
        root = legacy_vault(tmp_path / "vault")
        (root / ALPHA).write_text(
            "---\ntitle: Alpha\ntype: note\n---\n# Alpha\n\nThe lighthouse keeper logs the tide in [[Beta]].\n"
        )
        (root / DIAGRAM).write_bytes(DIAGRAM_BYTES)
        (root / BETA).write_text(
            "---\ntitle: Beta\ntype: note\n---\n# Beta\n\nThe tide table that the lighthouse keeper reads.\n"
        )
        with pytest.MonkeyPatch.context() as shell:
            shell.setenv(OPERATOR, OPERATOR_SECRET)
            assert cli._collections_main(["migrate", "--vault", str(root), "--apply"]) == 0
        build_indexes(root)
        capsys.readouterr()
        yield root
    stop_writer()


def build_indexes(root):
    """The owners' cold lexical and graph sidecars, as a service's first start leaves them."""
    graph_sync.await_active_rebuild(root)  # a write above starts one on its own thread
    lexstore.search_bm25(root, "lighthouse", 3, scope="kb", repair=True)
    epistemic_graph.EpistemicGraphIndex(root).rebuild_all()


def store_mode(root, *arguments, operator=True):
    """`exomem store-mode ...`, the operator credential set only for the command that applies."""
    with pytest.MonkeyPatch.context() as shell:
        if operator:
            shell.setenv(OPERATOR, OPERATOR_SECRET)
        return cli._store_mode_main([*arguments, "--vault", str(root)])


def migrate_apply(root):
    return store_mode(root, "migrate", "--apply", "--why", "drill")


def rollback_apply(root, export, *extra):
    return store_mode(
        root, "rollback", "--apply", "--export-to", str(export), "--why", "drill", *extra
    )


def table_rows(database, table):
    return sorted(database.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr)


def sidecar(path):
    return closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True))


def store_objects(root):
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return {name for (name,) in reader.execute("SELECT name FROM sqlite_master")}


def test_a_migration_stores_every_file_and_the_rows_equal_the_sidecars_and_no_vault_byte_changes(
    vault, capsys
):
    """A migration that rewrites a vault file, stores bytes that differ from the file, or installs rows that differ
    from the sidecars the readers use today."""
    before = vault_bytes(vault, outside=REPLICA_DIRECTORY)
    expected = {
        owned.rel: (vault / owned.rel).read_bytes()
        for owned in ownership.iter_owned(vault)
        if owned.ownership is ownership.Ownership.PAGE
    }
    assert store_mode(vault, "migrate", "--dry-run", operator=False) == 0
    assert printed(capsys)["pages"] == len(expected)
    assert migrate_apply(vault) == 0
    applied = printed(capsys)
    assert applied["status"] == "migrated" and applied["pages"] == len(expected)
    assert mode.store_mode_active(vault)
    with closing(connection.open_reader(connection.store_path(vault))) as store:
        assert dict(store.execute("SELECT path, body FROM store_pages")) == expected
        assert store.execute("SELECT path, sha256, size FROM store_binaries").fetchall() == [
            (DIAGRAM, hashlib.sha256(DIAGRAM_BYTES).hexdigest(), len(DIAGRAM_BYTES))
        ]
        assert store.execute("SELECT operation, reason FROM store_audit").fetchall() == [
            ("migrate", "drill")
        ]
        with sidecar(lexstore.lexical_path(vault)) as lexical:
            for table in ("pages", "fts", "semantic_units", "unit_fts"):
                assert table_rows(store, table) == table_rows(lexical, table)
            assert table_rows(lexical, "pages"), (
                "the lexical catalogue must hold pages for this check to mean anything"
            )
        with sidecar(epistemic_graph.sidecar_path(vault)) as graph:
            for table in ("graph_nodes", "graph_edges", "graph_dependencies"):
                assert table_rows(store, table) == table_rows(graph, table)
            assert table_rows(graph, "graph_edges"), (
                "the graph must hold the link for this check to mean anything"
            )
    assert vault_bytes(vault, outside=REPLICA_DIRECTORY) == before
    # The replica moves, so check what it carries: none of the pages, and none of the index objects that migration added.
    with closing(connection.open_reader(connection.store_path(vault))) as store:
        added = {name for (name,) in store.execute("SELECT name FROM store_mode_objects")}
    assert added, "migration must have recorded the objects it added for this check to mean anything"
    with closing(connection.open_reader(replica.replica_path(vault))) as copy:
        names = {name for (name,) in copy.execute("SELECT name FROM sqlite_master")}
        assert not names & added
        assert all(
            copy.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
            for table in schema.VAULT_STORE_TABLES
            if table in names
        )


def test_a_sidecar_row_that_no_longer_matches_its_file_refuses_the_migration_and_names_the_page(
    vault, capsys
):
    """A migration that copies a stale index into the store and calls it proven."""
    (vault / ALPHA).write_text(
        (vault / ALPHA).read_text() + "\nA later edit that the sidecar never saw.\n"
    )
    objects = store_objects(vault)
    assert migrate_apply(vault) == 1
    error = capsys.readouterr().err
    assert "VAULT_STORE_VERIFICATION_FAILED" in error and ALPHA in error
    assert store_objects(vault) == objects
    assert not mode.store_mode_active(vault)
    assert (
        state_migration.VAULT_STORE_COMPATIBILITY_ID
        not in state_migration.recorded_descriptor_ids(vault)
    )


def test_a_file_edited_between_staging_and_install_refuses_the_migration(
    vault, capsys, monkeypatch
):
    """A migration that installs the bytes it staged over a file a person changed while it proved."""
    prove = migrate.verify_staged

    def proved_then_edited(*arguments, **keywords):
        outcome = prove(*arguments, **keywords)
        (vault / BETA).write_text(
            "---\ntitle: Beta\ntype: note\n---\n# Beta\n\nEdited while the proof ran.\n"
        )
        return outcome

    monkeypatch.setattr(migrate, "verify_staged", proved_then_edited)
    objects = store_objects(vault)
    assert migrate_apply(vault) == 1
    error = capsys.readouterr().err
    assert "VAULT_STORE_FILES_CHANGED" in error and BETA in error
    assert store_objects(vault) == objects
    assert not mode.store_mode_active(vault)


def _migrate_and_die(environment, released):
    """`migrate --apply` in a fresh interpreter that the operating system kills inside the install transaction."""
    os.environ.clear()
    os.environ.update(environment)
    records._capture_sweep_carrier = records._due_state_carrier = lambda *a, **kw: None
    capability.RELEASED = released

    def die(*arguments, **keywords):
        os.kill(os.getpid(), signal.SIGKILL)

    migrate._recheck_files = die  # runs after the rows are copied and before the commit
    with library_scope():
        cli._store_mode_main(
            ["migrate", "--vault", environment["EXOMEM_VAULT_PATH"], "--apply", "--why", "kill"]
        )
    os._exit(0)


@pytest.mark.parametrize("rerun", ["migrate", "rollback"])
def test_a_kill_during_the_install_leaves_file_mode_and_either_command_reruns_cleanly(
    vault, capsys, tmp_path, rerun
):
    """A killed install that leaves a vault half in store mode, or an id that no command clears afterwards."""
    objects = store_objects(vault)
    child = multiprocessing.get_context("spawn").Process(
        target=_migrate_and_die,
        args=({**os.environ, OPERATOR: OPERATOR_SECRET}, RELEASED),
    )
    child.start()
    child.join(180)
    assert child.exitcode == -signal.SIGKILL
    assert not mode.store_mode_active(vault)
    assert store_objects(vault) == objects
    assert state_migration.VAULT_STORE_COMPATIBILITY_ID in state_migration.recorded_descriptor_ids(
        vault
    )
    client = writer_lease.LeaseCoordinatorClient(writer_lease.LeaseConfig.from_env())
    _until(lambda: (client.status().expires_at or 0) < time.time(), seconds=30)
    capsys.readouterr()
    if rerun == "migrate":
        assert migrate_apply(vault) == 0
        assert printed(capsys)["resumed_interrupted_activation"] is True
        assert mode.store_mode_active(vault)
    else:
        assert rollback_apply(vault, tmp_path / "export") == 0
        assert printed(capsys)["status"] == "interrupted-activation-cleared"
        assert (
            state_migration.VAULT_STORE_COMPATIBILITY_ID
            not in state_migration.recorded_descriptor_ids(vault)
        )
        assert not mode.store_mode_active(vault)


def test_a_rollback_returns_every_find_and_graph_answer_that_the_vault_gave_before_the_migration(
    vault, tmp_path
):
    """A rollback that loses a page or leaves an index that answers differently than before store mode, or a vault
    that cannot enter store mode again afterwards."""

    def answers():
        with library_scope():
            return (
                find.find(vault, query="lighthouse tide", mode="keyword", graph=False),
                epistemic_graph.graph_context(vault, path=ALPHA, depth=2),
            )

    before_files = vault_bytes(vault, outside=REPLICA_DIRECTORY)
    before = answers()
    assert ALPHA in json.dumps(before[0], default=str), (
        "the find answer must hit a page for this drill to mean anything"
    )
    assert before[1]["edges"], (
        "the graph answer must carry the link for this drill to mean anything"
    )
    assert migrate_apply(vault) == 0
    assert rollback_apply(vault, tmp_path / "export") == 0
    assert not mode.store_mode_active(vault)
    assert (
        state_migration.VAULT_STORE_COMPATIBILITY_ID
        not in state_migration.recorded_descriptor_ids(vault)
    )
    assert vault_bytes(vault, outside=REPLICA_DIRECTORY) == before_files
    assert answers() == before
    exported = json.loads((tmp_path / "export/export.json").read_text())
    assert exported["audit_rows"] == 1
    audit = [
        json.loads(line) for line in (tmp_path / "export/audit.jsonl").read_text().splitlines()
    ]
    assert [entry["operation"] for entry in audit] == ["migrate"]
    assert not store_objects(vault) & {"pages", "fts", "graph_nodes", "graph_edges"}
    with closing(connection.open_reader(connection.store_path(vault))) as store:
        assert store.execute("SELECT mode FROM store_mode").fetchone() == ("file",)
        assert store.execute("SELECT COUNT(*) FROM store_pages").fetchone() == (0,)
        assert [
            row[0] for row in store.execute("SELECT operation FROM store_audit ORDER BY seq")
        ] == [
            "migrate",
            "rollback",
        ]
    # A vault that returned to file mode migrates again, and the store keeps its history across both.
    assert migrate_apply(vault) == 0
    assert mode.store_mode_active(vault)


def test_a_person_edit_made_after_the_migration_blocks_the_rollback_until_it_is_exported(
    vault, capsys, tmp_path
):
    """A rollback that overwrites a person's edit that no importer took in, or one that exports nothing."""
    assert migrate_apply(vault) == 0
    stored = (vault / ALPHA).read_bytes()
    edited = stored + b"\nWritten in an editor after the migration.\n"
    (vault / ALPHA).write_bytes(edited)
    capsys.readouterr()
    assert rollback_apply(vault, tmp_path / "refused") == 1
    error = capsys.readouterr().err
    assert "VAULT_STORE_HELD_ITEMS" in error and ALPHA in error
    assert not (tmp_path / "refused").exists()
    assert mode.store_mode_active(vault)
    assert (vault / ALPHA).read_bytes() == edited
    assert rollback_apply(vault, tmp_path / "export", "--export-held") == 0
    assert (tmp_path / "export/held" / ALPHA).read_bytes() == edited
    assert (vault / ALPHA).read_bytes() == stored
    assert not mode.store_mode_active(vault)


def test_a_damaged_manifest_refuses_every_command_and_names_it_instead_of_reading_as_file_mode(
    vault, capsys
):
    """A command that treats a manifest it cannot read as "not in store mode" and goes on to migrate over it."""
    manifest = state_paths.vault_state_dir(vault) / state_migration.MANIFEST_NAME
    manifest.write_text("{not json")
    for command in (("status",), ("migrate", "--dry-run"), ("rollback", "--dry-run")):
        capsys.readouterr()
        assert store_mode(vault, *command, operator=False) == 1
        assert "STATE_MIGRATION_MANIFEST_INVALID" in capsys.readouterr().err


def test_a_migration_without_a_single_host_lease_refuses_before_it_takes_any_lease(
    vault, capsys, monkeypatch
):
    """A migration under a lease that can move to another host, where the store's pages have no replica story yet."""
    monkeypatch.delenv("EXOMEM_WRITER_LEASE_TOPOLOGY")
    assert migrate_apply(vault) == 1
    assert "VAULT_STORE_MULTI_HOST" in capsys.readouterr().err
    assert not mode.store_mode_active(vault)


def _stored_page(root, rel, body, *, held=None):
    """Make the store hold ``body`` for ``rel`` and, when given, a held edit, as a projector and importer would leave them."""
    with closing(sqlite3.connect(connection.store_path(root))) as store, store:
        store.execute(
            "UPDATE store_pages SET body = ?, sha256 = ?, size = ? WHERE path = ?",
            (body, hashlib.sha256(body).hexdigest(), len(body), rel),
        )
        if held is not None:
            store.execute(
                "INSERT INTO store_held(path, kind, body, detected_at) "
                "VALUES (?, 'edit', ?, '2026-01-01T00:00:00Z')",
                (rel, held),
            )


def test_a_rollback_exports_each_version_it_overwrites_when_the_importer_already_held_the_edit(
    vault, capsys, tmp_path
):
    """A rollback that leaves a held page's file as the person wrote it, or loses the held body when the file
    has since changed again."""
    assert migrate_apply(vault) == 0
    held_where_written, held_then_changed = b"Held, and still on disk.\n", b"Held body.\n"
    store_alpha, store_beta = b"The store's Alpha.\n", b"The store's Beta.\n"
    _stored_page(vault, ALPHA, store_alpha, held=held_where_written)
    _stored_page(vault, BETA, store_beta, held=held_then_changed)
    (vault / ALPHA).write_bytes(held_where_written)
    (vault / BETA).write_bytes(b"A third version, written after the importer held the second.\n")
    on_disk_beta = (vault / BETA).read_bytes()
    capsys.readouterr()
    assert rollback_apply(vault, tmp_path / "refused") == 1
    assert "VAULT_STORE_HELD_ITEMS" in capsys.readouterr().err
    assert rollback_apply(vault, tmp_path / "export", "--export-held") == 0
    result = printed(capsys)
    assert (vault / ALPHA).read_bytes() == store_alpha
    assert (vault / BETA).read_bytes() == store_beta
    assert (tmp_path / "export/held" / ALPHA).read_bytes() == held_where_written
    assert (tmp_path / "export/held" / BETA).read_bytes() == held_then_changed
    assert (tmp_path / "export/on-disk" / BETA).read_bytes() == on_disk_beta
    assert not (tmp_path / "export/on-disk" / ALPHA).exists()
    assert result["sidecars"] == {"lexical": "current", "graph": "rebuilt"}


def test_a_page_with_a_non_text_frontmatter_key_migrates(vault, capsys):
    """A YAML key that is a number, which JSON cannot hold, must not stop the migration of a vault that holds it."""
    page = "Knowledge Base/Notes/Year.md"
    (vault / page).write_text("---\ntitle: Year\n2024: the year of the flood\n---\n# Year\n\nA year page.\n")
    build_indexes(vault)
    capsys.readouterr()
    assert migrate_apply(vault) == 0
    with closing(connection.open_reader(connection.store_path(vault))) as store:
        (frontmatter,) = store.execute(
            "SELECT frontmatter_json FROM store_pages WHERE path = ?", (page,)
        ).fetchone()
    assert json.loads(frontmatter) == {"2024": "the year of the flood", "title": "Year"}


def test_an_interrupted_rollback_shows_as_its_own_state_and_the_command_finishes_it(
    vault, capsys, tmp_path
):
    """A status or a dry run that calls a vault with a retained id plain file mode or an interrupted activation."""
    assert migrate_apply(vault) == 0
    with closing(sqlite3.connect(connection.store_path(vault))) as store, store:
        store.execute("UPDATE store_mode SET mode = 'file'")
    capsys.readouterr()
    assert store_mode(vault, "status", operator=False) == 0
    assert printed(capsys)["mode"] == "interrupted-rollback"
    assert store_mode(vault, "rollback", "--dry-run", operator=False) == 0
    assert printed(capsys)["interrupted_rollback"] is True
    assert rollback_apply(vault, tmp_path / "export") == 0
    capsys.readouterr()
    assert store_mode(vault, "status", operator=False) == 0
    assert printed(capsys)["mode"] == "file"


def test_a_blank_reason_and_a_vault_that_is_not_in_store_mode_refuse_before_any_credential_is_read(
    vault, capsys, tmp_path
):
    """A command that takes the lease, or reaches the store, before it checks what it was asked to do."""
    assert store_mode(vault, "migrate", "--apply", "--why", "  ") == 2
    assert "non-blank --why" in capsys.readouterr().err
    assert (
        store_mode(
            vault, "rollback", "--apply", "--export-to", str(tmp_path / "x"), "--why", "r", operator=False
        )
        == 1
    )
    assert "VAULT_STORE_NOT_ACTIVE" in capsys.readouterr().err


def test_a_migration_and_a_rollback_each_remove_the_staging_directory_that_a_killed_run_left(
    vault, tmp_path
):
    """A killed run's private copy of the vault, which stays in the state directory until something removes it."""
    state_dir = state_paths.vault_state_dir(vault)
    leftover = state_dir / f"{migrate.STAGING_PREFIX}killed"
    for command in (lambda: migrate_apply(vault), lambda: rollback_apply(vault, tmp_path / "export")):
        leftover.mkdir()
        (leftover / "staged.sqlite").write_bytes(b"half a database")
        assert command() == 0
        assert not leftover.exists()
