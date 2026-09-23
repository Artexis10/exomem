"""Contract coverage for reaping abandoned lexical rebuild temporaries (#551).

`LexicalStore.rebuild_atomic` (lexstore.py) mints its detached-build sibling as
`<lexical-sidecar-name>.rebuild-<uuid4 hex>.tmp` and relies on its own
`finally: self._cleanup_sidecar_files(temp_path)` to remove it. That cleanup
never runs if the process is killed mid-build, so the temp (and any `-wal`/
`-shm` SQLite companions) is abandoned on disk forever. `graph_sync.
sweep_abandoned_temporaries` is the existing reaper for the sibling
`.graph-rebuild-*` family; before the #551 fix it does not know the lexical
shape at all.

Correction round (MAJOR, post-review): unlike the graph family, `lexstore.py`
has NO dependency on `graph_sync` — its mint site never calls
`register_temporary` or `claim_rebuild_owner`. A matching name is therefore
NOT sufficient evidence of abandonment for this family: a live in-flight
`rebuild_atomic()` build looks byte-for-byte identical by name to an
abandoned one. Every test below that expects removal explicitly ages the
file's mtime past `vault.REBUILD_TEMP_STALE_AGE_SECONDS` first
(`_age_file`, mirroring `tests/test_doctor_write_path.py`'s own helper); a
file left un-aged simulates one still being written by a live build.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest

from exomem import epistemic_graph, graph_sync, lexstore, vault


def _lexical_temp(vault_root: Path) -> Path:
    """Mint a path matching `lexstore.py`'s `rebuild_atomic` temp shape exactly.

    Mirrors the minting site directly:
    ``temp_path = self.path.with_name(f"{self.path.name}.rebuild-{uuid.uuid4().hex}.tmp")``
    """
    live = lexstore.lexical_path(vault_root)
    return live.with_name(f"{live.name}.rebuild-{uuid.uuid4().hex}.tmp")


def _age_file(path: Path, *, minutes_ago: float) -> None:
    """Back-date a file's mtime (and atime) by `minutes_ago` minutes."""
    ts = time.time() - minutes_ago * 60
    os.utime(path, (ts, ts))


# A comfortable margin past vault.REBUILD_TEMP_STALE_AGE_SECONDS (60 minutes),
# used throughout to mark a file as a genuinely abandoned orphan.
_STALE_MINUTES = 120


def test_sweep_removes_a_genuinely_stale_lexical_rebuild_temp_and_companions(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path
    live = lexstore.lexical_path(vault_root)
    live.parent.mkdir(parents=True)
    live.write_bytes(b"live lexical catalog")

    abandoned = _lexical_temp(vault_root)
    companions = [
        abandoned.with_name(f"{abandoned.name}{suffix}") for suffix in ("-wal", "-shm")
    ]
    for path in (abandoned, *companions):
        path.write_bytes(b"abandoned lexical rebuild temp")
        _age_file(path, minutes_ago=_STALE_MINUTES)

    removed = graph_sync.sweep_abandoned_temporaries(vault_root, live, live_paths=set())

    assert set(removed) == {abandoned, *companions}
    assert not abandoned.exists()
    assert all(not path.exists() for path in companions)
    assert live.read_bytes() == b"live lexical catalog"


def test_sweep_preserves_a_fresh_unclaimed_unregistered_lexical_temp(
    tmp_path: Path,
) -> None:
    """The blocking defect this test guards: `lexstore.py` never registers or
    claims its temp (no dependency on `graph_sync`), so a live in-flight
    build has NO ownership signal at all — the ONLY thing standing between
    the sweep and a live rebuild's temp is the mtime staleness gate. A fresh
    (just-written) temp must survive even though nothing registered or
    claimed it."""
    vault_root = tmp_path
    live = lexstore.lexical_path(vault_root)
    live.parent.mkdir(parents=True)

    in_flight = _lexical_temp(vault_root)
    in_flight.write_bytes(b"actively being written by rebuild_atomic")
    # No aging, no register_temporary, no claim_rebuild_owner: this is
    # exactly what a live lexstore build looks like on disk mid-build.

    removed = graph_sync.sweep_abandoned_temporaries(vault_root, live, live_paths=set())

    assert in_flight not in removed
    assert in_flight.exists()
    assert in_flight.read_bytes() == b"actively being written by rebuild_atomic"


def test_sweep_still_respects_live_path_exclusion_for_the_lexical_family(
    tmp_path: Path,
) -> None:
    """Defense in depth: even though production never registers a lexical
    temp today, a *stale* one that some caller DID register or claim must
    still be preserved — the active-path exclusion applies uniformly across
    families."""
    vault_root = tmp_path
    live = lexstore.lexical_path(vault_root)
    live.parent.mkdir(parents=True)

    registered = _lexical_temp(vault_root)
    registered.write_bytes(b"registered and stale")
    _age_file(registered, minutes_ago=_STALE_MINUTES)
    graph_sync.register_temporary(registered)
    try:
        removed = graph_sync.sweep_abandoned_temporaries(vault_root, live, live_paths=set())
        assert registered not in removed
        assert registered.exists()
    finally:
        graph_sync.unregister_temporary(registered)


def test_sweep_does_not_touch_a_similarly_named_but_non_matching_lexical_file(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path
    live = lexstore.lexical_path(vault_root)
    live.parent.mkdir(parents=True)

    # Not a match: hex segment is one character short of the minted 32.
    short_hex = live.with_name(f"{live.name}.rebuild-{'a' * 31}.tmp")
    # Not a match: an extra path segment sits between the hex and `.tmp`.
    extra_segment = live.with_name(f"{live.name}.rebuild-{uuid.uuid4().hex}.extra.tmp")
    # Not a match: uppercase hex (the mint site is always lowercase `.hex`).
    uppercase_hex = live.with_name(f"{live.name}.rebuild-{uuid.uuid4().hex.upper()}.tmp")
    # Not a match: a user file that merely happens to share the literal prefix.
    user_copy = live.with_name(f"{live.name}.rebuild-user-copy.tmp")
    decoys = (short_hex, extra_segment, uppercase_hex, user_copy)
    for path in decoys:
        path.write_bytes(b"decoy")
        # Aged too: even a stale decoy must never match on name alone.
        _age_file(path, minutes_ago=_STALE_MINUTES)

    removed = graph_sync.sweep_abandoned_temporaries(vault_root, live, live_paths=set())

    assert removed == []
    assert all(path.exists() for path in decoys)


def test_sweep_still_removes_the_graph_family_alongside_a_stale_lexical_one(
    tmp_path: Path,
) -> None:
    """The refactor must not regress the pre-existing `.graph-rebuild-*`
    family. Unlike lexical, the graph family needs no aging: its mint site
    holds `claim_rebuild_owner` for its entire build, and this sweep can only
    ever claim ownership itself while no builder currently holds it — so any
    name-matching graph temp found under a successful claim is abandoned by
    construction, fresh mtime or not."""
    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=1,
        mutation_id="1" * 24,
        paths=(("Knowledge Base/Notes/example.md", "a" * 64),),
        created_paths=("Knowledge Base/Notes/example.md",),
    )
    vault_root = tmp_path
    live = epistemic_graph.sidecar_path(vault_root)
    live.parent.mkdir(parents=True)
    graph_abandoned = graph_sync.temporary_sidecar_path(live, checkpoint)
    graph_abandoned.write_bytes(b"abandoned graph rebuild temp")
    # Deliberately left fresh (un-aged): the graph family's protection is the
    # ownership claim, not mtime, and must remove this immediately.

    lexical_live = lexstore.lexical_path(vault_root)
    lexical_abandoned = _lexical_temp(vault_root)
    lexical_abandoned.write_bytes(b"abandoned lexical rebuild temp")
    _age_file(lexical_abandoned, minutes_ago=_STALE_MINUTES)

    removed = graph_sync.sweep_abandoned_temporaries(vault_root, live, live_paths=set())

    assert set(removed) == {graph_abandoned, lexical_abandoned}
    assert not graph_abandoned.exists()
    assert not lexical_abandoned.exists()
    assert lexical_live.parent == live.parent


def test_a_family_level_unlink_failure_does_not_starve_the_other_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-family error isolation: a non-`PermissionError` unlink failure
    while sweeping the graph family (swept first) must not abort the lexical
    family's sweep in the same pass — this call is #551's only reaper for the
    lexical family, so letting a graph-side failure starve it would silently
    resume the exact leak this function exists to close."""
    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=1,
        mutation_id="3" * 24,
        paths=(("Knowledge Base/Notes/example.md", "b" * 64),),
        created_paths=("Knowledge Base/Notes/example.md",),
    )
    vault_root = tmp_path
    live = epistemic_graph.sidecar_path(vault_root)
    live.parent.mkdir(parents=True)
    graph_abandoned = graph_sync.temporary_sidecar_path(live, checkpoint)
    graph_abandoned.write_bytes(b"abandoned graph rebuild temp")

    lexical_stale = _lexical_temp(vault_root)
    lexical_stale.write_bytes(b"stale abandoned lexical rebuild temp")
    _age_file(lexical_stale, minutes_ago=_STALE_MINUTES)

    original_remove = epistemic_graph._remove_graph_rebuild_artifact

    def _fail_like_ebusy_for_graph_candidate(
        root: Path, candidate: Path, *, missing_ok: bool
    ) -> bool:
        if candidate == graph_abandoned:
            raise OSError(16, "simulated device-or-resource-busy failure")  # EBUSY
        return original_remove(root, candidate, missing_ok=missing_ok)

    monkeypatch.setattr(
        epistemic_graph,
        "_remove_graph_rebuild_artifact",
        _fail_like_ebusy_for_graph_candidate,
    )

    removed = graph_sync.sweep_abandoned_temporaries(vault_root, live, live_paths=set())

    assert lexical_stale in removed
    assert not lexical_stale.exists()
    # The graph family's own failure is isolated to that family: it neither
    # propagates out of the call nor removes the graph temp itself.
    assert graph_abandoned not in removed
    assert graph_abandoned.exists()


def test_stale_age_threshold_is_shared_with_vault_module(tmp_path: Path) -> None:
    """The staleness gate must reuse `vault.REBUILD_TEMP_STALE_AGE_SECONDS`
    (also `doctor.py`'s threshold) rather than a private duplicate: a temp
    aged to just inside the threshold survives, one aged just past it does
    not."""
    vault_root = tmp_path
    live = lexstore.lexical_path(vault_root)
    live.parent.mkdir(parents=True)
    threshold_minutes = vault.REBUILD_TEMP_STALE_AGE_SECONDS / 60

    just_fresh = _lexical_temp(vault_root)
    just_fresh.write_bytes(b"just inside the threshold")
    _age_file(just_fresh, minutes_ago=threshold_minutes - 1)

    just_stale = _lexical_temp(vault_root)
    just_stale.write_bytes(b"just past the threshold")
    _age_file(just_stale, minutes_ago=threshold_minutes + 1)

    removed = graph_sync.sweep_abandoned_temporaries(vault_root, live, live_paths=set())

    assert just_fresh not in removed
    assert just_fresh.exists()
    assert just_stale in removed
    assert not just_stale.exists()


def _sqlite_temp(path: Path) -> Path:
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    for suffix in ("-wal", "-shm"):
        companion = path.with_name(path.name + suffix)
        if not companion.exists():
            companion.write_bytes(b"")
    return path


def test_rebuild_start_sweeps_orphan_temps_older_than_ten_minutes(tmp_path: Path) -> None:
    """A killed rebuild leaves a whole catalogue behind as a temp. The next
    rebuild removes temps (and their WAL/SHM) untouched for ten minutes and
    not open, and keeps a fresh temp and one another connection still holds."""
    import sqlite3

    note = tmp_path / "Knowledge Base" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Note\n\nalpha body\n", encoding="utf-8")
    assert lexstore.search_bm25(tmp_path, "alpha", k=3, scope="kb") is not None

    orphan = _sqlite_temp(_lexical_temp(tmp_path))
    fresh = _sqlite_temp(_lexical_temp(tmp_path))
    held = _sqlite_temp(_lexical_temp(tmp_path))
    walless_base = _lexical_temp(tmp_path)
    walless = walless_base.with_name(walless_base.name + "-wal")
    walless.write_bytes(b"")
    for base in (orphan, held):
        for member in (base, base.with_name(base.name + "-wal"), base.with_name(base.name + "-shm")):
            _age_file(member, minutes_ago=11)
    _age_file(walless, minutes_ago=11)
    holder = sqlite3.connect(held)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("SELECT count(*) FROM t").fetchone()
    try:
        assert lexstore.get_store(tmp_path).rebuild_atomic()
    finally:
        holder.close()

    assert not orphan.exists()
    assert not orphan.with_name(orphan.name + "-wal").exists()
    assert not orphan.with_name(orphan.name + "-shm").exists()
    assert not walless.exists()
    assert fresh.exists()
    assert held.exists()


def test_the_sweep_never_removes_a_temp_its_builder_still_holds(tmp_path: Path, monkeypatch) -> None:
    """The builder closes its connection after the WAL fold and then re-walks
    the source; a stall there (or a clock jump) can make its temp look old and
    unopened. The builder's advisory lock on the temp, held for the whole
    build, keeps the sweep off it."""
    import threading

    note = tmp_path / "Knowledge Base" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Note\n\nalpha body\n", encoding="utf-8")
    assert lexstore.search_bm25(tmp_path, "alpha", k=3, scope="kb") is not None
    store = lexstore.get_store(tmp_path)
    original = type(store)._walk_entries
    stalled, resume = threading.Event(), threading.Event()
    calls = {"n": 0}

    def walk_then_stall(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            stalled.set()
            resume.wait(30)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(store), "_walk_entries", walk_then_stall)
    outcome = {}
    builder = threading.Thread(target=lambda: outcome.setdefault("published", store.rebuild_atomic()))
    builder.start()
    try:
        assert stalled.wait(30), "the build never reached its post-fold re-walk"
        live = lexstore.lexical_path(tmp_path)
        temps = sorted(live.parent.glob(f"{live.name}.rebuild-*.tmp*"))
        assert temps
        for member in temps:
            _age_file(member, minutes_ago=11)
        assert store._sweep_orphan_rebuild_temps() == []
        assert all(member.exists() for member in temps)
    finally:
        resume.set()
        builder.join(60)
    assert outcome.get("published") is True
