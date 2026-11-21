"""A foreground note save never waits behind an upload commit's byte movement.

An upload commit used to copy, hash, spool and install its bytes while it held the vault mutation
boundary, so a 900 MiB file kept every note save waiting past the five-second acquire timeout and
refused it `MUTATION_BUSY`. The commit now moves its bytes into a stage before the boundary and holds
the boundary only to plan and install, which grows with nothing in the payload.

The proof runs the real resumable-session commit on generated (sparse) bytes at two sizes. It asserts what
a person sees: a note saved while the big file is still being staged is neither refused nor made to wait,
and the boundary hold of the big file does not grow with the file.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from exomem import (
    artifact_lifecycle,
    original_pool,
    server_transfer,
    upload_sessions,
    upload_stage,
    writer_lease,
)
from exomem import vault as vault_module

SMALL = 16 * 1024 * 1024
LARGE = 512 * 1024 * 1024
HOLD_OPERATION = "upload_preserve"
# The hold of one commit is half a second of fan-out and fsyncs, whose latency swings by a third once a
# large file has just been written. The least of a few holds is the one such a swing cannot inflate.
REPEATS = 3
# A hold that moves the payload again costs a whole pass over 512 MiB, a multiple of the fixed hold (the
# boundary was held 5.2 times as long for 512 MiB as for 16 MiB before the stage). The wall-clock bound sits
# above the swing seen on a quiet disk (up to 1.7 times) and below any such pass.
WALL_RATIO_LIMIT = 2.5
# The bytes the process reads and writes under the hold are counted exactly, so a pass over the payload
# cannot hide in timing noise. Index and graph pages move the same few bytes at either size.
IO_GROWTH_LIMIT = 8 * 1024 * 1024


def _process_io_bytes() -> int | None:
    """Bytes this process has passed to read and write calls, or None where the OS does not count them."""
    try:
        counters = Path("/proc/self/io").read_text(encoding="ascii")
    except OSError:
        return None
    fields = dict(line.split(": ") for line in counters.splitlines())
    return int(fields["rchar"]) + int(fields["wchar"])


@pytest.fixture(autouse=True)
def _reset_managers():
    yield
    writer_lease.reset_managers_for_tests()


def _sparse_session(root: Path, size: int, name: str) -> upload_sessions.Session:
    """A session whose held bytes are `size` zero bytes; the file is sparse until a commit reads it."""
    digest = hashlib.sha256()
    zeros = bytes(1024 * 1024)
    for _ in range(size // len(zeros)):
        digest.update(zeros)
    target = {"filename": name, "scope": "Device", "category": "Uploads", "description": None,
              "raw_protection": False, "archive": None}
    session, _secret = upload_sessions.create(
        # One binding per session: a binding may hold only a few live sessions, and these never settle.
        root, binding=f"bearer:{name}", length=size, sha256=digest.hexdigest(), target=target,
        max_bytes=upload_sessions.DEFAULT_MAX_BYTES,
    )
    os.truncate(session.part, size)
    return session


class _RecordingManager:
    """The real manager, measuring each `upload_preserve` boundary hold and noting when the commit holds it."""

    def __init__(self, real: writer_lease.LeaseManager) -> None:
        self._real = real
        self.hold_ms: list[float] = []
        self.hold_io_bytes: list[int | None] = []
        self.holding = threading.Event()

    def __getattr__(self, name: str):  # noqa: ANN204 - every other method is the real manager's
        return getattr(self._real, name)

    @contextmanager
    def absorbing_mutation_guard(self, vault_root, *, operation, **kwargs):  # noqa: ANN001, ANN003, ANN202
        with self._real.absorbing_mutation_guard(vault_root, operation=operation, **kwargs) as held:
            # The hold is the time the body runs once the guard is acquired. `last_mutation_timing` is not read:
            # the graph's own later hold of the same boundary replaces it before this guard exits.
            acquired, io_before = time.perf_counter(), _process_io_bytes()
            if operation == HOLD_OPERATION:
                self.holding.set()
            yield held
            if operation == HOLD_OPERATION:
                self.hold_ms.append((time.perf_counter() - acquired) * 1000)
                io_after = _process_io_bytes()
                self.hold_io_bytes.append(None if io_before is None or io_after is None else io_after - io_before)


def _commit(root: Path, size: int, name: str, manager: _RecordingManager) -> dict:
    session = _sparse_session(root, size, name)
    try:
        return server_transfer.commit_session(root, session.part, session.record)
    finally:
        manager.holding.clear()


@pytest.mark.parametrize("pooled", [False, True], ids=["resident-file", "shared-pool"])
def test_a_big_upload_commit_holds_the_boundary_no_longer_than_a_small_one(
    vault: Path, monkeypatch: pytest.MonkeyPatch, pooled: bool
) -> None:
    if pooled:
        # A new archive records no intent in the fixture vault, so `activate_at_start` would leave the pool off.
        monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
        original_pool.enable(vault)
        assert original_pool.enabled(vault)
    recording = _RecordingManager(writer_lease.get_manager())
    monkeypatch.setattr(writer_lease, "get_manager", lambda: recording)

    wall: dict[int, float] = {}
    moved: dict[int, int | None] = {}
    for size in (SMALL, LARGE):
        recording.hold_ms.clear()
        recording.hold_io_bytes.clear()
        for attempt in range(REPEATS):
            _commit(vault, size, f"proof-{size}-{attempt}.bin", recording)
        assert len(recording.hold_ms) == REPEATS
        wall[size] = min(recording.hold_ms)
        moved[size] = None if None in recording.hold_io_bytes else min(recording.hold_io_bytes)

    assert wall[LARGE] <= WALL_RATIO_LIMIT * wall[SMALL], (
        f"the boundary was held {wall[LARGE]:.0f} ms for {LARGE >> 20} MiB against "
        f"{wall[SMALL]:.0f} ms for {SMALL >> 20} MiB"
    )
    if moved[SMALL] is not None and moved[LARGE] is not None:
        assert moved[LARGE] - moved[SMALL] <= IO_GROWTH_LIMIT, (
            f"the commit read and wrote {moved[LARGE] - moved[SMALL]:,} more bytes under the boundary "
            f"for {LARGE >> 20} MiB than for {SMALL >> 20} MiB"
        )


def test_a_note_saved_while_a_big_upload_is_staging_is_neither_refused_nor_made_to_wait(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = writer_lease.get_manager()
    recording = _RecordingManager(manager)
    monkeypatch.setattr(writer_lease, "get_manager", lambda: recording)
    session = _sparse_session(vault, LARGE, "staging.bin")
    commit_done = threading.Event()
    outcome: list[BaseException | dict] = []

    def commit() -> None:
        try:
            outcome.append(server_transfer.commit_session(vault, session.part, session.record))
        except BaseException as error:  # noqa: BLE001 - handed to the test thread to assert on
            outcome.append(error)
        finally:
            commit_done.set()

    committer = threading.Thread(target=commit)
    committer.start()
    try:
        deadline = time.monotonic() + 30
        stage_root = upload_stage.stage_root(vault)
        while not (stage_root.exists() and any(stage_root.glob("*.part"))):
            assert time.monotonic() < deadline and not commit_done.is_set(), "the upload never began staging"
            time.sleep(0.001)

        note = vault / "Knowledge Base" / "Notes" / "saved-during-upload.md"
        started = time.monotonic()
        with manager.mutation_guard(vault, operation="note_save"):
            waited = time.monotonic() - started
            vault_module.batch_atomic_write(
                [vault_module.PlannedWrite(
                    note, "saved during the upload\n", create_only=True,
                    expected_hash=vault_module.MISSING_CONTENT_HASH,
                )],
                vault_root=vault,
            )
        saved_before_commit_finished = not commit_done.is_set()
    finally:
        committer.join(timeout=120)

    assert note.read_text(encoding="utf-8") == "saved during the upload\n"
    assert saved_before_commit_finished, "the save returned only after the commit, so it waited behind it"
    assert waited < 0.5, f"the save waited {waited:.2f} s for the boundary while the upload staged"
    assert isinstance(outcome[0], dict), outcome[0]
    assert recording.holding.is_set()
