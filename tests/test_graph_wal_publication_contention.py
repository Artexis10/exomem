"""The live graph store must be in WAL mode from the moment it becomes live.

Three places state the invariant that every later publication builds on.
`graph_sync.replace_sidecar` opens with "An existing live graph runs in WAL
mode"; `graph_sync._publish_sidecar_in_place` repeats it ("The private rebuild
is a single rollback-journal file; the live destination is WAL"); and
`epistemic_graph._prepare_live_graph_wal_family` relies on it to justify
establishing WAL with a *zero* busy timeout -- "If another graph writer exists,
its WAL family is already reachable and can be published without taking the
write reservation below." That zero is deliberate and load-bearing: identity
coordination must never inherit the ordinary five-second SQLite wait, because
the wait would starve unrelated public mutations that need a catalogue
snapshot.

The invariant does not hold. A first publication has no live database to back
up into, so it *moves* the proven rebuild into place -- and a private rebuild is
deliberately kept in rollback-journal mode, so that authoritative rows never
remain in a detached `-wal` companion. The live store therefore begins life in
`delete` mode, and the DELETE->WAL conversion is left to whichever caller opens
it next, with no patience for a lock.

Converting a rollback-journal database to WAL needs exclusive access, so a
single concurrent reader holding SHARED is enough to refuse it. Re-issuing the
same pragma against a database that is *already* WAL needs no lock at all and
succeeds under the same contention, which is why the window closes for good the
moment one conversion lands, and why the defect is intermittent: it needs a
contender inside the window between the first publication and the first
successful conversion.

These tests pin the invariant at publication, the one point where the converter
is guaranteed to be uncontended.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from exomem import epistemic_graph, freshness, graph_sync, state_paths
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.epistemic_graph import EpistemicGraphIndex

NOTES = "Knowledge Base/Notes"


def _note(index: int) -> str:
    return "\n".join(
        [
            "---",
            "type: pattern",
            "status: active",
            "created: 2026-01-12",
            "updated: 2026-06-10",
            "sources: []",
            "pattern_type: architectural",
            "tags: [generated]",
            "---",
            "",
            f"# Generated note {index}",
            "",
            "## Problem",
            f"Synthetic body {index} for WAL publication contention.",
            "",
        ]
    )


def _published_graph(vault: Path) -> EpistemicGraphIndex:
    """Seed a vault and take it through exactly one real graph publication."""

    notes = vault / NOTES
    notes.mkdir(parents=True, exist_ok=True)
    for index in range(3):
        (notes / f"generated-note-{index}.md").write_text(_note(index), encoding="utf-8")
    freshness.seed(
        vault,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(vault)),
    )
    kb = vault / "Knowledge Base"
    freshness.seed(
        vault,
        "kb",
        ((str(path), freshness.stat_signature(path)) for path in find_module._walk_md(kb)),
    )
    graph = EpistemicGraphIndex(vault)
    graph.rebuild_all()
    assert graph.path.exists(), "the first publication did not create a live sidecar"
    return graph


def _journal_mode(path: Path) -> str:
    """Read the persisted journal mode without going through the converter."""

    conn = sqlite3.connect(path)
    try:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        conn.close()


def test_the_first_publication_leaves_the_live_store_in_wal_mode(tmp_path: Path) -> None:
    """The moved rebuild must be converted by its publisher, not by its next reader.

    `replace_sidecar` promises "An existing live graph runs in WAL mode" to every
    later publication. The publisher is the only actor that can keep that promise
    without racing: it has just created the live name, so nothing else can hold a
    lock on it yet.
    """

    graph = _published_graph(tmp_path)

    assert _journal_mode(graph.path) == "wal", (
        "the first publication left the live graph store in rollback-journal "
        "mode, so the DELETE->WAL conversion every later opener must perform "
        "needs exclusive access to a store that concurrent traffic can already "
        "be holding. `_prepare_live_graph_wal_family` issues that conversion "
        "with a zero busy timeout on purpose, so one contender refuses it "
        "outright."
    )


def test_a_live_graph_opener_survives_a_reader_after_the_first_publication(
    tmp_path: Path,
) -> None:
    """Opening the freshly published live store must tolerate a concurrent reader.

    A held SHARED read lock is the minimal faithful contender, and the one the
    publication path already names: "a reader holding SHARED across the window".
    It blocks a DELETE->WAL conversion, because that needs exclusive access, and
    blocks nothing at all once the store is WAL -- so this assertion is
    deterministic in both directions rather than a race that usually passes.
    """

    graph = _published_graph(tmp_path)

    reader = sqlite3.connect(graph.path, isolation_level=None)
    try:
        reader.execute("BEGIN DEFERRED")
        reader.execute("SELECT count(*) FROM graph_nodes").fetchone()

        connection = graph._connect()
        try:
            mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        finally:
            connection.close()
    finally:
        reader.rollback()
        reader.close()

    assert mode == "wal"


def test_an_in_place_republication_keeps_the_live_store_in_wal_mode(tmp_path: Path) -> None:
    """A second publication backs up into the live store; that must preserve WAL.

    The first publication moves the proven rebuild; every later one copies into
    the existing live database with SQLite's backup API. `Connection.backup`
    overwrites the destination wholesale, so the destination's journal mode is a
    property worth asserting rather than assuming -- if a republication silently
    returned the live store to rollback-journal mode, it would re-open the
    conversion window this change closes, one generation later.
    """

    graph = _published_graph(tmp_path)
    assert _journal_mode(graph.path) == "wal"

    # A second rebuild of a changed vault publishes in place rather than by move.
    (tmp_path / NOTES / "generated-note-3.md").write_text(_note(3), encoding="utf-8")
    freshness.seed(
        tmp_path,
        "vault",
        (
            (str(path), freshness.stat_signature(path))
            for path in vault_module.walk_vault_md(tmp_path)
        ),
    )
    freshness.seed(
        tmp_path,
        "kb",
        (
            (str(path), freshness.stat_signature(path))
            for path in find_module._walk_md(tmp_path / "Knowledge Base")
        ),
    )
    report = graph.rebuild_all()

    assert report["indexed_files"] == 4, "the second rebuild did not see the new note"
    assert _journal_mode(graph.path) == "wal", (
        "an in-place republication returned the live store to rollback-journal "
        "mode, which re-opens the DELETE->WAL conversion window for every later "
        "opener"
    )


def _publish_again(vault: Path, graph: EpistemicGraphIndex, index: int) -> dict[str, int]:
    """Add a note and take the vault through a second, in-place publication."""

    (vault / NOTES / f"generated-note-{index}.md").write_text(_note(index), encoding="utf-8")
    freshness.seed(
        vault,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(vault)),
    )
    freshness.seed(
        vault,
        "kb",
        (
            (str(path), freshness.stat_signature(path))
            for path in find_module._walk_md(vault / "Knowledge Base")
        ),
    )
    return graph.rebuild_all()


def test_two_publications_leave_no_graph_rebuild_runtime_file_behind(tmp_path: Path) -> None:
    """A sealed temporary must not regrow companions the publication cannot carry.

    `replace_sidecar` publishes and removes the temporary's *main* file, so
    anything else sharing its stem outlives the publication. A read-only open of
    a sealed WAL database that has no companions CREATES `-wal`/`-shm`, and a
    read-only last close does not remove them -- so the in-place backup path,
    which opens the temporary `?mode=ro`, can strand a pair in the state root.

    That is not harmless litter. `vault.is_graph_rebuild_runtime_file_name`
    already counts those suffixes as rebuild runtime files, so `doctor` warns on
    a healthy vault once the pair ages past
    `vault.REBUILD_TEMP_STALE_AGE_SECONDS` and recommends stopping the service;
    and the pair becomes the newest group for the preserved-temporary reaper
    (`PRESERVED_TEMPORARY_LIMIT = 1`), so a genuinely retained temporary is the
    one collected instead. `test_graph_rebuild_availability.py::
    test_full_rebuild_replacement_refusal_retains_complete_temp_for_later_recovery`
    fails for exactly that reason when a pair is stranded.

    The existing companion test guards the live store; this one guards the
    temporary, which is where the risk actually materialised.
    """

    graph = _published_graph(tmp_path)
    assert _publish_again(tmp_path, graph, 3)["indexed_files"] == 4

    state_dir = state_paths.vault_state_dir(tmp_path)
    stranded = sorted(
        entry.name
        for entry in state_dir.iterdir()
        if vault_module.is_graph_rebuild_runtime_file_name(entry.name)
    )

    assert stranded == [], (
        "a graph-rebuild runtime file outlived the publication that consumed it: "
        f"{stranded}. The publication moves or copies only the main file, so a "
        "companion created after sealing is stranded in the state root, ages into "
        "a doctor WARN on a healthy vault, and displaces a genuinely preserved "
        "temporary in the reaper's newest group."
    )


def test_the_published_store_carries_no_detached_wal_companion(tmp_path: Path) -> None:
    """The move must transport one self-contained file, and it must read back.

    A rebuild is deliberately kept in rollback-journal mode so the single-file
    move cannot leave authoritative rows behind in a `-wal` the move does not
    carry. Sealing the rebuild as WAL preserves that only because a clean last
    close checkpoints the companions away and leaves the setting in the database
    header -- so this asserts both halves: no companion travels with the file,
    and the published store still answers for every seeded note.
    """

    graph = _published_graph(tmp_path)

    for suffix in ("-wal", "-shm"):
        companion = graph.path.with_name(f"{graph.path.name}{suffix}")
        assert not companion.exists(), (
            f"the published live store kept a detached {suffix} companion; a "
            "single-file publication cannot carry it"
        )

    conn = sqlite3.connect(graph.path)
    try:
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute("SELECT count(*) FROM graph_nodes").fetchone()[0] == 3
    finally:
        conn.close()


def test_sealing_the_rebuild_opens_no_sqlite_under_the_publication_hold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sealing must happen while the rebuild is private, never under the hold.

    The claim is narrow and deliberately so: the **seal** adds no SQLite work
    under the hold. The hold is not SQLite-free in general -- an in-place
    republication's `Connection.backup` runs under it by existing design. What
    matters is that the hold is the shared mutation boundary, so nothing new
    should be added to what it spans.

    `test_graph_rebuild_availability.py::test_rebuild_publication_hold_runs_no_disk_or_sqlite_work`
    pins that for a first publication, but it cannot run on every host (the
    secure rebuild lock is unavailable on some, and it fails there before
    publication is ever reached), so this repeats the guarantee for the seal on
    the path that does run.

    The first placement of this fix put the seal inside the hold and broke that
    contract; this is the regression test for it.
    """

    graph = EpistemicGraphIndex(tmp_path)
    notes = tmp_path / NOTES
    notes.mkdir(parents=True, exist_ok=True)
    for index in range(3):
        (notes / f"generated-note-{index}.md").write_text(_note(index), encoding="utf-8")
    freshness.seed(
        tmp_path,
        "vault",
        (
            (str(path), freshness.stat_signature(path))
            for path in vault_module.walk_vault_md(tmp_path)
        ),
    )
    freshness.seed(
        tmp_path,
        "kb",
        (
            (str(path), freshness.stat_signature(path))
            for path in find_module._walk_md(tmp_path / "Knowledge Base")
        ),
    )

    holding = False
    holds_taken = 0
    opened_under_hold: list[str] = []
    inner = graph._mutation_coordinator

    class HoldWatcher:
        """Delegate to the real coordinator while observing its hold window."""

        def __getattr__(self, name: str) -> object:
            return getattr(inner, name)

        @contextmanager
        def hold(self, **kwargs: object) -> Iterator[None]:
            nonlocal holding, holds_taken
            holds_taken += 1
            holding = True
            try:
                with inner.hold(**kwargs):
                    yield
            finally:
                holding = False

    graph._mutation_coordinator = HoldWatcher()

    real_connect = epistemic_graph.sqlite3.connect

    def watched_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        if holding:
            opened_under_hold.append(str(args[0]) if args else "<unknown>")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(epistemic_graph.sqlite3, "connect", watched_connect)

    graph.rebuild_all()

    assert holds_taken > 0, "no publication hold was taken; the assertion below would be vacuous"
    assert opened_under_hold == [], (
        "SQLite was opened under the publication hold, which starves unrelated "
        f"mutations at the shared boundary: {opened_under_hold}"
    )
    assert _journal_mode(graph.path) == "wal"


def test_an_unsealable_rebind_candidate_falls_back_instead_of_failing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A seal refusal at the registry-rebind site must answer `False`, not raise.

    That path has no `except` of its own, only a `finally`. An escaping refusal
    would skip the full-rebuild fallback and surface to the caller as a
    convergence failure, which overstates the problem: not being able to rebind
    the registry in place is exactly the condition the full rebuild exists for.
    `False` is the answer the function already gives for "cannot rebind".
    """

    graph = _published_graph(tmp_path)

    # `rebind_registry` only proceeds for a full-scope checkpoint that the
    # canonical epoch already names. A plain rebuild leaves the epoch's
    # checkpoint unset and a governed write makes it `paths`-scoped, so the
    # full-scope epoch is supplied here; everything from the private candidate
    # build onward then runs for real up to the seal.
    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=1,
        mutation_id=f"{1:024x}",
        paths=(),
        created_paths=(),
        scope="full",
    )
    epoch = dataclasses.replace(
        graph_sync.canonical_publication_epoch(tmp_path), checkpoint=checkpoint
    )
    monkeypatch.setattr(graph_sync, "canonical_publication_epoch", lambda _root: epoch)

    # A registry rebind exists for exactly one condition: the published store's
    # extension-registry hash no longer matches the loaded registry. Simulating
    # that change is what makes `_registry_rebind_source_proof` produce a proof
    # and the path reach the seal.
    graph.registry = dataclasses.replace(graph.registry, extension_hash="f" * 64)

    refusals: list[Path] = []

    def refuse(_vault_root: Path, temporary: Path) -> None:
        refusals.append(temporary)
        raise sqlite3.DatabaseError("proven graph rebuild could not be sealed in WAL mode")

    monkeypatch.setattr(epistemic_graph, "_seal_graph_rebuild_as_wal", refuse)

    declined = graph.rebind_registry(checkpoint)

    assert refusals, "the seal was never reached, so the assertion below would be vacuous"
    assert declined is False, (
        "an unsealable rebind candidate raised instead of answering False, so the "
        "full-rebuild fallback is skipped and the caller reports a convergence "
        "failure for a condition the full rebuild already handles"
    )


def test_a_companion_beside_the_rebuild_refuses_the_immutable_backup(tmp_path: Path) -> None:
    """An unsettled source must be refused, not read immutably.

    The in-place publication opens the rebuild `?mode=ro&immutable=1`, which
    tells SQLite the file cannot change: it skips locking and **ignores a hot
    rollback journal**. Measured on this SQLite, an immutable open of a
    rollback-mode database with a hot journal served 499 uncommitted rows and
    reported `integrity_check = ok`, where a plain read-only open refuses with
    "attempt to write a readonly database". An unexpected companion is therefore
    the difference between a loud refusal and an undetectable wrong answer
    published as the live graph.

    A sealed, cleanly closed rebuild has no `-journal`, `-wal` or `-shm`, so this
    refusal cannot fire on the settled path -- it makes the immutable open's
    precondition something the code holds rather than something a comment claims.
    """

    graph = _published_graph(tmp_path)
    live_before = graph.path.read_bytes()

    temporary = graph.path.with_name(f".graph-rebuild-{'a' * 64}-{'b' * 24}.sqlite")
    rebuild = graph._connect(temporary)
    rebuild.close()
    epistemic_graph._seal_graph_rebuild_as_wal(tmp_path, temporary)

    journal = temporary.with_name(f"{temporary.name}-journal")
    journal.write_bytes(b"an unsettled rollback journal")

    with pytest.raises(sqlite3.DatabaseError, match="not settled for an immutable read"):
        epistemic_graph._backup_graph_rebuild_into_store(
            tmp_path,
            temporary,
            graph.path,
            timeout=0.5,
        )

    assert graph.path.read_bytes() == live_before, (
        "the live graph store was modified from a source that was not settled"
    )


def test_re_establishing_wal_on_the_live_store_is_lock_free(tmp_path: Path) -> None:
    """The zero busy timeout is only safe because a settled store needs no lock.

    This is the fact `_prepare_live_graph_wal_family`'s deliberate zero rests on,
    and the reason the fix belongs at publication rather than in the pragma's
    patience: once the store is WAL, every later opener re-issues the same
    journal-mode pragma under contention and succeeds without waiting, so
    identity coordination never inherits a five-second SQLite wait.
    """

    graph = _published_graph(tmp_path)
    settled = graph._connect()
    settled.close()
    assert _journal_mode(graph.path) == "wal"

    writer = sqlite3.connect(graph.path, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        opener = sqlite3.connect(graph.path, isolation_level=None)
        try:
            epistemic_graph._set_sqlite_busy_timeout(opener, 0)
            assert opener.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        finally:
            opener.close()
    finally:
        writer.rollback()
        writer.close()
