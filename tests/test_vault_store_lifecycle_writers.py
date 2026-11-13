"""The lifecycle writers move, recover and trash pages through the store in a store-mode vault.

OpenSpec make-vault-store-canonical task 2.5. The vault is the migration suite's generated vault in
store mode, with the writer bound as the store thread binds it. The projector is off unless a test
runs its pass by hand, so a page that a commit stores has no file and every answer comes from the
store.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from contextlib import closing

import pytest
import test_vault_store_read_write as read_write
import test_vault_store_write_path as write_path
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_bypass_writers import (
    DELTA,
    GAMMA,
    GAMMA_TEXT,
    gamma_rows,
    govern,
    new_files,
    store_delta,
    store_gamma,
    unimported_note,
)
from test_vault_store_migration import vault  # noqa: F401
from test_vault_store_write_path import planned, revision, store_rows, tree

from exomem import delete_directory, delete_file, move_file, recover_from_trash, semantic_contract
from exomem import vault as vault_module
from exomem.cli_ops import OpError
from exomem.collection_store import connection
from exomem.governance import receipts
from exomem.governance.principal import (
    library_scope,
    most_restrictive_principal,
    request_scope,
)
from exomem.vault_store import (
    availability,
    batch,
    importer,
    projection,
    projector,
    read,
    verify,
    write,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="The offline-owner lease is Linux-only"
)

# The fixtures of the suites that build this vault, re-exported by assignment: a test that takes one
# is then no redefinition of an import.
stored, writer, served = read_write.stored, read_write.writer, write_path.served

MOVED = "Knowledge Base/Notes/Gamma Moved.md"
EPSILON = "Knowledge Base/Notes/Epsilon.md"
EPSILON_TEXT = "---\ntitle: Epsilon\ntype: note\n---\n# Epsilon\n\nThe ledger is in [[Gamma]].\n"


def race_after_check(monkeypatch, writer, change):
    """Commit ``change`` once, right after the batch checked what it holds: a competing writer."""
    original_check = batch._check_held
    raced = []

    def check_then_race(plan):
        original_check(plan)
        if not raced:
            raced.append(1)
            write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "race", **change))

    monkeypatch.setattr(batch, "_check_held", check_then_race)


def project_by_hand(root):
    """The projector's step as the write-path suite runs it: each stored page becomes its file."""
    with read.snapshot(root) as view:
        for meta in view.pages():
            target = projection.write_file(
                root, meta.path, view.page(meta.path).body, durable=False
            )
            os.utime(target, ns=(meta.mtime_ns, meta.mtime_ns))


def differences_from_a_cold_build(root, tmp_path):
    """Rows that differ from what file mode derives from the same files, outside one key table."""
    project_by_hand(root)
    expected = tmp_path / "expected.sqlite"
    verify.derive_expected(root, expected)
    with closing(sqlite3.connect(connection.store_path(root))) as store:
        comparison = verify.compare_tables(store, expected)
    assert comparison.tables, "the comparison must cover tables for this check to mean anything"
    # A trigger stamps a revision key for a parent whose page rows go, in file mode as here; a cold
    # build creates none, so that one key-only table is the only place the two may differ.
    return [d for d in comparison.differences if d.table != "unit_revisions"]


def test_a_move_carries_the_rows_and_rewrites_a_stored_linker_without_writing_markdown(served):
    """A move that leaves rows at the old path, misses a linker with no file, or writes Markdown."""
    store_gamma(served)
    store_delta(served)
    rows_before = gamma_rows(served)
    before = tree(served)

    result = move_file.move_file(served, old_path=GAMMA, new_path=MOVED)

    assert result.files_touched == [DELTA]
    assert read.owned_bytes(served, MOVED) == GAMMA_TEXT.encode()
    assert not read.page_exists(served, GAMMA)
    assert b"[[Gamma Moved]]" in read.owned_bytes(served, DELTA)
    assert store_rows(served, "SELECT 1 FROM pages WHERE path = ?", GAMMA) == []
    moved_rows = store_rows(served, "SELECT 1 FROM pages WHERE path = ?", MOVED)
    assert len(moved_rows) == len(rows_before["pages"])
    assert store_rows(
        served,
        "SELECT DISTINCT source_path FROM graph_edges WHERE dst_page_key = ?",
        f"file:{MOVED}",
    ) == [(DELTA,)]
    assert new_files(before, served) == set(), "the projector, not the move, writes files"
    assert result.index["upsert_reports"][0]["components"], "the report names the commit's rows"
    assert result.index["delete_report"]["requested_paths"] == [GAMMA]


def test_a_move_of_a_page_that_a_competing_commit_changed_refuses_and_commits_nothing(
    served, writer, monkeypatch
):
    """A move that wins a race against an edit and carries bytes that it never read."""
    store_gamma(served)
    race_after_check(monkeypatch, writer, {"puts": (write.PagePut(GAMMA, b"# Gamma, raced\n"),)})
    before = tree(served)
    start = revision(served)

    with pytest.raises(move_file.MoveFileError) as refused:
        move_file.move_file(served, old_path=GAMMA, new_path=MOVED)

    assert refused.value.code == "SOURCE_CHANGED"
    assert revision(served) == start + 1, "only the competing commit may have committed"
    assert read.owned_bytes(served, GAMMA) == b"# Gamma, raced\n"
    assert not read.page_exists(served, MOVED)
    assert tree(served) == before


def test_a_move_of_a_page_that_gains_a_link_after_the_scan_refuses_and_commits_nothing(
    served, writer, monkeypatch
):
    """A move that rewrote the links it scanned, then left a link that a commit then made dangling."""
    store_gamma(served)
    race_after_check(
        monkeypatch, writer, {"puts": (write.PagePut(EPSILON, EPSILON_TEXT.encode()),)}
    )
    start = revision(served)

    with pytest.raises(move_file.MoveFileError) as refused:
        move_file.move_file(served, old_path=GAMMA, new_path=MOVED)

    assert refused.value.code == "SOURCE_CHANGED"
    assert "a page now links to it" in refused.value.reason
    assert EPSILON not in refused.value.reason
    assert revision(served) == start + 1, "only the competing commit may have committed"
    assert read.owned_bytes(served, GAMMA) == GAMMA_TEXT.encode()
    assert not read.page_exists(served, MOVED)


def test_a_move_onto_a_page_that_a_competing_commit_created_refuses_and_keeps_both(
    served, writer, monkeypatch
):
    """A move that overwrites a page that appeared at its destination after it looked."""
    store_gamma(served)
    race_after_check(monkeypatch, writer, {"puts": (write.PagePut(MOVED, b"# Taken\n"),)})
    start = revision(served)

    with pytest.raises(move_file.MoveFileError) as refused:
        move_file.move_file(served, old_path=GAMMA, new_path=MOVED)

    assert refused.value.code == "DEST_EXISTS"
    assert revision(served) == start + 1, "only the competing commit may have committed"
    assert read.owned_bytes(served, MOVED) == b"# Taken\n"
    assert read.owned_bytes(served, GAMMA) == GAMMA_TEXT.encode()


def test_a_move_whose_bytes_change_stores_them_as_one_move_that_the_projector_renames(
    served, writer
):
    """A page whose self-link a move rewrites, stored with its old bytes, recorded as a delete and
    a new page (which ends the page's life), or left at its old file after the projector runs."""
    self_linked = GAMMA_TEXT + "\nSee also [[Gamma]].\n"
    vault_module.batch_atomic_write(
        [planned(served, GAMMA, self_linked, create_only=True)],
        vault_root=served,
        post_commit_fanout=False,
    )
    projecting = projector.Projector(served, run=lambda work: work(writer))
    projecting.run_pass()
    assert (served / GAMMA).read_bytes() == self_linked.encode()

    move_file.move_file(served, old_path=GAMMA, new_path=MOVED)

    moved = read.owned_bytes(served, MOVED)
    assert b"[[Gamma Moved]]" in moved and b"[[Gamma]]" not in moved
    assert store_rows(
        served,
        "SELECT operation, from_path, sha256 FROM store_audit WHERE path = ? ORDER BY revision DESC "
        "LIMIT 1",
        MOVED,
    ) == [("move", GAMMA, hashlib.sha256(moved).hexdigest())]
    report = projecting.run_pass()
    assert (report.held, report.deferred) == (0, {})
    assert not (served / GAMMA).exists()
    assert (served / MOVED).read_bytes() == moved


def test_a_projected_move_needs_nothing_from_the_importer(served, writer):
    """A move whose projection the importer then reads as an outside rename, delete or edit."""
    store_gamma(served)
    store_delta(served)
    projecting = projector.Projector(served, run=lambda work: work(writer))
    projecting.run_pass()

    move_file.move_file(served, old_path=GAMMA, new_path=MOVED)
    projecting.run_pass()
    swept = importer.Importer(served, run=lambda work: work(writer)).sweep()

    assert not (served / GAMMA).exists()
    assert (served / MOVED).read_bytes() == read.owned_bytes(served, MOVED)
    assert (served / DELTA).read_bytes() == read.owned_bytes(served, DELTA)
    assert (swept.imported, swept.moved, swept.removed, swept.held) == ([], {}, [], [])


def test_the_rows_of_a_move_equal_a_cold_derivation_from_the_files(served, tmp_path):
    """Rows that a store move commits and that differ from what file mode derives from the same
    vault: a graph edge still on the old path, or a search row that the move dropped."""
    store_gamma(served)
    store_delta(served)
    move_file.move_file(served, old_path=GAMMA, new_path=MOVED)

    assert differences_from_a_cold_build(served, tmp_path) == []


@pytest.mark.parametrize("removal", ["delete", "move"])
def test_a_typed_link_to_a_page_that_a_commit_removes_lands_as_a_cold_derivation_lands_it(
    served, tmp_path, removal
):
    """A commit that removes a page while its file waits for the projector, and re-derives a page
    that links to it against that file: the link stays a link to a file after the file goes."""
    claim = "Knowledge Base/Notes/Tide Claim.md"
    claim_text = (
        "---\ntitle: Tide Claim\ntype: note\n---\n# Tide Claim\n\n"
        "- supports [[Knowledge Base/Notes/Gamma]]\n"
    )
    store_gamma(served)
    vault_module.batch_atomic_write(
        [planned(served, claim, claim_text, create_only=True)],
        vault_root=served,
        post_commit_fanout=False,
    )
    project_by_hand(served)

    if removal == "delete":
        delete_file.delete_file(served, path=GAMMA, confirm=True, force_orphan=True)
    else:
        move_file.move_file(served, old_path=GAMMA, new_path=MOVED, update_wikilinks=False)
    assert (served / GAMMA).is_file(), "the removed page's file waits for the projector"
    (served / GAMMA).unlink()  # the projector's step for the page that the commit removed

    assert differences_from_a_cold_build(served, tmp_path) == []


def test_a_moves_store_reads_do_not_grow_with_its_linking_pages(served, monkeypatch):
    """A move that opens a store snapshot per linking page: its cost grows with the page's links,
    and its reads may mix revisions in one plan."""
    opened = []
    real = connection.store_reader

    def counting(*arguments, **keywords):
        opened.append(1)
        return real(*arguments, **keywords)

    def reads_for_one_move(linkers, source, destination):
        vault_module.batch_atomic_write(
            [planned(served, source, GAMMA_TEXT, create_only=True)]
            + [
                planned(
                    served,
                    f"Knowledge Base/Notes/{destination.rsplit('/', 1)[-1][:-3]} linker {n}.md",
                    f"# Linker {n}\n\nSee [[{source.rsplit('/', 1)[-1][:-3]}]].\n",
                    create_only=True,
                )
                for n in range(linkers)
            ],
            vault_root=served,
            post_commit_fanout=False,
        )
        opened.clear()
        with monkeypatch.context() as patched:
            patched.setattr(connection, "store_reader", counting)
            result = move_file.move_file(served, old_path=source, new_path=destination)
        assert len(result.files_touched) == linkers
        return len(opened)

    few = reads_for_one_move(2, "Knowledge Base/Notes/Few.md", "Knowledge Base/Notes/Few2.md")
    many = reads_for_one_move(30, "Knowledge Base/Notes/Many.md", "Knowledge Base/Notes/Many2.md")

    assert few == many


def test_the_corpus_holds_a_page_that_only_the_store_holds_and_sees_its_next_bytes(served):
    """A semantic preflight that judges a move against the projection: it misses a page that has no
    file, or keeps the bytes of a page that a later commit changed."""
    store_gamma(served)
    first = semantic_contract.build_corpus_context(served)
    assert GAMMA in first.pages
    assert not (served / GAMMA).exists()

    vault_module.batch_atomic_write(
        [planned(served, GAMMA, GAMMA_TEXT.replace("harbour", "river"))],
        vault_root=served,
        post_commit_fanout=False,
    )
    second = semantic_contract.build_corpus_context(served)

    assert second.pages[GAMMA].source_hash != first.pages[GAMMA].source_hash
    assert second.pages[GAMMA].source_hash == vault_module.content_hash(
        read.owned_bytes(served, GAMMA).decode("utf-8")
    )


def test_a_validated_move_names_the_hashes_that_its_commit_then_writes(served):
    """A validation that reads a projection (no file: it fails; a stale file: wrong hashes), so a
    caller that binds a plan to it commits other bytes than it validated."""
    store_gamma(served)
    store_delta(served)
    start = revision(served)

    validation = move_file.move_file(served, old_path=GAMMA, new_path=MOVED, validate_only=True)

    assert revision(served) == start, "a validation commits nothing"
    move_file.move_file(served, old_path=GAMMA, new_path=MOVED)
    after = {entry["path"]: entry.get("content_hash") for entry in validation.after}
    assert after[DELTA] == hashlib.sha256(read.owned_bytes(served, DELTA)).hexdigest()
    renamed = {entry["path"]: entry for entry in validation.rename_after}
    assert (
        renamed[MOVED]["content_hash"]
        == hashlib.sha256(read.owned_bytes(served, MOVED)).hexdigest()
    )
    assert renamed[GAMMA] == {"path": GAMMA, "absent": True}


def test_a_governed_page_moves_through_the_store_with_no_tombstone_or_receipt(served):
    """A move of a governed page that refuses as the vault-wide policy check did, or that writes
    the tombstone and receipt that only a trash or a recovery writes, in file mode as here."""
    store_gamma(served)
    govern(served, GAMMA)
    start = revision(served)

    move_file.move_file(served, old_path=GAMMA, new_path=MOVED, validate_only=True)
    assert revision(served) == start
    move_file.move_file(served, old_path=GAMMA, new_path=MOVED)

    assert read.owned_bytes(served, MOVED) == GAMMA_TEXT.encode()
    assert not store_rows(served, "SELECT 1 FROM store_pages WHERE path = ?", GAMMA)
    assert not list((served / "Knowledge Base/_Governance").rglob("*.json"))
    assert receipts.event_records(served) == []


@pytest.mark.parametrize(
    ("caller", "warnings"),
    [
        (
            library_scope,
            ["1 file(s) that the store has not imported yet keep their link to the old path"],
        ),
        (lambda: request_scope(most_restrictive_principal(surface="rest")), []),
    ],
    ids=["owner", "restricted"],
)
def test_only_the_owner_hears_that_an_unimported_note_keeps_its_link_after_a_move(
    served, caller, warnings
):
    """A move that hides from the owner a link it left pointing at the old path, or tells a
    restricted mover about a file that no policy covers yet."""
    store_gamma(served)
    unimported_note(served)

    with caller():
        result = move_file.move_file(served, old_path=GAMMA, new_path=MOVED)

    assert result.warnings == warnings
    mine = served / "Knowledge Base/Notes/Mine.md"
    assert "[[Gamma]]" in mine.read_text(encoding="utf-8"), "the move rewrites no file"


def test_a_recovery_after_a_delete_restores_the_page_and_its_rows_and_drops_the_sidecar(
    served, writer
):
    """A recovery that loses the page's rows, leaves the sidecar's record for the importer to find
    as a file with no page, or that validates other semantics than it then commits."""
    store_gamma(served)
    rows_before = gamma_rows(served)
    trashed = delete_file.delete_file(served, path=GAMMA, confirm=True)
    assert read.page_exists(served, trashed.trash_path)
    before = tree(served)

    validation = recover_from_trash.recover_from_trash(
        served, trash_path=trashed.trash_path, validate_only=True
    )
    assert tree(served) == before and read.page_exists(served, trashed.trash_path)
    result = recover_from_trash.recover_from_trash(served, trash_path=trashed.trash_path)

    assert result.restored_path == GAMMA
    assert read.owned_bytes(served, GAMMA) == GAMMA_TEXT.encode()
    assert not read.page_exists(served, trashed.trash_path)
    assert gamma_rows(served) == rows_before
    assert store_rows(
        served, "SELECT 1 FROM store_binaries WHERE path = ?", trashed.trash_meta_path
    ) == []
    assert {**result.semantic, "mutated": False} == validation.semantic
    assert new_files(before, served) == set(), "the projector, not the recovery, changes files"
    report = projector.Projector(served, run=lambda work: work(writer)).run_pass()
    assert (report.held, report.deferred) == (0, {})
    assert not (served / trashed.trash_meta_path).exists()
    assert (served / GAMMA).read_bytes() == GAMMA_TEXT.encode()


def test_a_recovery_of_a_trashed_page_that_a_competing_commit_changed_refuses_and_keeps_it(
    served, writer, monkeypatch
):
    """A recovery that restores bytes it never read, or that half-applies when the store refuses."""
    store_gamma(served)
    trashed = delete_file.delete_file(served, path=GAMMA, confirm=True)
    race_after_check(
        monkeypatch, writer, {"puts": (write.PagePut(trashed.trash_path, b"# Gamma, raced\n"),)}
    )
    start = revision(served)

    with pytest.raises(recover_from_trash.RecoverError) as refused:
        recover_from_trash.recover_from_trash(served, trash_path=trashed.trash_path)

    assert refused.value.code == "PATH_GUARD_CHANGED"
    assert revision(served) == start + 1, "only the competing commit may have committed"
    assert read.owned_bytes(served, trashed.trash_path) == b"# Gamma, raced\n"
    assert not read.page_exists(served, GAMMA)
    assert store_rows(
        served, "SELECT 1 FROM store_binaries WHERE path = ?", trashed.trash_meta_path
    ) != [], "the sidecar's record stays with the entry it describes"


def test_a_recovery_onto_a_page_that_now_exists_refuses_and_keeps_both(served):
    """A recovery that overwrites the page that took the trashed page's place."""
    store_gamma(served)
    trashed = delete_file.delete_file(served, path=GAMMA, confirm=True)
    store_gamma(served)
    start = revision(served)

    with pytest.raises(recover_from_trash.RecoverError) as refused:
        recover_from_trash.recover_from_trash(served, trash_path=trashed.trash_path)

    assert refused.value.code == "DEST_EXISTS"
    assert revision(served) == start
    assert read.page_exists(served, trashed.trash_path)


def test_a_recovery_to_a_path_the_store_does_not_own_refuses_in_validation_and_commits_nothing(
    served,
):
    """A recovery that validates a destination outside the store, so a caller plans on a recovery
    that its commit then refuses."""
    store_gamma(served)
    trashed = delete_file.delete_file(served, path=GAMMA, confirm=True)
    start = revision(served)

    for validate_only in (True, False):
        with pytest.raises(OpError) as refused:
            recover_from_trash.recover_from_trash(
                served,
                trash_path=trashed.trash_path,
                restore_path="Inbox/Gamma.md",
                validate_only=validate_only,
            )
        assert refused.value.code == availability.WRITER_UNROUTED, validate_only

    assert revision(served) == start
    assert read.page_exists(served, trashed.trash_path)
    assert not (served / "Inbox/Gamma.md").exists()


HARBOUR = "Knowledge Base/Projects/Harbour"
#: The folder size of the folder-trash cases; the latency drill sets it to 200.
FOLDER_PAGES = int(os.environ.get("EXOMEM_LIFECYCLE_DRILL_PAGES", "40"))


def store_folder(root, pages, folder=HARBOUR):
    """A folder of stored pages with no files, each linking to the first one inside the folder."""
    paths = [f"{folder}/Log {n:03d}.md" for n in range(pages)]
    vault_module.batch_atomic_write(
        [
            planned(
                root,
                path,
                f"---\ntitle: Log {n:03d}\ntype: note\n---\n# Log {n:03d}\n\n"
                f"Tide reading {n}, after [[Log 000]].\n",
                create_only=True,
            )
            for n, path in enumerate(paths)
        ],
        vault_root=root,
        post_commit_fanout=False,
    )
    return paths


def store_outside_linker(root):
    """A stored page outside the folder that links to one page in it."""
    vault_module.batch_atomic_write(
        [planned(root, EPSILON, EPSILON_TEXT.replace("[[Gamma]]", "[[Log 001]]"), create_only=True)],
        vault_root=root,
        post_commit_fanout=False,
    )


def folder_rows(root, folder):
    """Every derived row of the folder's pages, by the column that names the page in each table."""
    below = f"{folder}/%"
    return {
        table: Counter(store_rows(root, f"SELECT * FROM {table} WHERE {column} LIKE ?", below))
        for table, column in (("pages", "path"), ("graph_edges", "source_path"), ("identities", "path"))
    }


def test_a_folder_trash_moves_every_page_under_one_trash_folder_once_its_outside_link_is_waived(
    served,
):
    """A folder trash that counts a link between its own pages, misses the link from outside it,
    leaves a page or its rows at the old paths, or writes Markdown."""
    pages = store_folder(served, FOLDER_PAGES)
    store_outside_linker(served)
    before = tree(served)
    start = revision(served)

    with pytest.raises(delete_directory.DeleteDirectoryError) as refused:
        delete_directory.delete_directory(served, path=HARBOUR, confirm=True, recursive=True)
    assert refused.value.code == "INBOUND_LINKS"
    assert "1 external inbound" in refused.value.reason
    assert f"{EPSILON}:" in refused.value.reason and "Log 001.md" in refused.value.reason
    assert revision(served) == start and tree(served) == before

    result = delete_directory.delete_directory(
        served, path=HARBOUR, confirm=True, recursive=True, force_orphan=True
    )

    assert (result.file_count, result.inbound_link_count) == (len(pages), 1)
    trashed = [f"{result.trash_path}/{page[len(HARBOUR) + 1:]}" for page in pages]
    with read.snapshot(served) as view:
        assert view.held_pages(trashed) == frozenset(trashed)
        assert view.held_pages(pages) == frozenset()
    assert folder_rows(served, HARBOUR) == {
        "pages": Counter(), "graph_edges": Counter(), "identities": Counter()
    }
    assert new_files(before, served) == {result.trash_meta_path}, "the projector moves the pages"
    sidecar = json.loads((served / result.trash_meta_path).read_text(encoding="utf-8"))
    assert (sidecar["original_path"], sidecar["file_count_at_trash"]) == (HARBOUR, len(pages))
    assert result.index["components"], "the report names the row work that the commit did"


def test_a_trashed_folder_recovers_with_every_page_and_its_rows(served):
    """A folder round trip that loses a page or its rows, or leaves the sidecar's record for the
    importer to find as a file with no entry."""
    pages = store_folder(served, 5)
    rows_before = folder_rows(served, HARBOUR)

    trashed = delete_directory.delete_directory(served, path=HARBOUR, confirm=True, recursive=True)
    recovered = recover_from_trash.recover_from_trash(served, trash_path=trashed.trash_path)

    assert (recovered.kind, recovered.restored_path) == ("directory", HARBOUR)
    with read.snapshot(served) as view:
        assert view.held_pages(pages) == frozenset(pages)
        assert next(view.pages(f"{trashed.trash_path}/"), None) is None
    assert folder_rows(served, HARBOUR) == rows_before
    assert store_rows(
        served, "SELECT 1 FROM store_binaries WHERE path = ?", trashed.trash_meta_path
    ) == []


def test_a_projected_folder_trash_and_its_recovery_need_nothing_from_the_importer(served, writer):
    """A folder trash or recovery whose projection the importer then reads as outside renames,
    deletes or edits, or that leaves a page's file in the trash."""
    pages = store_folder(served, 5)
    projecting = projector.Projector(served, run=lambda work: work(writer))
    sweeping = importer.Importer(served, run=lambda work: work(writer))
    projecting.run_pass()

    trashed = delete_directory.delete_directory(served, path=HARBOUR, confirm=True, recursive=True)
    projecting.run_pass()
    swept_after_trash = sweeping.sweep()
    in_trash = [f"{trashed.trash_path}/{page[len(HARBOUR) + 1:]}" for page in pages]
    assert not any((served / page).exists() for page in pages)
    assert all((served / page).read_bytes() == read.owned_bytes(served, page) for page in in_trash)

    recover_from_trash.recover_from_trash(served, trash_path=trashed.trash_path)
    projecting.run_pass()
    swept_after_recovery = sweeping.sweep()

    assert all((served / page).read_bytes() == read.owned_bytes(served, page) for page in pages)
    assert not any((served / page).exists() for page in in_trash)
    assert not (served / trashed.trash_meta_path).exists()
    for swept in (swept_after_trash, swept_after_recovery):
        assert (swept.imported, swept.moved, swept.removed, swept.held) == ([], {}, [], [])


def test_a_folder_trash_where_a_page_changed_after_the_check_refuses_and_commits_nothing(
    served, writer, monkeypatch
):
    """A folder trash that half-applies, or that trashes bytes nobody read, when one page loses a
    race against an edit."""
    pages = store_folder(served, 3)
    race_after_check(monkeypatch, writer, {"puts": (write.PagePut(pages[1], b"# Log, raced\n"),)})
    before = tree(served)
    start = revision(served)

    with pytest.raises(delete_directory.DeleteDirectoryError) as refused:
        delete_directory.delete_directory(served, path=HARBOUR, confirm=True, recursive=True)

    assert refused.value.code == "PATH_GUARD_CHANGED"
    assert revision(served) == start + 1, "only the competing commit may have committed"
    with read.snapshot(served) as view:
        assert view.held_pages(pages) == frozenset(pages)
    assert tree(served) == before, "the refused commit must roll back the sidecar it published"


def test_a_folder_above_the_trash_cap_refuses_and_commits_nothing(served, monkeypatch):
    """A folder trash whose single commit grows without a bound."""
    monkeypatch.setattr(delete_directory, "DIRECTORY_TRANSITION_LIMIT", 3)
    store_folder(served, 4)
    start = revision(served)

    with pytest.raises(delete_directory.DeleteDirectoryError) as refused:
        delete_directory.delete_directory(served, path=HARBOUR, confirm=True, recursive=True)

    assert refused.value.code == "PATH_GUARD_LIMIT"
    assert revision(served) == start


def test_a_folder_with_an_unimported_note_refuses_by_name_and_keeps_it(served):
    """A folder trash that moves the pages and leaves behind a note that nothing has imported,
    while it reports the folder trashed."""
    store_folder(served, 2)
    draft = served / HARBOUR / "Draft.md"
    draft.parent.mkdir(parents=True)
    draft.write_text("# Draft\n\nNot imported yet.\n", encoding="utf-8")
    before = tree(served)
    start = revision(served)

    with pytest.raises(OpError) as refused:
        delete_directory.delete_directory(served, path=HARBOUR, confirm=True, recursive=True)
    assert refused.value.code == availability.WRITER_UNROUTED
    assert "delete_directory" in refused.value.message

    assert revision(served) == start
    assert tree(served) == before


def test_the_rows_of_a_folder_trash_equal_a_cold_derivation_from_the_files(served, tmp_path):
    """Rows that a folder trash commits and that differ from what file mode derives from the same
    vault: an edge into the old folder, or a search row left at an old path."""
    store_folder(served, 5)
    store_outside_linker(served)
    delete_directory.delete_directory(
        served, path=HARBOUR, confirm=True, recursive=True, force_orphan=True
    )

    assert differences_from_a_cold_build(served, tmp_path) == []


def test_a_folder_trashs_store_reads_do_not_grow_with_its_pages(served, monkeypatch):
    """A folder trash that opens a store snapshot per page: its cost grows with the folder, and its
    reads may mix revisions in one plan."""
    opened = []
    real = connection.store_reader

    def counting(*arguments, **keywords):
        opened.append(1)
        return real(*arguments, **keywords)

    def reads_for_one_trash(folder, pages):
        store_folder(served, pages, folder=folder)
        opened.clear()
        with monkeypatch.context() as patched:
            patched.setattr(connection, "store_reader", counting)
            result = delete_directory.delete_directory(
                served, path=folder, confirm=True, recursive=True
            )
        assert result.file_count == pages
        return len(opened)

    few = reads_for_one_trash("Knowledge Base/Projects/Few", 2)
    many = reads_for_one_trash("Knowledge Base/Projects/Many", 30)

    assert few == many
