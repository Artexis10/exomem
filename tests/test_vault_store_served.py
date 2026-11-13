"""Store-mode bookkeeping in a served process (OpenSpec make-vault-store-canonical 4.1, 4.2, 6.4).

The service starts as a service starts (``writer_lease.start_server_lifecycle``), so the store
thread binds its own production writer (``preview.preview_store`` over the runtime's checkout) and
each write meets the store's real lease check, which requires the writing thread to hold the vault
boundary. The projector, the importer and the media writers reach the store thread through
``projector.store_thread_runner``, which takes that boundary on the store thread.
"""

from __future__ import annotations

import hashlib
import json
import os
import statistics
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_vault_store_read_write as read_write
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_scene_frames import _FakeImg, _scene
from test_vault_store_binary_writers import VIDEO, frame_rows
from test_vault_store_bypass_writers import RAW_SOURCE
from test_vault_store_media import PICTURE, PICTURE_BYTES
from test_vault_store_migration import ALPHA, BETA, DIAGRAM, DIAGRAM_BYTES, vault  # noqa: F401
from test_vault_store_write_path import store_rows

from exomem import (
    commands,
    extract,
    extraction_parts,
    media_jobs,
    media_worker,
    mutation_lock,
    scene_frames,
    writer_lease,
)
from exomem.collection_store import runtime
from exomem.governance.principal import owner_principal, request_scope
from exomem.vault_store import importer, projector, read, rederive, write
from exomem.vault_store import mode as store_mode
from exomem.writer_lease import get_manager

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="The offline-owner lease is Linux-only"),
    pytest.mark.timeout(300),
]

stored = read_write.stored

SCHEMA = "Knowledge Base/_Schema"
NOTES = "Knowledge Base/Notes"


@pytest.fixture
def service(stored, monkeypatch):
    """The service's own store thread over the store-mode vault, started as a service starts it.

    It records when each registry tick starts and how it ends: ``ended`` when a request ended its
    wait for the vault boundary (``StoreServer._maintaining``), ``returned`` otherwise.
    """
    ticks = SimpleNamespace(entered=[], ended=[], returned=[])
    real = runtime.StoreServer._registry_tick

    def recording(self):
        ticks.entered.append(time.monotonic())
        try:
            outcome = real(self)
        except mutation_lock.WaitEnded:
            ticks.ended.append(time.monotonic())
            raise
        ticks.returned.append(time.monotonic())
        return outcome

    monkeypatch.setattr(runtime.StoreServer, "_registry_tick", recording)
    manager = writer_lease.start_server_lifecycle()
    server = None
    try:
        deadline = time.monotonic() + 60
        while (server := runtime._SERVERS.get(stored.resolve())) is None or server.runtime is None:
            assert time.monotonic() < deadline, "the store thread did not open the store"
            time.sleep(0.05)
        yield ticks
    finally:
        manager.close()
        if server is not None:
            server.thread.join(10)
        writer_lease._MANAGERS.pop(manager.config, None)


def call(root, tool, **arguments):
    """One MCP command as the owner, through the dispatcher that routes a write to the store."""
    command = next(c for c in commands.product_commands_for("mcp") if c.name == tool)
    with request_scope(owner_principal(surface="mcp")):
        return writer_lease.invoke_command(command, root, **arguments)


def commit(root, **change):
    """A governed store commit through the store thread's door, as a background writer sends one."""
    projector.store_thread_runner(root)(
        lambda writer: write.commit(
            writer, write.StoreChange(write.Origin.GOVERNED, "served test", **change)
        )
    )


@contextmanager
def held_elsewhere(root):
    """Another thread of this process holds the vault boundary, as a background holder does, until
    the block ends or the yielded event is set."""
    taken, release = threading.Event(), threading.Event()

    def hold():
        guard = get_manager().mutation_guard
        with guard(root, operation="another_holder", holder_kind="background"):
            taken.set()
            release.wait(60)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert taken.wait(30), "the other holder took the boundary"
    try:
        yield release
    finally:
        release.set()
        thread.join(30)


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def manifest(root, rel):
    return store_rows(root, "SELECT sha256, revision FROM store_manifest WHERE path = ?", rel)


def queued(root):
    return {path for (path,) in store_rows(root, "SELECT path FROM store_projection_queue")}


def test_a_projector_pass_records_a_governed_writes_files_in_a_served_process(stored, service):
    """A served projector whose manifest records the store refuses: it writes or renames the file
    and records nothing, so every pass redoes it and the importer cannot tell its bytes apart."""
    created = f"{NOTES}/Served.md"
    moved = f"{NOTES}/Beta Served.md"
    call(stored, "manage_memory_file", operation="create", path=created, content="Served text.\n")
    call(stored, "manage_memory_file", operation="move", old_path=BETA, new_path=moved)

    report = projector.Projector(stored).run_pass()

    assert not report.busy and report.deferred == {}
    for rel in (created, moved):
        body = read.owned_bytes(stored, rel)
        assert (stored / rel).read_bytes() == body
        assert [row[0] for row in manifest(stored, rel)] == [sha(body)]
    assert not (stored / BETA).exists() and manifest(stored, BETA) == []
    assert queued(stored) & {created, moved, BETA} == set()


def test_the_importer_settles_outside_edits_renames_and_media_in_a_served_process(
    stored, service
):
    """A served importer whose commits the store refuses: an outside edit of a page and a rename
    of a binary stay unimported, an outside picture gains no sidecar page, and the sweep fails on
    every pass."""
    edited = b"---\ntitle: Alpha\ntype: note\n---\n# Alpha\n\nA person edited this line.\n"
    renamed = f"{NOTES}/diagram-renamed.bin"
    (stored / ALPHA).write_bytes(edited)
    (stored / DIAGRAM).rename(stored / renamed)
    (stored / PICTURE).write_bytes(PICTURE_BYTES)

    report = importer.Importer(stored).import_paths(
        [ALPHA, DIAGRAM, renamed, PICTURE], moves={renamed: DIAGRAM}
    )

    assert report.unjudged == {} and report.held == []
    assert sorted(report.imported) == sorted([ALPHA, PICTURE])
    assert report.moved == {renamed: DIAGRAM}
    assert report.media_derived == [PICTURE] and report.media_failed == {}
    assert sha(PICTURE_BYTES).encode() in read.owned_bytes(stored, f"{PICTURE}.md")
    assert read.owned_bytes(stored, ALPHA) == edited
    assert store_rows(stored, "SELECT sha256 FROM store_binaries WHERE path = ?", renamed) == [
        (sha(DIAGRAM_BYTES),)
    ]
    assert store_rows(stored, "SELECT 1 FROM store_binaries WHERE path = ?", DIAGRAM) == []


def test_a_hop_from_a_thread_that_holds_the_boundary_fails_at_once_by_name(stored):
    """A caller that hops while it holds the vault boundary: the store thread would wait for it,
    and it for the store thread, until the mutation timeout."""
    began = time.monotonic()
    with get_manager().mutation_guard(stored, operation="a_holder", holder_kind="background"):
        with pytest.raises(projector.HopUnderBoundary):
            projector.store_thread_runner(stored)(lambda _writer: None)
    assert time.monotonic() - began < 5.0


def test_a_busy_boundary_leaves_bookkeeping_for_the_next_pass_and_logs_no_error(
    stored, service, caplog
):
    """A served projector or importer that fails its pass, logs an error, or loses the intent when
    another holder keeps the vault boundary."""
    created = f"{NOTES}/Waiting.md"
    call(stored, "manage_memory_file", operation="create", path=created, content="Waiting.\n")
    edited = b"---\ntitle: Alpha\ntype: note\n---\n# Alpha\n\nEdited while the boundary was held.\n"
    caplog.set_level("WARNING")

    with held_elsewhere(stored):
        (stored / ALPHA).write_bytes(edited)
        passed = projector.Projector(stored).run_pass()
        imported = importer.Importer(stored).import_paths([ALPHA])

    assert passed.busy and passed.settled == 0 and passed.deferred == {}
    assert created in queued(stored), "the intent stays queued"
    assert imported.unjudged == {ALPHA: importer.BOUNDARY_BUSY} and imported.imported == []
    assert [r for r in caplog.records if r.name.startswith("exomem.vault_store")] == []

    assert projector.Projector(stored).run_pass().projected >= 1
    assert importer.Importer(stored).import_paths([ALPHA]).imported == [ALPHA]
    assert created not in queued(stored) and read.owned_bytes(stored, ALPHA) == edited


def test_the_media_writers_commit_in_a_served_process_past_a_parked_registry_tick(
    stored, service, monkeypatch
):
    """A media writer that a served process refuses, or whose hop waits out the mutation timeout
    behind a registry tick that waits for a boundary another thread holds (tasks.md 2.5)."""
    # The scheduler starts no tick while another thread holds the boundary, so a tick parks only
    # when a holder takes it between the tick's start and its write. ``pending`` opens that gap.
    started, go = threading.Event(), threading.Event()
    pending = rederive.pending

    def gapped(*arguments):
        if not go.is_set():
            started.set()
            assert go.wait(30), "the test opened the gap"
        return pending(*arguments)

    monkeypatch.setattr(rederive, "pending", gapped)
    mutation = get_manager()._mutation_coordinator_for(stored)
    video = stored / VIDEO
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"\x00video")
    parts = [extraction_parts.part_path(Path(RAW_SOURCE), n).as_posix() for n in (2, 3)]
    source = "---\ntitle: Minutes\ntype: source\nextraction_parts: 2\n---\n# Minutes\n"
    pages = {RAW_SOURCE: source, **{rel: f"---\npart: {rel[-5]}\n---\n# Part\n" for rel in parts}}
    go.set()
    commit(stored, puts=tuple(write.PagePut(rel, body.encode()) for rel, body in pages.items()))
    hold_seconds = 1.0
    stalls: dict[str, float] = {}

    def behind_a_parked_tick(name, round_number, writer_call):
        """An overlay change on disk makes the registry tick write. Another thread takes the
        boundary just before that write and keeps it ``hold_seconds`` once the tick waits for it.
        The writer hops meanwhile."""
        commit(stored, puts=(write.PagePut(f"{SCHEMA}/served-{round_number}.md", b"# Served\n"),))
        projector.Projector(stored).run_pass()
        started.clear()
        go.clear()
        assert started.wait(30), "a registry tick started"
        with held_elsewhere(stored) as release:
            go.set()
            deadline = time.monotonic() + 30
            while not mutation.waiter_count():  # the tick waits for the boundary
                assert time.monotonic() < deadline, "the registry tick waited for the boundary"
                time.sleep(0.01)
            threading.Timer(hold_seconds, release.set).start()
            began = time.monotonic()
            outcome = writer_call()
            stalls[name] = round(time.monotonic() - began, 3)
        return outcome

    frames = [(_scene(10.0), _FakeImg())]
    pairs = behind_a_parked_tick(
        "write", 1, lambda: scene_frames.write_scene_frames(stored, video, frames)
    )
    assert len(pairs) == 1 and frame_rows(stored) != ([], [])
    removed = behind_a_parked_tick(
        "clear", 2, lambda: scene_frames.clear_scene_frames(stored, video)
    )
    kept = behind_a_parked_tick(
        "retire", 3, lambda: extraction_parts.retire(stored, (stored / parts[1],))
    )

    waits = {"ended_by_a_request": len(service.ended), "returned": len(service.returned)}
    print(json.dumps({"hold_seconds": hold_seconds, "stalls": stalls, "tick_waits": waits}))
    assert removed == 2 and frame_rows(stored) == ([], [])
    assert kept == ()
    assert not read.page_exists(stored, parts[1]) and read.page_exists(stored, parts[0])
    # Each waited for the other holder's release and one poll, not for the mutation timeout.
    assert max(stalls.values()) < hold_seconds + 1.0
    deadline = time.monotonic() + 30
    while store_mode.status(stored)["registry"] != rederive.CURRENT:
        assert time.monotonic() < deadline, "the tick that stepped aside runs once it is free"
        time.sleep(0.05)


def test_a_media_job_result_commits_through_the_served_door(stored, service, monkeypatch):
    """A media worker whose result a served process refuses, so the extracted text never reaches
    the sidecar page and the result stays pending (tasks.md 6.4)."""
    (stored / PICTURE).write_bytes(PICTURE_BYTES)
    importer.Importer(stored).import_paths([PICTURE])
    jobs = media_jobs.MediaJobStore(stored)
    job = jobs.claim_next()
    assert job is not None and job.binary_path == stored / PICTURE
    words = "Words read from the picture."
    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda *_args, **_kwargs: extract.ExtractResult(
            text=words, media_type="image", engine="test"
        ),
    )
    worker = media_worker.MediaWorker(stored, execution_mode="process")
    worker._process(job)
    assert jobs.pending_result_count() == 1

    worker._drain_parent_results()

    assert jobs.pending_result_count() == 0
    assert words.encode() in read.owned_bytes(stored, f"{PICTURE}.md")


@pytest.mark.timeout(900)
def test_a_projector_drain_holds_the_boundary_per_record_and_a_foreground_write_waits_little(
    stored, service, monkeypatch, caplog
):
    """A drain that holds the vault boundary for its whole queue, so a foreground write waits for
    all of it and fails at the mutation timeout, or a drain that logs or fails on a busy boundary.
    Sized by ``EXOMEM_SERVED_DRILL_INTENTS``; ``EXOMEM_SERVED_DRILL_REPORT`` names a file for the
    measurements."""
    intents = int(os.environ.get("EXOMEM_SERVED_DRILL_INTENTS", "40"))
    holds: dict[str, list[float]] = {}
    real = projector._holding_boundary

    @contextmanager
    def timed(root, operation):
        """From the take to the release of one hold; a busy miss is no hold."""
        began, held = time.monotonic(), False
        try:
            with real(root, operation):
                held = True
                yield
        finally:
            if held:
                holds.setdefault(operation, []).append(time.monotonic() - began)

    monkeypatch.setattr(projector, "_holding_boundary", timed)

    def foreground(label):
        """One governed write as a person makes it, and how long it took."""
        began = time.monotonic()
        call(
            stored,
            "manage_memory_file",
            operation="create",
            path=f"{NOTES}/Foreground {label}.md",
            content="A foreground write.\n",
        )
        return time.monotonic() - began

    server = runtime._server(stored.resolve())

    def waits():
        """What a foreground write waits for that the drain can hold: the vault boundary, taken as
        a command takes it, and a store request's turn on the store thread."""
        began = time.monotonic()
        with get_manager().mutation_guard(stored, operation="a_foreground_write"):
            boundary = time.monotonic() - began
        began = time.monotonic()
        server.call(lambda: None, wake_maintenance=False)
        return boundary, time.monotonic() - began

    drained_pages = [f"{NOTES}/Drained/Page {n:04d}.md" for n in range(intents)]
    commit(stored, puts=tuple(write.PagePut(rel, f"# {rel}\n".encode()) for rel in drained_pages))
    # The same waits and writes on the same pages, with the intents queued and no projector.
    baseline_waits = []
    for _ in range(20):
        baseline_waits.append(waits())
        time.sleep(0.2)
    baseline = [foreground(f"before {n}") for n in range(2)]
    holds.clear()
    caplog.set_level("WARNING", logger="exomem.vault_store")
    # The service's own projector loop, which a busy pass sends to its next poll.
    loop = projector.Projector(stored)
    began = time.monotonic()
    loop.start()
    during, during_waits = [], []
    try:
        while set(drained_pages) & queued(stored):
            assert time.monotonic() - began < 780, "the drain finished"
            if len(during) < 2 and len(during_waits) in (5, 15):
                during.append(foreground(f"during {len(during)}"))
            during_waits.append(waits())
            time.sleep(0.2)
        drain_seconds = time.monotonic() - began
    finally:
        loop.stop()
    assert len(during_waits) >= 3, "foreground waits were measured while the projector drained"

    # One importer batch: an outside edit of as many pages as one store-thread job settles.
    batch = drained_pages[: importer.APPLY_BATCH]
    for rel in batch:
        (stored / rel).write_bytes(f"# {rel}\n\nEdited outside.\n".encode())
    imported = importer.Importer(stored).import_paths(batch)

    def ms(values, how):
        return round(how(values) * 1000, 1)

    projection = holds.get("vault_store_projection", [])
    b_idle, q_idle = zip(*baseline_waits, strict=True)
    b_busy, q_busy = zip(*during_waits, strict=True)
    figures = {
        "intents": intents,
        "drain_seconds": round(drain_seconds, 1),
        "projection_holds": len(projection),
        "projection_hold_ms": {"median": ms(projection, statistics.median),
                               "max": ms(projection, max)},
        "import_batch_paths": len(batch),
        "import_hold_ms": [ms([t], max) for t in holds.get("vault_store_import", [])],
        "boundary_wait_ms_without_drain": {"median": ms(b_idle, statistics.median),
                                           "max": ms(b_idle, max), "samples": len(b_idle)},
        "boundary_wait_ms_during_drain": {"median": ms(b_busy, statistics.median),
                                          "max": ms(b_busy, max), "samples": len(b_busy)},
        "store_turn_ms_without_drain": {"median": ms(q_idle, statistics.median),
                                        "max": ms(q_idle, max)},
        "store_turn_ms_during_drain": {"median": ms(q_busy, statistics.median),
                                       "max": ms(q_busy, max)},
        "create_ms_without_drain": [ms([t], max) for t in baseline],
        "create_ms_during_drain": [ms([t], max) for t in during],
        "load_average": [round(x, 1) for x in os.getloadavg()],
    }
    print(json.dumps(figures))
    if report := os.environ.get("EXOMEM_SERVED_DRILL_REPORT"):
        Path(report).write_text(json.dumps(figures, indent=1), encoding="utf-8")
    assert [r.getMessage() for r in caplog.records if r.name.startswith("exomem.vault_store")] == []
    assert len(projection) >= intents, "one hold for each record, none for the whole drain"
    assert sorted(imported.imported) == sorted(batch)
    # A hold is one record or one batch: far inside the timeout that a foreground writer waits.
    timeout = get_manager()._mutation_timeout_seconds
    assert max(max(times) for times in holds.values()) < timeout / 4
    assert max(b_busy) < timeout / 4 and max(q_busy) < timeout / 4
