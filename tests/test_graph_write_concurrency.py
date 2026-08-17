"""A graph rebuild and a canonical write may overlap without either losing.

Until an interactive write stopped joining its own rebuild, these two could not
run at the same time, and the join was the only thing preventing it. They
conflict, on POSIX as well as Windows, so this was never a platform quirk --
and every path that already rebuilds off the write path (the watcher's
background rebuild, deferred drains) could reach it before that change.

Two halves of that conflict are guarded here.

The census half: a rebuild creates, replaces and removes its scratch sidecars
inside the very directory a canonical write takes a guarded census of, so
without an exclusion an in-flight rebuild turns an unrelated write into
`PATH_GUARD_CHANGED` and then `STALE_RECORD`. The exclusion is deliberately
narrow, and the narrowness is the part worth testing: `.graph-sync.json` and
`.graph-sync-floor.json` are written by the canonical batch *itself*, so a
change to them under a write is exactly what the guard must still refuse. An
over-broad "ignore anything starting with .graph" would silently disarm the
guard against the batch writer's own artifacts.

The sharing half: on Windows a reader that holds a page open refuses a
concurrent replacement of it. A derived-index reader has no business taking
that custody.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from exomem import deferred_index, graph_sync
from exomem import vault as vault_module


def _census(path: Path) -> tuple[str, ...]:
    """The names a canonical write's guarded directory census would record."""
    identity = vault_module._identity(path.name, os.stat(path))
    entries = vault_module._bounded_directory_entries(
        path,
        relative=path.name,
        expected=identity,
        max_entries=4096,
    )
    return tuple(entry.relative_path for entry in entries)


def test_the_census_exclusion_tracks_graph_syncs_own_artifact_names() -> None:
    """`vault` spells these literally to avoid an import cycle; bind them anyway.

    A rename of the rebuild temp prefix or the live sidecar in `graph_sync`
    would otherwise leave `vault`'s copy stale, and the census would start
    counting rebuild residue again -- reintroducing the exact failure this
    exclusion exists to prevent, silently and far from either definition.
    """
    assert vault_module._DERIVED_INDEX_PREFIXES == (
        graph_sync._TEMP_PREFIX,
        graph_sync._RESET_PREFIX,
    )

    # The reset manifest enumerates every file a reset moves. Its graph-sidecar
    # members are derived; its checkpoint and floor members are canonical-batch
    # output and must stay censused.
    canonical_batch_members = {graph_sync._CHECKPOINT_FILENAME, graph_sync._FLOOR_FILENAME}
    derived_members = set(graph_sync._RESET_MEMBERS) - canonical_batch_members
    assert vault_module._DERIVED_INDEX_NAMES == derived_members


@pytest.mark.parametrize(
    "name",
    [
        ".graph.sqlite",
        ".graph.sqlite-journal",
        ".graph.sqlite-wal",
        ".graph.sqlite-shm",
        ".graph-rebuild-0123456789abcdef.sqlite",
        ".graph-rebuild-0123456789abcdef.sqlite-wal",
        ".graph-reset-0123456789abcdef",
    ],
)
def test_a_rebuilds_residue_does_not_move_the_census(tmp_path: Path, name: str) -> None:
    """The write must commit even though a rebuild is churning beside it."""
    guarded = tmp_path / "Knowledge Base"
    guarded.mkdir()
    (guarded / "page.md").write_text("body\n", encoding="utf-8")

    before = _census(guarded)

    residue = guarded / name
    if name.startswith(graph_sync._RESET_PREFIX):
        residue.mkdir()
    else:
        residue.write_bytes(b"derived")

    assert _census(guarded) == before


@pytest.mark.parametrize(
    "name",
    [
        ".deferred-index.sqlite",
        ".deferred-index.sqlite-journal",
        ".deferred-index.sqlite-wal",
        ".deferred-index.sqlite-shm",
    ],
)
def test_the_dirty_path_queues_database_does_not_move_the_census(
    tmp_path: Path, name: str
) -> None:
    """The graph enqueue runs *inside* the guarded window, by design.

    `converge-graph-incrementally` records a batch's graph debt before that
    batch commits, so a crash between the markdown and the enqueue cannot lose
    the dirty set. The ordering is load-bearing, and it means a derived SQLite
    file is created and journalled while the census is open. Counting it made
    the first write to a fresh vault fail as `STALE_RECORD: canonical record
    changed before commit` -- the write invalidating itself, with nothing
    concurrent involved at all.
    """
    guarded = tmp_path / "Knowledge Base"
    guarded.mkdir()
    (guarded / "page.md").write_text("body\n", encoding="utf-8")

    before = _census(guarded)
    (guarded / name).write_bytes(b"derived")

    assert _census(guarded) == before


def test_the_census_exclusion_tracks_the_deferred_indexs_own_database_name() -> None:
    """Bind the literal, for the reason the graph_sync names are bound.

    `vault` cannot import `deferred_index` at module scope without a cycle, so
    it spells the basename. A rename there would otherwise leave this copy
    stale and quietly start counting the queue again.
    """
    assert deferred_index.store_path(Path("vault")).name == vault_module._DEFERRED_INDEX_BASENAME


@pytest.mark.parametrize("name", [".graph-sync.json", ".graph-sync-floor.json"])
def test_the_canonical_batchs_own_graph_artifacts_stay_censused(
    tmp_path: Path, name: str
) -> None:
    """The narrow half: these are batch output, so the guard must still see them."""
    guarded = tmp_path / "Knowledge Base"
    guarded.mkdir()
    (guarded / "page.md").write_text("body\n", encoding="utf-8")

    before = _census(guarded)
    (guarded / name).write_bytes(b"{}")
    after = _census(guarded)

    assert after != before
    assert any(entry.endswith(name) for entry in after)


#: Replacements to drive against a continuously re-opening reader. Far more
#: hostile than any real rebuild, which reads a page once and moves on.
_REPLACEMENT_ROUNDS = 200


def _drive_reader_against_replacements(
    page: Path,
    staging: Path,
    read: Callable[[Path], object],
    replace: Callable[[Path, Path], None],
) -> tuple[list[BaseException], list[BaseException]]:
    """Run `read` in a loop against `replace` in a loop; collect both sides' failures."""
    stop = threading.Event()
    read_failures: list[BaseException] = []
    replace_failures: list[BaseException] = []

    def read_continuously() -> None:
        while not stop.is_set():
            try:
                read(page)
            except FileNotFoundError:
                pass  # a replacement is not atomic against a *missing* target
            except BaseException as error:  # noqa: BLE001 - the point of the test
                read_failures.append(error)
                return

    reader = threading.Thread(target=read_continuously, name="rebuild-reader")
    reader.start()
    try:
        for attempt in range(_REPLACEMENT_ROUNDS):
            staged = staging / f"staged-{attempt}"
            staged.write_bytes(b"replaced %d\n" % attempt)
            try:
                replace(staged, page)
            except BaseException as error:  # noqa: BLE001 - the point of the test
                replace_failures.append(error)
    finally:
        stop.set()
        reader.join(timeout=30)

    assert not reader.is_alive(), "the reader thread wedged"
    return read_failures, replace_failures


def test_a_rebuilds_reads_and_a_writers_replacements_both_survive(tmp_path: Path) -> None:
    """The pair contract, driven from both sides at once.

    A single read/replace pair almost never overlaps, so this drives both in
    loops -- the shape that actually reproduced the failure. On POSIX
    `rename(2)` over an open file is always permitted and this passes trivially;
    the assertion is identical either way, which is the point.
    """
    page = tmp_path / "page.md"
    page.write_bytes(b"original\n")
    staging = tmp_path / "staging"
    staging.mkdir()

    read_failures, replace_failures = _drive_reader_against_replacements(
        page,
        staging,
        vault_module.read_bytes_without_pinning,
        lambda src, dst: vault_module.replace_tolerating_transient_sharing(
            lambda: os.replace(src, dst)
        ),
    )

    assert read_failures == []
    assert replace_failures == []
    assert page.read_bytes() == b"replaced %d\n" % (_REPLACEMENT_ROUNDS - 1)


@pytest.mark.skipif(os.name != "nt", reason="only Windows restricts sharing this way")
def test_neither_half_of_the_pair_is_sufficient_alone(tmp_path: Path) -> None:
    """Why both fixes exist. Records the failure each half leaves behind.

    A pinning read kills the *reader*: a rebuild sweeping the corpus dies the
    moment it opens a page a writer has marked delete-pending. Switching to a
    non-pinning read fixes that and makes the *writer* worse, because
    FILE_SHARE_DELETE is exactly what lets a replacement mark the target
    delete-pending while that handle lives.

    Without this test, a later reader could "simplify" either half away and see
    the suite stay green on the half that remains.
    """
    page = tmp_path / "page.md"
    staging = tmp_path / "staging"
    staging.mkdir()

    page.write_bytes(b"original\n")
    pinning_reads, _ = _drive_reader_against_replacements(
        page, staging, lambda p: p.read_bytes(), os.replace
    )
    assert pinning_reads, "a pinning read must be observed failing against a replacement"
    assert all(isinstance(error, PermissionError) for error in pinning_reads)

    page.write_bytes(b"original\n")
    _, unretried_replacements = _drive_reader_against_replacements(
        page, staging, vault_module.read_bytes_without_pinning, os.replace
    )
    assert unretried_replacements, (
        "an unretried replacement must be observed failing against a non-pinning reader"
    )
    assert all(
        error.winerror in vault_module._WINDOWS_SHARING_ERRORS
        for error in unretried_replacements
    )


def test_the_sharing_retry_reports_which_attempt_succeeded() -> None:
    """The budget must be observably sized, not merely observed to fit."""
    attempts: list[int] = []

    def fail_twice_then_succeed() -> None:
        attempts.append(len(attempts))
        if len(attempts) <= 2:
            error = PermissionError(13, "Access is denied")
            error.winerror = 32
            raise error

    rechecked = 0

    def recheck() -> None:
        nonlocal rechecked
        rechecked += 1

    if os.name != "nt":
        pytest.skip("the retry deliberately does not engage off Windows")

    assert vault_module.replace_tolerating_transient_sharing(
        fail_twice_then_succeed, recheck=recheck
    ) == 2
    assert rechecked == 2, "the precondition must be re-proved after every wait"


def test_a_non_sharing_permission_error_is_not_retried() -> None:
    """Waiting out a genuine permission failure would hang the request."""
    calls = 0

    def always_denied() -> None:
        nonlocal calls
        calls += 1
        error = PermissionError(13, "Access is denied")
        error.winerror = 1314  # ERROR_PRIVILEGE_NOT_HELD, not a sharing violation
        raise error

    with pytest.raises(PermissionError):
        vault_module.replace_tolerating_transient_sharing(always_denied)
    assert calls == 1


def test_read_bytes_without_pinning_returns_the_same_bytes(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    page.write_bytes(b"# heading\n\nbody with \xc3\xa9 and a \x00 byte\n")
    assert vault_module.read_bytes_without_pinning(page) == page.read_bytes()


def test_read_bytes_without_pinning_reports_a_missing_file_normally(tmp_path: Path) -> None:
    """Callers catch FileNotFoundError; a raw OSError would escape them."""
    with pytest.raises(FileNotFoundError):
        vault_module.read_bytes_without_pinning(tmp_path / "absent.md")

    with pytest.raises(FileNotFoundError):
        vault_module.read_bytes_without_pinning(tmp_path / "no-such-dir" / "absent.md")
