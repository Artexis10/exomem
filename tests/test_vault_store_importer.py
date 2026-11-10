"""Store mode's importer: a change made outside Exomem reaches the store, or is held, and no
person's bytes are lost.

OpenSpec make-vault-store-canonical tasks 4.2 and 4.3. The vault is the migration suite's
generated vault, moved into store mode by ``store-mode migrate``. The importer reads real files,
commits through the real ``write.commit`` on a real store writer, and the projector runs its real
passes. Each outcome is read back from the store's rows, never from the importer's own report.
"""

from __future__ import annotations

import hashlib
import itertools
import multiprocessing
import os
import queue
import signal
import sqlite3
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest
import test_managed_collections_migration as managed_suite
import test_vault_store_migration as migration_suite
import test_vault_store_projector as projector_suite
from test_vault_store_migration import ALPHA, BETA, DIAGRAM, DIAGRAM_BYTES, migrate_apply
from test_vault_store_projector import NOTES, commit, put, rows

from exomem import file_watcher, server_runtime
from exomem.cli_ops import OpError
from exomem.collection_store import connection, preview
from exomem.collection_store.writer import CollectionWriter
from exomem.vault_store import importer, ownership, projector, read, verify, write

# The generated vault and the store fixtures are the sibling suites' own: pytest finds a fixture by
# its name in this module, and an alias keeps a test's argument from shadowing an import.
coordinator_url = managed_suite.coordinator_url
vault = migration_suite.vault
stored = projector_suite.stored
writer = projector_suite.writer
projecting = projector_suite.projecting

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="The offline-owner lease is Linux-only"
)

GENERATED = f"{NOTES}/Generated"
PERSON = b"A person edited this page in an outside editor.\n"
LATER = b"The same person saved this page again.\n"
STORE_BYTES = b"---\ntitle: Alpha\ntype: note\n---\n# Alpha\n\nBytes that only the store holds.\n"
UNOWNED = "Knowledge Base/Records/Work/_collection.md"


def sha(body):
    return hashlib.sha256(body).hexdigest()


def generated(number):
    return f"{GENERATED}/Page{number:02d}.md"


def generated_bytes(number):
    return (
        f"---\ntitle: Page {number}\ntype: note\n---\n# Page {number}\n\n"
        f"The lighthouse keeper logs entry {number} in the tide table.\n"
    ).encode()


@pytest.fixture
def importing(stored, writer):
    """An importer whose store work runs on this thread's writer, as the store thread runs it."""
    return importer.Importer(stored, run=lambda work: work(writer))


def revision(root):
    return read.current_revision(root)


def imports(root, rel=None):
    """The audit rows that record an import, each with the origin that the store wrote."""
    where, arguments = ("", ()) if rel is None else (" AND path = ?", (rel,))
    return rows(
        root,
        f"SELECT path, origin FROM store_audit WHERE operation = 'import'{where} ORDER BY seq",
        *arguments,
    )


# -- the rehearsal ---------------------------------------------------------------------------------

KINDS = ("append", "inline", "property", "crlf")


def edited(kind, text, token):
    """The page as an editor would save it: appended, edited in a line, a new property, CRLF."""
    marker = f"\n- [observation] Rehearsal edit {token}\n"
    if kind == "append":
        return text.rstrip("\n") + "\n" + marker
    if kind == "inline":
        at = text.index("\n---\n", 4) + 5
        end = text.index("\n", at)
        return text[:end] + f" {token}" + text[end:]
    if kind == "property":
        return text[:4] + f"rehearsal: {token}\n" + text[4:]
    return (text.rstrip("\n") + "\n" + marker).replace("\n", "\r\n")


def outcome(root, rel, since):
    """What the store's rows say became of ``rel`` after revision ``since``."""
    held = rows(root, "SELECT kind FROM store_held WHERE path = ?", rel)
    if held:
        return f"held {held[0][0]}"
    operations = {
        operation
        for (operation,) in rows(
            root,
            "SELECT operation FROM store_audit WHERE path = ? AND revision > ?",
            rel,
            since,
        )
    }
    for name in ("move", "import"):
        if name in operations:
            return {"move": "moved", "import": "imported"}[name]
    return "unchanged"


def test_the_rehearsal_of_28_outside_edits_ends_as_the_design_promises(
    stored, writer, projecting, importing
):
    """An importer that overwrites a person's bytes, imports an edit that raced a store write,
    misses a delete, a rename or a new file, or relies on size and mtime to see a change."""
    pool = [generated(number) for number in range(30)]
    commit(writer, puts=tuple(write.PagePut(rel, generated_bytes(n)) for n, rel in enumerate(pool)))
    assert projecting.run_pass().projected == 30
    base = revision(stored)
    fresh = iter(pool)
    cases = []  # (kind, path, expected outcome)
    events, moves = [], {}
    tokens = itertools.count(1)

    def person_writes(rel, body):
        (stored / rel).write_bytes(body)

    for number in range(16):  # clean edits
        kind, rel = KINDS[number % 4], next(fresh)
        text = (stored / rel).read_text(encoding="utf-8")
        person_writes(rel, edited(kind, text, f"zqreh{next(tokens):04d}").encode())
        events.append(rel)
        cases.append((kind, rel, "imported"))
    for _ in range(2):  # touched, bytes unchanged
        rel = next(fresh)
        os.utime(stored / rel)
        events.append(rel)
        cases.append(("touch", rel, "unchanged"))
    same_size = []
    for _ in range(2):  # an edit that keeps size and mtime, and whose event never arrives
        rel = next(fresh)
        before = (stored / rel).stat()
        body = bytearray((stored / rel).read_bytes())
        at = body.index(b"keeper")
        body[at : at + 6] = b"sleeper"[:6]
        person_writes(rel, bytes(body))
        os.utime(stored / rel, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert (stored / rel).stat().st_size == before.st_size
        same_size.append(rel)
        cases.append(("same size and mtime", rel, "imported by the sweep only"))
    for _ in range(2):  # a store write is queued, the projector has not run, a person edits
        rel = next(fresh)
        commit(writer, **put(rel, generated_bytes(99)))
        person_writes(rel, PERSON + rel.encode())
        events.append(rel)
        cases.append(("edit against a queued store write", rel, "held edit"))
    for _ in range(2):
        rel = next(fresh)
        (stored / rel).unlink()
        events.append(rel)
        cases.append(("delete", rel, "held delete"))
    renamed = []
    for hinted in (True, False):
        old = next(fresh)
        new = f"{GENERATED}/Renamed-{Path(old).name}"
        (stored / old).rename(stored / new)
        events.extend((old, new))
        if hinted:
            moves[new] = old
        renamed.append((old, new))
        cases.append(
            ("rename" + (" with a move event" if hinted else " paired by hash"), new, "moved")
        )
    create = f"{NOTES}/Insights/outside-create.md"
    (stored / create).parent.mkdir(exist_ok=True)
    person_writes(create, b"# Created outside\n\nA new page that a person saved.\n")
    events.append(create)
    cases.append(("new file", create, "imported"))

    importing.import_paths(events, moves)

    # The projector now runs, and then a person edits a path with a store write queued.
    racing = next(fresh)
    commit(writer, **put(racing, generated_bytes(98)))
    person_writes(racing, PERSON + racing.encode())
    projecting.run_pass()
    importing.import_paths([racing])
    cases.append(("edit against a queued store write, projector running", racing, "held edit"))

    pending = {rel: outcome(stored, rel, base) for rel in same_size}
    swept = importing.sweep()
    pages_before_pass = {
        rel: (stored / rel).read_bytes() for _, rel, _ in cases if (stored / rel).exists()
    }
    projecting.run_pass()

    table = []
    for kind, rel, expected in cases:
        actual = outcome(stored, rel, base)
        if kind == "same size and mtime":
            actual = (
                "imported by the sweep only"
                if (pending[rel], actual) == ("unchanged", "imported")
                else actual
            )
        table.append((kind, expected, actual))
    print("\n28-edit rehearsal (case, expected, actual)")
    for kind, expected, actual in table:
        print(f"  {kind:<56} {expected:<28} {actual}")
    assert len(table) == 28
    assert [(k, e, a) for k, e, a in table if e != a] == []

    assert swept.imported == sorted(same_size) and swept.held == [] and not swept.moved
    for _, rel, expected in cases:
        if expected in ("imported", "imported by the sweep only"):
            assert rows(stored, "SELECT body FROM store_pages WHERE path = ?", rel) == [
                ((stored / rel).read_bytes(),)
            ]
            assert rows(stored, "SELECT sha256 FROM store_manifest WHERE path = ?", rel) == [
                (sha((stored / rel).read_bytes()),)
            ]
            assert imports(stored, rel) == [(rel, "outside editor")], "one import for each edit"
        elif expected == "held edit":
            held = rows(stored, "SELECT body, sha256 FROM store_held WHERE path = ?", rel)
            assert held == [(PERSON + rel.encode(), sha(PERSON + rel.encode()))]
            assert (stored / rel).read_bytes() == PERSON + rel.encode(), "no Markdown overwritten"
            assert rows(
                stored, "SELECT operation FROM store_projection_queue WHERE path = ?", rel
            ) == [("write",)]
        elif expected == "held delete":
            assert not (stored / rel).exists()
            assert len(rows(stored, "SELECT path FROM store_pages WHERE path = ?", rel)) == 1
        elif expected == "moved":
            old = dict((new, old) for old, new in renamed)[rel]
            assert rows(stored, "SELECT body FROM store_pages WHERE path = ?", rel) == [
                ((stored / rel).read_bytes(),)
            ]
            assert rows(stored, "SELECT path FROM store_pages WHERE path = ?", old) == []
    assert rows(stored, "SELECT COUNT(*) FROM store_held") == [(5,)]
    assert pages_before_pass == {rel: (stored / rel).read_bytes() for rel in pages_before_pass}, (
        "the projector's later pass overwrote or removed a file"
    )
    assert importing.sweep().settled == 0 and importing.import_paths(events, moves).settled == 0


# -- one behaviour each ----------------------------------------------------------------------------


def test_one_held_path_edited_twice_is_one_row_with_the_latest_bytes(
    stored, writer, importing, monkeypatch
):
    """An importer that adds a held row for each save, loses the first detection time, or rewrites
    a hold whose bytes did not change."""
    clock = itertools.count(1)
    monkeypatch.setattr(write, "timestamp", lambda: f"2026-11-{next(clock):02d}T00:00:00Z")
    commit(writer, **put(ALPHA, STORE_BYTES))

    (stored / ALPHA).write_bytes(PERSON)
    importing.import_paths([ALPHA])
    detected = rows(stored, "SELECT detected_at FROM store_held WHERE path = ?", ALPHA)[0][0]
    (stored / ALPHA).write_bytes(LATER)
    importing.import_paths([ALPHA])
    unchanged = importing.import_paths([ALPHA])

    assert rows(stored, "SELECT kind, body, sha256, detected_at FROM store_held") == [
        ("edit", LATER, sha(LATER), detected)
    ]
    assert (unchanged.held, unchanged.unchanged) == ([], 1), "a hold with the same bytes is left"
    assert imports(stored) == []
    assert (stored / ALPHA).read_bytes() == LATER


def test_replaying_exomems_own_projections_imports_nothing_and_holds_nothing(
    stored, writer, projecting, importing
):
    """An importer that takes Exomem's own projection for a person's edit: the file a pass wrote,
    one that a kill left before its manifest row, or older bytes of a revision still queued."""
    drifter = f"{NOTES}/Drifter.md"
    commit(writer, **put(drifter, b"# Drifter\n"))
    projecting.run_pass()
    gamma = f"{NOTES}/Gamma.md"
    commit(
        writer,
        puts=(write.PagePut(gamma, b"# Gamma\n"), write.PagePut(ALPHA, STORE_BYTES)),
        deletes=(write.PageDelete(BETA),),
        moves=(write.PageMove(drifter, f"{NOTES}/Archive/Drifter.md"),),
    )
    projecting.run_pass()
    touched = [gamma, ALPHA, BETA, drifter, f"{NOTES}/Archive/Drifter.md"]

    after_a_pass = importing.import_paths(touched)
    swept = importing.sweep()

    assert (after_a_pass.settled, swept.settled) == (0, 0)
    # A projection that died before its manifest row, and then a newer revision queued over it.
    commit(writer, **put(gamma, b"# Gamma\n\nVersion two.\n"))
    (stored / gamma).write_bytes(b"# Gamma\n\nVersion two.\n")
    commit(writer, **put(gamma, b"# Gamma\n\nVersion three.\n"))
    after_a_kill = importing.import_paths([gamma])

    assert after_a_kill.settled == 0
    assert rows(stored, "SELECT COUNT(*) FROM store_held") == [(0,)]
    assert imports(stored) == []


def test_a_killed_importer_leaves_no_duplicate_imports(stored):
    """An importer that imports a path twice after a restart, or that loses an edit that its
    killed run did not reach."""
    paths = [generated(number) for number in range(12)]
    root = stored
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as handle:
        writing = CollectionWriter(root, handle)
        commit(
            writing,
            puts=tuple(write.PagePut(rel, generated_bytes(n)) for n, rel in enumerate(paths)),
        )
        projector.Projector(root, run=lambda work: work(writing)).run_pass()
    for number, rel in enumerate(paths):
        (root / rel).write_bytes(generated_bytes(number) + b"A person added this line.\n")

    child = multiprocessing.get_context("spawn").Process(
        target=_import_and_die, args=(dict(os.environ), root, 5)
    )
    child.start()
    child.join(300)
    assert child.exitcode == -signal.SIGKILL, "the child must be killed inside the fifth import"
    done = {path for path, _ in imports(root)}
    assert len(done) == 4, "each import before the kill is durable on its own"

    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as handle:
        writing = CollectionWriter(root, handle)
        restarted = importer.Importer(root, run=lambda work: work(writing))
        first = restarted.sweep()
        second = restarted.sweep()

    assert sorted(first.imported) == sorted(set(paths) - done), (
        "the restart takes up where it stopped"
    )
    assert second.settled == 0
    assert sorted(path for path, _ in imports(root)) == sorted(paths), "one import for each path"
    for rel in paths:
        assert rows(root, "SELECT body FROM store_pages WHERE path = ?", rel) == [
            ((root / rel).read_bytes(),)
        ]
    assert rows(root, "SELECT COUNT(*) FROM store_held") == [(0,)]


def _import_and_die(environment, root, countdown):
    """One importer sweep in a fresh interpreter that the operating system kills partway through."""
    os.environ.clear()
    os.environ.update(environment)
    seen = set()
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as handle:

        def trace(statement):
            # SQLite reports a statement once per step, so the expanded text counts each audit row.
            if statement.startswith("INSERT INTO store_audit"):
                seen.add(statement)
                if len(seen) == countdown:
                    os.kill(os.getpid(), signal.SIGKILL)

        handle.connection.set_trace_callback(trace)
        importer.Importer(root, run=lambda work: work(CollectionWriter(root, handle))).sweep()
    os._exit(0)


def test_a_binary_that_changes_or_disappears_is_recorded_from_the_disk(stored, writer, importing):
    """An importer that forgets a changed binary's new hash, keeps the record of a binary that a
    person deleted, holds a delete that has no bytes to keep, or records a binary that a queued
    store delete is about to remove."""
    (stored / DIAGRAM).write_bytes(DIAGRAM_BYTES + b"edited")
    fresh = f"{NOTES}/new-diagram.bin"
    (stored / fresh).write_bytes(b"\x00\x01\x02")

    report = importing.import_paths([DIAGRAM, fresh])

    assert sorted(report.imported) == sorted([DIAGRAM, fresh])
    assert rows(stored, "SELECT sha256, size FROM store_binaries WHERE path = ?", DIAGRAM) == [
        (sha(DIAGRAM_BYTES + b"edited"), len(DIAGRAM_BYTES) + 6)
    ]
    assert imports(stored, fresh) == [(fresh, "outside editor")]

    (stored / DIAGRAM).unlink()
    gone = importing.import_paths([DIAGRAM])

    assert (gone.removed, gone.held) == ([DIAGRAM], [])
    assert rows(stored, "SELECT path FROM store_binaries WHERE path = ?", DIAGRAM) == []
    assert rows(stored, "SELECT COUNT(*) FROM store_held") == [(0,)]
    assert rows(
        stored, "SELECT operation FROM store_projection_queue WHERE path = ?", DIAGRAM
    ) == [], "a queued delete would remove the file that a person restores next"
    assert rows(
        stored, "SELECT origin FROM store_audit WHERE operation = 'delete' AND path = ?", DIAGRAM
    ) == [("outside editor",)]

    commit(writer, binary_deletes=(write.BinaryDelete(fresh),))
    (stored / fresh).write_bytes(b"\x09\x09")
    raced = importing.import_paths([fresh])
    assert raced.held == [fresh], "a changed file under a queued delete is held, not recorded"
    assert rows(stored, "SELECT kind, body, sha256 FROM store_held WHERE path = ?", fresh) == [
        ("edit", None, sha(b"\x09\x09"))
    ]
    assert (stored / fresh).read_bytes() == b"\x09\x09"


def test_an_import_that_settles_a_held_binary_ends_the_hold_in_the_same_commit(
    stored, writer, importing
):
    """An importer that names a held binary's row wrongly, so that the commit conflicts and holds
    the path again, or that clears the hold in a second step that a kill can leave undone."""
    held = f"{NOTES}/held-diagram.bin"
    (stored / held).write_bytes(b"\x00\x01")
    importing.import_paths([held])
    commit(writer, binary_deletes=(write.BinaryDelete(held),))
    (stored / held).write_bytes(b"\x09\x09")
    assert importing.import_paths([held]).held == [held]
    commit(writer, binaries=(write.BinaryRecord(held, sha(b"\x00\x01"), 2),))
    (stored / held).write_bytes(b"\x08\x08")
    before = revision(stored)

    settled = importing.import_paths([held])

    assert (settled.imported, settled.held) == ([held], [])
    assert rows(stored, "SELECT COUNT(*) FROM store_held WHERE path = ?", held) == [(0,)]
    assert revision(stored) == before + 1, "the record and the end of the hold are one revision"
    assert rows(stored, "SELECT sha256 FROM store_binaries WHERE path = ?", held) == [
        (sha(b"\x08\x08"),)
    ]


def test_a_renamed_binary_moves_its_record_by_hash(stored, importing):
    """An importer that records a renamed binary as a new file and a deleted one, or pairs two
    binaries that only share a name."""
    renamed = f"{NOTES}/renamed-diagram.bin"
    (stored / DIAGRAM).rename(stored / renamed)

    report = importing.import_paths([DIAGRAM, renamed])

    assert report.moved == {renamed: DIAGRAM} and report.held == [] and report.removed == []
    assert rows(stored, "SELECT sha256 FROM store_binaries WHERE path = ?", renamed) == [
        (sha(DIAGRAM_BYTES),)
    ]
    assert rows(stored, "SELECT path FROM store_binaries WHERE path = ?", DIAGRAM) == []
    assert rows(stored, "SELECT COUNT(*) FROM store_projection_queue") == [(0,)]
    assert rows(stored, "SELECT COUNT(*) FROM store_held") == [(0,)]
    assert importing.sweep().settled == 0


def test_an_edit_too_large_to_copy_is_held_by_its_hash_and_stays_on_disk(
    stored, writer, importing, monkeypatch
):
    """An importer that loads a huge file into a hold or into the store, or that drops it."""
    monkeypatch.setattr(projector, "HELD_BODY_LIMIT", len(PERSON) - 1)

    report = importing.import_paths([ALPHA]) if (stored / ALPHA).write_bytes(PERSON) else None

    assert report is not None and report.held == [ALPHA] and report.imported == []
    assert rows(stored, "SELECT kind, body, sha256 FROM store_held WHERE path = ?", ALPHA) == [
        ("edit", None, sha(PERSON))
    ]
    assert (stored / ALPHA).read_bytes() == PERSON
    assert imports(stored) == []


def test_a_path_the_importer_cannot_read_is_reported_and_never_taken_for_a_delete(
    stored, writer, importing
):
    """An importer that reads a refusal, a symlink or an unreadable folder as a deleted page, and
    holds or imports it."""
    target = stored / "outside-target.md"
    target.write_bytes(PERSON)
    (stored / ALPHA).unlink()
    (stored / ALPHA).symlink_to(target)
    folder = stored / "Knowledge Base" / "Sources"
    if os.geteuid() != 0:
        folder.chmod(0)
    try:
        events = importing.import_paths([ALPHA])
        swept = importing.sweep()
    finally:
        folder.chmod(0o755)

    assert ALPHA in events.unjudged and (events.held, events.imported) == ([], [])
    assert swept.held == [], "a refusal and a partial walk are not deletes"
    assert rows(stored, "SELECT COUNT(*) FROM store_held") == [(0,)]
    assert len(rows(stored, "SELECT path FROM store_pages WHERE path = ?", ALPHA)) == 1


def cold_differences(root, scratch):
    """The derived tables of the store that differ from a cold derivation of the vault's files."""
    expected = scratch / "expected.sqlite"
    expected.unlink(missing_ok=True)
    verify.derive_expected(root, expected)
    with closing(sqlite3.connect(connection.store_path(root))) as store:
        comparison = verify.compare_tables(store, expected)
    assert comparison.tables, "a comparison of no table proves nothing"
    return comparison.differences


def test_an_outside_edit_leaves_rows_that_equal_a_cold_derivation_of_the_files(
    stored, importing, tmp_path
):
    """An importer that imports a collection layer's file into the store's pages, or leaves the
    search, graph and identity rows of an edited file stale, so that a search finds old text."""
    layer = stored / UNOWNED
    original = layer.read_bytes()
    assert cold_differences(stored, tmp_path) == (), "the vault starts equal to its derivation"
    before = revision(stored)
    layer.write_bytes(original)
    assert importing.import_paths([UNOWNED]).settled == 0
    assert revision(stored) == before, "a file with the bytes that its rows describe is current"

    edited_layer = original + b"\nThe harbour master keeps the tide ledger.\n"
    layer.write_bytes(edited_layer)
    alpha = (stored / ALPHA).read_text(encoding="utf-8")
    (stored / ALPHA).write_text(alpha + "\nThe heron fishes at low tide.\n", encoding="utf-8")
    report = importing.import_paths([UNOWNED, ALPHA])

    assert (report.refreshed, report.imported, report.held) == ([UNOWNED], [ALPHA], [])
    assert rows(stored, "SELECT path FROM store_pages WHERE path = ?", UNOWNED) == []
    assert rows(stored, "SELECT content_hash FROM pages WHERE path = ?", UNOWNED) == [
        (sha(edited_layer),)
    ]
    assert rows(stored, "SELECT path FROM store_audit WHERE path = ?", UNOWNED) == [], (
        "the store keeps no page, record or audit row for a file that it does not own"
    )
    assert cold_differences(stored, tmp_path) == ()

    layer.write_bytes(edited_layer + b"Saved again, with no event.\n")
    swept = importing.sweep()
    assert swept.refreshed == [UNOWNED], "the sweep finds a stale row without an event"
    assert cold_differences(stored, tmp_path) == ()
    assert importing.sweep().settled == 0, "a refreshed file is current: the next sweep is quiet"

    layer.unlink()
    assert importing.import_paths([UNOWNED]).refreshed == [UNOWNED]
    assert rows(stored, "SELECT path FROM pages WHERE path = ?", UNOWNED) == []


def test_a_file_whose_rows_the_recall_policy_withholds_is_refreshed_once(
    stored, importing, monkeypatch
):
    """An importer that commits a refresh for the same bytes at every event or sweep, so that a
    file that has no rows by policy moves the store's revision without end."""
    monkeypatch.setattr("exomem.recall_policy.admits_markdown_content", lambda *args: False)
    loose = stored / "Outside" / "Withheld.md"
    loose.parent.mkdir()
    loose.write_bytes(b"# Withheld\n\nA page that recall policy keeps out of the search rows.\n")

    first = importing.import_paths(["Outside/Withheld.md"])
    before = revision(stored)
    again = importing.import_paths(["Outside/Withheld.md"])

    assert first.refreshed == ["Outside/Withheld.md"]
    assert rows(stored, "SELECT path FROM pages WHERE path = ?", "Outside/Withheld.md") == []
    assert (again.settled, revision(stored)) == (0, before)


def test_a_refused_refresh_of_a_new_file_is_asked_for_again(stored, writer):
    """An importer that remembers a new file as refreshed when the store refused the refresh, so
    that the file has no search rows until a person edits it again."""
    refusals = [write.NOT_OWNED]

    def run(work):
        if refusals:
            raise OpError(refusals.pop(), "the store refused the refresh")
        work(writer)

    refusing = importer.Importer(stored, run=run)
    fresh = stored / "Outside" / "Fresh.md"
    fresh.parent.mkdir()
    fresh.write_bytes(b"# Fresh\n\nA page that the harbour master added outside Exomem.\n")

    first = refusing.import_paths(["Outside/Fresh.md"])
    assert first.unjudged == {"Outside/Fresh.md": write.NOT_OWNED} and first.refreshed == []
    assert rows(stored, "SELECT path FROM pages WHERE path = ?", "Outside/Fresh.md") == []

    again = refusing.sweep()

    assert "Outside/Fresh.md" in again.refreshed
    assert rows(stored, "SELECT path FROM pages WHERE path = ?", "Outside/Fresh.md") != []


def test_a_sweep_refreshes_a_new_markdown_file_that_no_event_named(stored, importing, tmp_path):
    """An importer that finds an unowned file only through rows that exist, so that a file added
    while no watcher ran has no search rows, and a sweep that hides what its walk cost."""
    fresh = stored / "Outside" / "Fresh.md"
    fresh.parent.mkdir()
    fresh.write_bytes(b"# Fresh\n\nA page that the harbour master added with no event.\n")

    swept = importing.sweep()

    assert swept.refreshed == ["Outside/Fresh.md"]
    assert rows(stored, "SELECT path FROM pages WHERE path = ?", "Outside/Fresh.md") != []
    assert cold_differences(stored, tmp_path) == ()
    assert swept.corpus_files >= 2 and swept.corpus_seconds > 0, "the walk's cost is reported"
    assert importing.sweep().refreshed == [], "a refreshed file is current: the next sweep is quiet"


def test_a_sync_conflict_copy_of_an_unowned_file_gets_no_rows(stored, importing, tmp_path):
    """An importer that judges an unowned file by the Markdown suffix alone, so that a conflict
    copy that no corpus walk keeps enters the search rows through its event."""
    outside = stored / "Outside"
    outside.mkdir()
    (outside / "Fresh.md").write_bytes(b"# Fresh\n\nThe harbour master keeps this page.\n")
    copy = "Outside/Fresh.sync-conflict-20260101-120000-ABCDEFG.md"
    (stored / copy).write_bytes(b"# Fresh\n\nThe other device's copy of the page.\n")

    by_event = importing.import_paths([copy, "Outside/Fresh.md"])
    swept = importing.sweep()

    assert by_event.refreshed == ["Outside/Fresh.md"] and swept.refreshed == []
    assert rows(stored, "SELECT path FROM pages WHERE path = ?", copy) == []
    assert cold_differences(stored, tmp_path) == ()


def test_a_markdown_file_at_the_vault_root_is_imported_by_a_sweep_and_an_event(
    stored, importing, tmp_path
):
    """An importer that cannot open the folder of a file at the vault root, so that a note kept
    next to the Knowledge Base folder never reaches the search rows."""
    (stored / "Home.md").write_bytes(b"# Home\n\nThe lighthouse keeper keeps this page here.\n")
    (stored / "Landing.md").write_bytes(b"# Landing\n\nThe pilot reads this page first.\n")

    by_event = importing.import_paths(["Home.md"])
    swept = importing.sweep()

    assert by_event.refreshed == ["Home.md"] and not by_event.unjudged
    assert swept.refreshed == ["Landing.md"] and not swept.unjudged
    for rel in ("Home.md", "Landing.md"):
        assert rows(stored, "SELECT path FROM pages WHERE path = ?", rel) != []
    assert cold_differences(stored, tmp_path) == ()


def test_a_file_restored_where_the_stores_life_of_the_path_ended_is_imported(
    stored, writer, projecting, importing
):
    """An importer that takes the bytes of a deleted, moved or removed path for Exomem's own, so
    that a person's restore is neither imported nor held and the store never learns of it."""
    original = {rel: (stored / rel).read_bytes() for rel in (ALPHA, BETA, DIAGRAM)}
    moved = f"{NOTES}/Archive/Alpha.md"
    commit(
        writer,
        deletes=(write.PageDelete(BETA),),
        moves=(write.PageMove(ALPHA, moved),),
        binary_deletes=(write.BinaryDelete(DIAGRAM),),
    )
    projecting.run_pass()
    assert not any((stored / rel).exists() for rel in (ALPHA, BETA, DIAGRAM))
    for rel, body in original.items():
        (stored / rel).write_bytes(body)

    report = importing.import_paths([ALPHA, BETA, DIAGRAM])

    assert sorted(report.imported) == sorted([ALPHA, BETA, DIAGRAM]) and report.held == []
    for rel in (ALPHA, BETA):
        stored_body = rows(stored, "SELECT body FROM store_pages WHERE path = ?", rel)
        assert stored_body == [(original[rel],)]
    assert rows(stored, "SELECT sha256 FROM store_binaries WHERE path = ?", DIAGRAM) == [
        (sha(DIAGRAM_BYTES),)
    ]
    assert sorted(path for path, _ in imports(stored)) == sorted([ALPHA, BETA, DIAGRAM])
    projecting.run_pass()
    assert {rel: (stored / rel).read_bytes() for rel in original} == original


def test_a_move_after_a_kill_that_lost_the_manifest_row_removes_the_old_file_and_imports_nothing(
    stored, writer, projecting, importing
):
    """A projector or importer that takes the leftover file of a moved page for a person's edit,
    so that the store ends with the page at both its old path and its new one."""
    old, new = f"{NOTES}/Gamma.md", f"{NOTES}/Elsewhere/Gamma.md"
    body = b"# Gamma\n\nA page that a kill left on disk without a manifest row.\n"
    commit(writer, **put(old, body))
    (stored / old).write_bytes(body)
    assert rows(stored, "SELECT COUNT(*) FROM store_manifest WHERE path = ?", old) == [(0,)]
    commit(writer, moves=(write.PageMove(old, new),))

    projecting.run_pass()
    report = importing.import_paths([old, new])

    assert not (stored / old).exists(), "the projector removed Exomem's own leftover"
    assert (stored / new).read_bytes() == body
    assert rows(stored, "SELECT path FROM store_pages WHERE path IN (?, ?)", old, new) == [(new,)]
    assert (report.imported, report.held) == ([], [])
    assert imports(stored) == [] and rows(stored, "SELECT COUNT(*) FROM store_held") == [(0,)]


def test_a_held_delete_clears_when_the_file_comes_back_with_exomems_bytes(stored, importing):
    """An importer that leaves a deleted page held after the person restored it, so that the hold
    waits for a review that has nothing to decide, or that drops a held delete when the file comes
    back with other bytes."""
    original = (stored / ALPHA).read_bytes()
    (stored / ALPHA).unlink()
    assert importing.import_paths([ALPHA]).held == [ALPHA]

    (stored / ALPHA).write_bytes(original)
    by_event = importing.import_paths([ALPHA])

    assert (by_event.cleared, by_event.held, by_event.imported) == ([ALPHA], [], [])
    assert rows(stored, "SELECT COUNT(*) FROM store_held") == [(0,)]
    assert imports(stored) == [], "the bytes are the store's own: nothing is imported"

    (stored / ALPHA).unlink()
    assert importing.sweep().held == [ALPHA]
    (stored / ALPHA).write_bytes(original)
    assert importing.sweep().cleared == [ALPHA], "the sweep sees a restore with the recorded bytes"
    assert rows(stored, "SELECT COUNT(*) FROM store_held") == [(0,)]

    (stored / ALPHA).unlink()
    importing.import_paths([ALPHA])
    (stored / ALPHA).write_bytes(PERSON)
    importing.import_paths([ALPHA])
    assert rows(stored, "SELECT kind, body FROM store_held WHERE path = ?", ALPHA) == [
        ("edit", PERSON)
    ], "other bytes replace the held delete, and a person's bytes are kept"


def test_a_clean_sweep_counts_every_path_it_examined_as_unchanged(stored, importing):
    """A report whose `unchanged` counts only some of the paths that were read, so that a sweep of
    a whole vault reads as if it had looked at a handful."""
    owned = sum(1 for _ in ownership.iter_owned(stored))
    # A raw Records item has an identity row and no lexical row; the sweep reads it too.
    unowned = [
        path
        for (path,) in rows(
            stored,
            "SELECT path FROM pages WHERE content_hash IS NOT NULL UNION SELECT path FROM identities",
        )
        if not importing.takes(path) and ownership.is_markdown(path)
    ]
    assert owned > 1 and unowned, "the generated vault has owned files and an unowned page"

    report = importing.sweep()

    assert (report.settled, report.unchanged) == (0, owned + len(unowned))


def test_a_sweep_does_not_hash_a_binary_that_shows_the_version_it_hashed_before(
    stored, importing, monkeypatch
):
    """A sweep that reads every binary of the vault each interval, however large, or one that
    trusts a version so far that it misses an edit that keeps the file's size."""
    digests = []
    real = hashlib.file_digest

    def counting(*arguments, **options):
        digests.append(1)
        return real(*arguments, **options)

    monkeypatch.setattr(hashlib, "file_digest", counting)
    binaries = sum(
        1
        for found in ownership.iter_owned(stored)
        if ownership.classify(found.rel) is ownership.Ownership.BINARY
    )
    assert binaries >= 1

    first = importing.sweep()
    first_reads = len(digests)
    digests.clear()
    second = importing.sweep()

    assert first_reads - len(digests) == binaries, "the second sweep hashed no unchanged binary"
    assert (first.settled, second.settled) == (0, 0) and second.unchanged == first.unchanged
    body = bytearray(DIAGRAM_BYTES)
    body[0] ^= 0xFF
    (stored / DIAGRAM).write_bytes(bytes(body))

    third = importing.sweep()

    assert third.imported == [DIAGRAM], "an edit that keeps the size changes the version"
    assert rows(stored, "SELECT sha256 FROM store_binaries WHERE path = ?", DIAGRAM) == [
        (sha(bytes(body)),)
    ]


# -- the watcher -----------------------------------------------------------------------------------


class StoreThread:
    """One thread that owns the store's writer, as the service's store thread does."""

    def __init__(self, root):
        self._root = root
        self._jobs = queue.Queue()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        assert self._ready.wait(60)

    def _serve(self):
        path = connection.store_path(self._root)
        with connection.open_writer(path, lease_check=lambda: True) as handle:
            writing = CollectionWriter(self._root, handle)
            # The service's store thread runs every job with this binding, which is what lets a
            # governed batch commit; any other thread's batch is refused.
            preview._BOUND.set((self._root.resolve(), writing, False))
            self.ident = threading.get_ident()
            self._ready.set()
            while (job := self._jobs.get()) is not None:
                work, outcome_queue = job
                try:
                    work(writing)
                except BaseException as error:  # noqa: BLE001 - handed to the caller
                    outcome_queue.put(error)
                else:
                    outcome_queue.put(None)

    def run(self, work):
        outcome_queue = queue.Queue()
        self._jobs.put((work, outcome_queue))
        error = outcome_queue.get(timeout=120)
        if error is not None:
            raise error

    def close(self):
        self._jobs.put(None)
        self._thread.join(30)


@pytest.fixture
def store_thread(stored):
    thread = StoreThread(stored)
    yield thread
    thread.close()


def wait_for(condition, what, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.1)
    pytest.fail(f"timed out waiting for {what}")


def test_a_running_watcher_imports_a_save_and_a_rename_and_ignores_exomems_own_projection(
    stored, store_thread, monkeypatch
):
    """A watcher that never hands an editor's save or rename to the importer, or that imports or
    holds the file that the projector itself just wrote."""
    reports = []

    class Recording(importer.Importer):
        def import_paths(self, rels, moves=None):
            report = super().import_paths(rels, moves)
            reports.append((sorted(rels), report))
            return report

    watching = file_watcher.FileWatcher(
        stored, debounce_seconds=0.2, importer=Recording(stored, run=store_thread.run)
    )
    assert watching.start()
    try:
        assert watching.wait_until_seeded(120), "the first sweep ends the startup pass"
        (stored / ALPHA).write_bytes(PERSON)
        wait_for(lambda: read.owned_bytes(stored, ALPHA) == PERSON, "the saved page in the store")
        assert imports(stored, ALPHA) == [(ALPHA, "outside editor")]

        gamma = f"{NOTES}/Gamma.md"
        store_thread.run(lambda w: commit(w, **put(gamma, b"# Gamma\n")))
        projector.Projector(stored, run=store_thread.run).run_pass()
        wait_for(lambda: any(gamma in rels for rels, _ in reports), "the event of the projection")
        echo = next(report for rels, report in reports if gamma in rels)
        assert echo.settled == 0 and echo.unchanged >= 1
        assert imports(stored, gamma) == [] and rows(stored, "SELECT COUNT(*) FROM store_held") == [
            (0,)
        ]

        renamed = f"{NOTES}/Beta-renamed.md"
        os.rename(stored / BETA, stored / renamed)
        wait_for(
            lambda: rows(stored, "SELECT path FROM store_pages WHERE path = ?", renamed) != [],
            "the renamed page in the store",
        )
    finally:
        watching.stop()
    assert rows(stored, "SELECT path FROM store_pages WHERE path = ?", BETA) == []
    assert rows(
        stored, "SELECT origin FROM store_audit WHERE operation = 'move' AND path = ?", renamed
    ) == [("outside editor",)]


def test_the_service_gives_its_watcher_an_importer_only_for_a_store_mode_vault(
    vault,
    monkeypatch,
):
    """A service that sends a file-mode vault's events to the importer, or one that leaves a
    store-mode vault's events to the file-mode machinery."""
    monkeypatch.delenv("EXOMEM_DISABLE_FILE_WATCHER", raising=False)
    assert server_runtime._create_file_watcher(vault)._importer is None

    assert migrate_apply(vault) == 0

    assert isinstance(server_runtime._create_file_watcher(vault)._importer, importer.Importer)


# -- two threads, and a file that changes between a read and its commit ----------------------------


def test_an_event_import_waits_for_a_sweep_in_flight_and_the_store_keeps_the_newest_bytes(
    stored, store_thread, monkeypatch
):
    """An importer whose sweep and event import overlap, so that a commit carries bytes that the
    other has since replaced and the store ends at the older ones."""
    settling = importer.Importer(stored, run=store_thread.run)
    real, paused, release = projector.observe, threading.Event(), threading.Event()

    def observe(filesystem, rel, **how):
        found = real(filesystem, rel, **how)
        if rel == ALPHA and how.get("keep_body") and threading.current_thread().name == "sweeper":
            if not paused.is_set():
                paused.set()
                assert release.wait(60), "the sweep was never released"
        return found

    monkeypatch.setattr(projector, "observe", observe)
    (stored / ALPHA).write_bytes(PERSON)
    reports = {}
    sweeper = threading.Thread(
        target=lambda: reports.update(sweep=settling.sweep()), name="sweeper"
    )
    sweeper.start()
    assert paused.wait(60), "the sweep reads the first save and stops before its commit"
    (stored / ALPHA).write_bytes(LATER)
    eventer = threading.Thread(
        target=lambda: reports.update(event=settling.import_paths([ALPHA])), name="eventer"
    )
    eventer.start()
    eventer.join(1.0)
    assert eventer.is_alive(), "the event import runs only after the sweep has settled"

    release.set()
    sweeper.join(60)
    eventer.join(60)

    assert read.owned_bytes(stored, ALPHA) == LATER == (stored / ALPHA).read_bytes()
    assert rows(stored, "SELECT COUNT(*) FROM store_held") == [(0,)]
    assert reports["sweep"].unjudged == {ALPHA: importer.CHANGED_WHILE_READ}
    assert reports["event"].imported == [ALPHA]
    assert settling.sweep().settled == 0


def test_a_commit_of_bytes_read_before_a_newer_governed_write_is_skipped(
    stored, store_thread, monkeypatch
):
    """An importer that commits a person's older save over a governed write that was projected
    after the importer read the file, so that the store loses its newest page."""
    settling = importer.Importer(stored, run=store_thread.run)
    projection = projector.Projector(stored, run=store_thread.run)
    original = (stored / ALPHA).read_bytes()
    real, interleaved = projector.observe, []

    def observe(filesystem, rel, **how):
        found = real(filesystem, rel, **how)
        if rel == ALPHA and how.get("keep_body") and not interleaved:
            interleaved.append(rel)
            (stored / ALPHA).write_bytes(original)  # the person reverts their save
            store_thread.run(lambda w: commit(w, **put(ALPHA, STORE_BYTES)))
            projection.run_pass()  # the governed write reaches the file before the commit
        return found

    monkeypatch.setattr(projector, "observe", observe)
    (stored / ALPHA).write_bytes(PERSON)

    report = settling.import_paths([ALPHA])

    assert report.unjudged == {ALPHA: importer.CHANGED_WHILE_READ} and report.imported == []
    assert read.owned_bytes(stored, ALPHA) == STORE_BYTES == (stored / ALPHA).read_bytes()
    assert rows(stored, "SELECT COUNT(*) FROM store_held") == [(0,)]
    assert imports(stored) == []
    assert settling.import_paths([ALPHA]).settled == 0
