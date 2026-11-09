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
import sys
import threading
import time
from pathlib import Path

import pytest
import test_managed_collections_migration as managed_suite
import test_vault_store_migration as migration_suite
import test_vault_store_projector as projector_suite
from test_vault_store_migration import ALPHA, BETA, DIAGRAM, DIAGRAM_BYTES, migrate_apply
from test_vault_store_projector import NOTES, commit, put, rows

from exomem import file_watcher, server_runtime
from exomem.collection_store import connection
from exomem.collection_store.writer import CollectionWriter
from exomem.vault_store import importer, projector, read, write

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


def test_a_binary_that_changes_is_recorded_and_one_that_disappears_is_held(
    stored, writer, importing
):
    """An importer that forgets a changed binary's new hash, drops its record when a person
    deletes the file, or records a binary that a queued store delete is about to remove."""
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

    assert gone.held == [DIAGRAM]
    assert rows(stored, "SELECT kind, body FROM store_held WHERE path = ?", DIAGRAM) == [
        ("delete", None)
    ]
    assert len(rows(stored, "SELECT path FROM store_binaries WHERE path = ?", DIAGRAM)) == 1

    commit(writer, binary_deletes=(write.BinaryDelete(fresh),))
    (stored / fresh).write_bytes(b"\x09\x09")
    raced = importing.import_paths([fresh])
    assert raced.held == [fresh], "a changed file under a queued delete is held, not recorded"
    assert (stored / fresh).read_bytes() == b"\x09\x09"


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


def test_a_markdown_file_the_store_does_not_own_is_noted_for_a_lexical_refresh(stored, importing):
    """An importer that imports a collection layer's file into the store's pages, or ignores a
    change to a file that has lexical rows, so that its search rows go stale."""
    layer = stored / UNOWNED
    original = layer.read_bytes()
    layer.write_bytes(original)
    assert importing.import_paths([UNOWNED]).settled == 0
    assert importing.take_refresh() == [], "a file with the bytes that its rows describe is current"

    layer.write_bytes(original + b"\nAn outside edit.\n")
    report = importing.import_paths([UNOWNED])

    assert (report.imported, report.held) == ([], [])
    assert importing.take_refresh() == [UNOWNED]
    assert importing.take_refresh() == [], "taking the list clears it"
    assert rows(stored, "SELECT path FROM store_pages WHERE path = ?", UNOWNED) == []

    layer.write_bytes(original)
    layer.write_bytes(original.replace(b"a", b"e", 1) if b"a" in original else original + b" ")
    importing.sweep()
    assert importing.take_refresh() == [UNOWNED], "the sweep finds a stale row without an event"


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
