"""A writer that bypasses ``batch_atomic_write`` commits to the store in a store-mode vault.

OpenSpec make-vault-store-canonical task 2.2. The vault is the migration suite's generated vault in
store mode, and the writer is bound as the store thread binds it (``require_servable`` still refuses
a store-mode service start, so no service holds the store here). The projector is off unless a test
runs its pass by hand: nothing writes a stored page to disk, so each answer comes from the store.
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import ALPHA, BETA, vault  # noqa: F401
from test_vault_store_projector import projecting  # noqa: F401
from test_vault_store_read_write import stored, writer  # noqa: F401
from test_vault_store_write_path import (
    bm25_paths,
    edge_targets,
    planned,
    revision,
    served,  # noqa: F401
    store_rows,
    tree,
)

from exomem import delete_file, vault as vault_module
from exomem.collection_store import connection
from exomem.vault_store import availability, batch, read, verify, write

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="The offline-owner lease is Linux-only"
)

GAMMA = "Knowledge Base/Notes/Gamma.md"
GAMMA_TEXT = (
    "---\ntitle: Gamma\ntype: note\n---\n# Gamma\n\nThe harbour master keeps the tide ledger.\n"
)


def store_gamma(root):
    """A page that only the store holds: it has no file until the projector writes one."""
    vault_module.batch_atomic_write(
        [planned(root, GAMMA, GAMMA_TEXT, create_only=True)],
        vault_root=root,
        post_commit_fanout=False,
    )


def stored_bytes(root, rel):
    return read.owned_bytes(root, rel)


def new_files(before, root):
    """The files that appeared on disk since ``before``, and any file whose bytes changed."""
    after = tree(root)
    return {path.relative_to(root).as_posix() for path in after if before.get(path) != after[path]}


def test_delete_moves_a_stored_page_to_the_trash_with_its_rows_and_writes_no_markdown(served):
    """A page deleted in store mode that leaves rows, or Markdown bytes, behind at its old path."""
    store_gamma(served)
    assert bm25_paths(served, "harbour") == [GAMMA]
    before = tree(served)

    result = delete_file.delete_file(served, path=GAMMA, confirm=True)

    assert not store_rows(served, "SELECT 1 FROM store_pages WHERE path = ?", GAMMA)
    assert stored_bytes(served, result.trash_path) == GAMMA_TEXT.encode()
    assert bm25_paths(served, "harbour") == []
    assert edge_targets(served, GAMMA) == set()
    # The only file that appears is the metadata sidecar, which is a binary and goes to disk first.
    assert new_files(before, served) == {result.trash_meta_path}
    assert result.index["components"], "the report names the row work that the commit did"
    assert not (served / result.trash_path).exists()


def test_delete_of_a_page_that_a_competing_commit_changed_refuses_and_leaves_no_sidecar(
    served, writer, monkeypatch  # noqa: F811
):
    """A delete that wins a race against an edit and trashes bytes that it never read."""
    store_gamma(served)
    original_check = batch._check_held
    raced = []

    def check_then_race(plan):
        original_check(plan)
        if not raced:
            raced.append(1)
            write.commit(
                writer,
                write.StoreChange(
                    write.Origin.GOVERNED, "race", puts=(write.PagePut(GAMMA, b"# Gamma, raced\n"),)
                ),
            )

    monkeypatch.setattr(batch, "_check_held", check_then_race)
    before = tree(served)
    start = revision(served)

    with pytest.raises(delete_file.DeleteFileError) as refused:
        delete_file.delete_file(served, path=GAMMA, confirm=True)

    assert refused.value.code == "STALE_DELETE"
    assert revision(served) == start + 1, "only the competing commit may have committed"
    assert stored_bytes(served, GAMMA) == b"# Gamma, raced\n"
    assert tree(served) == before, "the sidecar published for the refused commit must be rolled back"
