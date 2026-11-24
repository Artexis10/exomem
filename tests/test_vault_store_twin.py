"""A file-mode vault and its store-mode twin answer alike (OpenSpec make-vault-store-canonical).

The store twin is the migration suite's vault, moved into store mode by the real migrate route. The
file twin is a copy of its files at another path, so it is a file-mode vault: a vault's mode lives
in state that is keyed by the vault's path. Each step runs the same writer on both. After the store
twin's projector and importer have settled, the store twin's files and its derived tables must equal
what file mode wrote and what file mode derives from those files.
"""

from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
from contextlib import closing

import pytest
import test_vault_store_read_write as read_write
import test_vault_store_write_path as write_path
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_bypass_writers import govern
from test_vault_store_migration import BETA, DIAGRAM, build_indexes, vault  # noqa: F401
from test_vault_store_served import call, derived, service  # noqa: F401
from test_vault_store_write_path import planned, store_rows

from exomem import compiled_history, delete_directory, delete_file, move_file, recover_from_trash
from exomem import vault as vault_module
from exomem.collection_store import connection
from exomem.vault_store import importer, ownership, projector, verify

pytestmark = [
    pytest.mark.timeout(300),
]

stored, writer, served = read_write.stored, read_write.writer, write_path.served

TODAY = dt.date(2026, 11, 14)
LINKER = "Knowledge Base/Notes/Harbour.md"
TARGET = "Knowledge Base/Notes/Lighthouse.md"
MOVED = "Knowledge Base/Notes/Old Lighthouse.md"
FOLDER = "Knowledge Base/Projects/Tides"


def page(title, body):
    return f"---\ntitle: {title}\ntype: note\n---\n# {title}\n\n{body}\n"


def write(root, rel, text, **keywords):
    vault_module.batch_atomic_write(
        [planned(root, rel, text, **keywords)], vault_root=root, post_commit_fanout=False
    )


def stream(root):
    """The same governed writes, in order, on one vault."""
    write(root, TARGET, page("Lighthouse", "The keeper logs the tide."), create_only=True)
    write(root, LINKER, page("Harbour", "The harbour answers to [[Lighthouse]]."), create_only=True)
    write(root, TARGET, page("Lighthouse", "The keeper logs the tide twice a day."))
    move_file.move_file(root, old_path=TARGET, new_path=MOVED, today=TODAY)
    trashed = delete_file.delete_file(
        root, path=LINKER, confirm=True, force_orphan=True, today=TODAY,
        now=dt.datetime(2026, 11, 14, 9, 0, 0),
    )
    recover_from_trash.recover_from_trash(root, trash_path=trashed.trash_path, today=TODAY)
    for name in ("Neap", "Spring"):
        text = page(name, f"The {name.lower()} tide.")
        write(root, f"{FOLDER}/{name}.md", text, create_only=True)
    folder = delete_directory.delete_directory(
        root, path=FOLDER, confirm=True, recursive=True, force_orphan=True, today=TODAY,
        now=dt.datetime(2026, 11, 14, 9, 5, 0),
    )
    recover_from_trash.recover_from_trash(root, trash_path=folder.trash_path, today=TODAY)
    binary = delete_file.delete_file(
        root, path=DIAGRAM, confirm=True, force_orphan=True, today=TODAY,
        now=dt.datetime(2026, 11, 14, 9, 10, 0),
    )
    recover_from_trash.recover_from_trash(root, trash_path=binary.trash_path, today=TODAY)
    govern(root, BETA)
    delete_file.delete_file(
        root, path=BETA, confirm=True, force_orphan=True, today=TODAY,
        now=dt.datetime(2026, 11, 14, 9, 15, 0),
    )


def settled(root, writer):
    """The store twin once its projector has drained the queue and its importer has swept.

    With no ``writer`` they reach the store through the running store thread's door, and the thread
    derives the rows of the served writes after their replies, so this waits for that too.
    """
    run = None if writer is None else lambda work: work(writer)
    for _ in range(10):
        projector.Projector(root, run=run).run_pass()
        importer.Importer(root, run=run).sweep()
        if not store_rows(root, "SELECT path FROM store_projection_queue"):
            if writer is None:
                derived(root)
            return
    raise AssertionError(store_rows(root, "SELECT path, operation FROM store_projection_queue"))


def owned_files(root):
    """Every file under ``root`` that the vault store owns, with its bytes."""
    found = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_file() and ownership.classify(rel) is not ownership.Ownership.NONE:
            found[rel] = path.read_bytes()
    return found


def table_differences(store_root, file_root, scratch):
    """Every derived table of the store twin that differs from what the file twin's files derive.

    A projection writes its file after the commit that stored the page, so a page's modification
    time is never the file twin's. The derived copy takes the stored time first, as the rehearsal
    driver does, and only that column is set aside.
    """
    expected = scratch / "expected.sqlite"
    verify.derive_expected(file_root, expected)
    with closing(sqlite3.connect(connection.store_path(store_root))) as store:
        stored_times = dict(store.execute("SELECT path, mtime_ns FROM pages"))
        with closing(sqlite3.connect(expected)) as derived:
            derived.executemany(
                "UPDATE pages SET mtime_ns = ? WHERE path = ?",
                [(mtime, rel) for rel, mtime in stored_times.items()],
            )
            derived.commit()
        comparison = verify.compare_tables(store, expected)
    assert comparison.tables, "the comparison must cover tables for this check to mean anything"
    return {d.table: d.keys for d in comparison.differences}


def test_a_write_stream_leaves_the_store_twin_as_file_mode_leaves_its_vault(
    served, writer, tmp_path, monkeypatch
):
    """A store-mode writer whose files or derived rows differ from what file mode leaves after the
    same writes: a reader then sees another vault than the person's file-mode vault would be."""
    monkeypatch.setattr(verify, "KEYS_REPORTED", 50)  # name every differing key, not five
    twin = tmp_path / "file-twin"
    shutil.copytree(served, twin, ignore=shutil.ignore_patterns("_Collections"))
    build_indexes(twin)

    stream(twin)
    stream(served)
    settled(served, writer)

    assert owned_files(served) == owned_files(twin)
    assert table_differences(served, twin, tmp_path) == {}


def test_an_edit_of_a_compiled_note_leaves_no_row_that_file_mode_lacks(
    stored, service, tmp_path  # noqa: F811
):
    """Store-mode rows for a compiled-history entry, which file mode never catalogues because its
    walks yield Markdown alone: a search can then return the entry where file mode returns the note.

    History entries are named by the transition's time and id, so no file twin can match their
    paths; the store's rows are compared with what file mode derives from the store's own files.
    """
    created = call(
        stored, "remember", title="Tide gauge",
        content="# Tide gauge\n\n## Claim\n\nThe gauge reads two metres at noon.\n",
        relation_disposition="reviewed_none", relation_review_reason="a gauge reading stands alone",
    )
    call(
        stored, "edit_memory", path=created["path"], why="the gauge was recalibrated",
        operation={
            "kind": "replace_string", "old_string": "two metres", "new_string": "three metres",
            "relation_disposition": "reviewed_none",
            "relation_review_reason": "a gauge reading stands alone",
        },
    )
    history = f"{compiled_history.history_root()}/%"
    assert store_rows(stored, "SELECT path FROM store_pages WHERE path LIKE ?", history), (
        "the edit must keep a history entry for this check to mean anything"
    )
    settled(stored, None)

    assert table_differences(stored, stored, tmp_path) == {}


def test_a_second_capture_of_the_same_text_reuses_the_first(stored, service):  # noqa: F811
    """A store-mode capture that misses the duplicate that file mode finds, and stores the text as
    a second Source. The first Source has no file yet (no projector runs here), so the lookup must
    answer from the store's derived payload hash."""
    text = "The spring tide peaks an hour after noon at the north quay."

    origin = {"source_kind": "article", "url": "https://tides.example/north-quay"}
    first = call(stored, "capture_source", title="Spring tide", content=text, **origin)
    assert not (stored / first["path"]).exists(), (
        "the first Source must have no file yet for this check to mean anything"
    )
    second = call(stored, "capture_source", title="Spring tide, again", content=text, **origin)

    assert second.get("capture_outcome") == "reused", second
    # The door names a fresh Source by `path` and a reused one under `source`.
    assert second["source"]["path"] == first["path"]
