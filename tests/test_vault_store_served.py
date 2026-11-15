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
import logging
import os
import shutil
import signal
import statistics
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_media_store as media_store_suite
import test_vault_store_read_write as read_write
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_media_store import _offload, _volume
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
    media_store,
    media_worker,
    mutation_lock,
    reserved_paths,
    scene_frames,
    server_runtime,
    writer_lease,
)
from exomem.collection_store import runtime
from exomem.governance.principal import owner_principal, request_scope
from exomem.vault_store import binaries, importer, projector, read, rederive, write
from exomem.vault_store import media as store_media
from exomem.vault_store import mode as store_mode
from exomem.writer_lease import get_manager

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="The offline-owner lease is Linux-only"),
    pytest.mark.timeout(300),
]

stored = read_write.stored
selected_store = media_store_suite.selected_store

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
    # Each waited for the other holder's release and one poll, not for the mutation timeout. The
    # bound is halfway between the two, so a loaded host does not fail it.
    waited_out = get_manager()._mutation_timeout_seconds
    assert max(stalls.values()) < hold_seconds + (waited_out - hold_seconds) / 2
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


def test_a_served_start_gives_a_media_file_dropped_on_disk_its_companion_page(
    stored, service, monkeypatch, caplog
):
    """A served start whose media pass hops from under its own boundary
    (``HopUnderBoundary``), so a media file that arrived on disk with no companion page gets
    none, and its content stays out of search and recall."""
    dropped = f"{NOTES}/dropped.png"
    (stored / dropped).write_bytes(PICTURE_BYTES)
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    caplog.set_level("WARNING")

    assert server_runtime._start_media_worker(stored) is None

    assert sha(PICTURE_BYTES).encode() in read.owned_bytes(stored, f"{dropped}.md")
    assert [r for r in caplog.records if "HopUnderBoundary" in r.getMessage()] == []
    assert [r for r in caplog.records if "media reconciliation failed" in r.getMessage()] == []


def test_the_media_workers_own_frame_write_commits_through_the_served_door(stored, service):
    """A worker whose scene-frame unit hops from under its own boundary, or that a served process
    refuses, so a video keeps no viewable frames and its frames get no OCR jobs."""
    video = stored / VIDEO
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"\x00video")
    importer.Importer(stored).import_paths([VIDEO])  # the video gets its companion page
    job = media_jobs.MediaJob(
        binary_path=video, sidecar_path=stored / f"{VIDEO}.md", media_type="video"
    )
    worker = media_worker.MediaWorker(stored, execution_mode="process")

    # No CLIP pass ran, so there are no parent samples to bind, as the direct write takes it.
    worker._persist_scene_frames(job, [(_scene(10.0), _FakeImg())], parent_clip_samples=None)

    pages, records = frame_rows(stored)
    assert len(pages) == 1 and len(records) == 1
    queued_frames = media_jobs.MediaJobStore(stored).get_by_binary(stored / records[0][0])
    assert queued_frames is not None and queued_frames.do_ocr


def test_a_media_store_offload_publishes_its_reference_through_the_served_door(
    stored, service, selected_store
):
    """An offload whose reference unit a served process refuses, or that hops from under its own
    boundary, so the original never records its stored copy and can never be evicted."""
    (stored / PICTURE).write_bytes(PICTURE_BYTES)
    importer.Importer(stored).import_paths([PICTURE])

    reference = _offload(stored, stored / PICTURE)

    assert reference is not None
    assert reference.object.encode() in read.owned_bytes(stored, f"{PICTURE}.md")
    assert media_store.original_state(stored, PICTURE).state == "cached"


def test_an_eviction_that_meets_a_busy_boundary_ends_its_pass_quietly_and_the_next_one_releases(
    stored, service, selected_store, monkeypatch, caplog
):
    """An eviction whose absent stored version must be released while another holder keeps the
    vault boundary: ``MEDIA_STORE_BUSY`` escapes to the worker, which logs a failure or (at start)
    skips the rest of its scan, or the reference to the lost copy is never released."""
    (stored / PICTURE).write_bytes(PICTURE_BYTES)
    importer.Importer(stored).import_paths([PICTURE])
    reference = _offload(stored, stored / PICTURE)
    # The stored copy is gone, so the eviction must release the reference instead of evicting.
    (Path(selected_store[0]["root"]) / reference.object / reference.version).unlink()
    monkeypatch.setattr(shutil, "disk_usage", _volume(10_000, 9_000, stored / PICTURE))
    monkeypatch.setattr(store_media, "BUSY_WAIT_SECONDS", 0.5)
    caplog.set_level("WARNING")

    with held_elsewhere(stored):
        assert media_store.evict_to_target(stored) == 0
        media_worker._evict(stored)

    assert reference.object.encode() in read.owned_bytes(stored, f"{PICTURE}.md")
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    media_worker._evict(stored)

    assert reference.object.encode() not in read.owned_bytes(stored, f"{PICTURE}.md")
    assert (stored / PICTURE).read_bytes() == PICTURE_BYTES


def idle_service_projector(root, poll_seconds=60.0):
    """The service's projector loop, started as the service starts it and then idle in its poll."""
    loop = projector.Projector(root, poll_seconds=poll_seconds)
    loop.start()
    deadline = time.monotonic() + 30
    while queued(root):
        assert time.monotonic() < deadline, "the start drained the queue"
        time.sleep(0.05)
    time.sleep(0.5)  # its pass has ended; it now waits out the poll
    return loop


def test_a_clean_stop_projects_what_is_queued_so_the_files_match_the_store(stored, service):
    """A clean service stop that leaves committed pages queued, so their files lag the store and an
    offline rollback refuses until the next start."""
    activation = server_runtime.LocalRuntimeActivation(stored, deferred=True)
    activation.vault_projector = idle_service_projector(stored)
    created = [f"{NOTES}/Stopping {n}.md" for n in range(5)]
    commit(stored, puts=tuple(write.PagePut(rel, f"# {rel}\n".encode()) for rel in created))

    activation._stop_background_workers()

    assert queued(stored) == set()
    assert all((stored / rel).read_bytes() == f"# {rel}\n".encode() for rel in created)


def test_a_stop_drains_no_longer_than_its_bound_and_the_next_start_projects_the_rest(
    stored, service, monkeypatch
):
    """A stop whose drain outlasts its bound and holds up the service's shutdown, or that loses
    the intents it did not reach."""
    monkeypatch.setattr(projector, "DRAIN_ON_STOP_SECONDS", 0.3)
    activation = server_runtime.LocalRuntimeActivation(stored, deferred=True)
    activation.vault_projector = idle_service_projector(stored)
    created = [f"{NOTES}/Bounded {n:03d}.md" for n in range(150)]  # about 12 s of drain
    commit(stored, puts=tuple(write.PagePut(rel, f"# {rel}\n".encode()) for rel in created))

    began = time.monotonic()
    activation._stop_background_workers()
    stopped_in = time.monotonic() - began

    left = queued(stored)
    assert stopped_in < 4.0 and left and left <= set(created)
    projector.Projector(stored).run_pass()  # the next start
    assert queued(stored) == set()


def rollout_override():
    """The journey's `sitecustomize.py`, which opens the two store-mode rollout fences in a child
    process, as the store-mode fixtures open them in this one."""
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "scripts" / "e2e_product_loop.py"
    spec = importlib.util.spec_from_file_location("e2e_product_loop_for_replicas", script)
    journey = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = journey  # registered before it runs, as its dataclasses need
    spec.loader.exec_module(journey)
    return journey._ROLLOUT_OVERRIDE


REST_KEY = "replica-rest-key"


@contextmanager
def http_replica(root, tmp_path, name, *, activation_after=None):
    """One installed-shape HTTP service process on ``root``, with its own lease replica id.

    No request here calls the liveness route, so the service activates its background workers,
    the vault projector among them, on its fallback timer: after ``activation_after`` seconds
    when given, else the service's own default.
    """
    import socket
    import subprocess
    import urllib.request

    shim = tmp_path / f"rollout-{name}"
    shim.mkdir(exist_ok=True)
    override = rollout_override()
    if activation_after is not None:
        override += (
            "\n_OPENERS['exomem.server_runtime'] = lambda module: "
            "module.LocalRuntimeActivation.__init__.__kwdefaults__.update("
            f"fallback_seconds={float(activation_after)!r})\n"
        )
    (shim / "sitecustomize.py").write_text(override, encoding="utf-8")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, (str(shim), os.environ.get("PYTHONPATH")))),
        "EXOMEM_REST_API_KEY": REST_KEY,
        "EXOMEM_WRITER_LEASE_REPLICA_ID": name,
        "EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp_path / f"lease-{name}"),
        "EXOMEM_WRITER_LEASE_PREFERRED": "0",
        "EXOMEM_LOG_DIR": str(tmp_path / f"logs-{name}"),
        "EXOMEM_DISABLE_EMBEDDINGS": "1",
        "EXOMEM_DISABLE_MEDIA_EXTRACTION": "1",
        "EXOMEM_DISABLE_FILE_WATCHER": "1",
        "EXOMEM_DISABLE_WARMUP": "1",
    }
    log_path = tmp_path / f"{name}.log"
    script = Path(__file__).resolve().parents[1] / "scripts" / "e2e_http_server.py"
    with log_path.open("w", encoding="utf-8") as console:
        process = subprocess.Popen(
            [sys.executable, str(script), "--host", "127.0.0.1", "--port", str(port)],
            env=env, stdout=console, stderr=subprocess.STDOUT,
        )
        url = f"http://127.0.0.1:{port}"
        replica = SimpleNamespace(process=process, url=url, log=log_path)

        def post(route, body):
            request = urllib.request.Request(
                f"{url}/api/{route}", data=json.dumps(body).encode(), method="POST",
                headers={"Authorization": f"Bearer {REST_KEY}", "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as answer:
                    return json.loads(answer.read())
            except urllib.error.HTTPError as refused:
                return json.loads(refused.read())

        replica.post = post
        try:
            deadline = time.monotonic() + 120
            while True:
                assert process.poll() is None, log_path.read_text(encoding="utf-8")[-3000:]
                try:
                    urllib.request.urlopen(f"{url}/api/openapi.json", timeout=1).close()
                    break
                except OSError:
                    assert time.monotonic() < deadline, "the replica did not start"
                    time.sleep(0.1)
            yield replica
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(60)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(10)


def remember(replica, title):
    return replica.post(
        "remember",
        {"content": f"# {title}\n\n## Claim\n\nWriter-lease serialization.\n", "title": title,
         "note_type": "insight", "status": "draft"},
    )


@pytest.mark.timeout(600)
def test_a_service_stopped_before_it_activated_still_projects_the_write_it_accepted(
    stored, tmp_path
):
    """A service that accepts a write before its activation starts the vault projector, then
    stops: a stop that drains only a started projector leaves the write's projections queued, so
    the person's page has no file until a service starts again."""
    with http_replica(stored, tmp_path, "unactivated", activation_after=600) as service:
        written = remember(service, "Accepted before activation")
        assert written["success"] is True, written
        service.process.terminate()
        service.process.wait(60)
    left = store_rows(stored, "SELECT path, revision, queued_at FROM store_projection_queue")
    assert left == [], (left, service.log.read_text(encoding="utf-8")[-6000:])
    page = written["data"]["path"]
    assert (stored / page).read_bytes() == read.owned_bytes(stored, page)
    assert service.process.returncode == -signal.SIGTERM


@pytest.mark.timeout(600)
def test_a_second_replica_that_takes_the_lease_and_stops_at_once_leaves_nothing_queued(
    stored, tmp_path
):
    """Two services on one vault: the first writes and stops, the second takes the lease over with
    its own write and stops right after it. A stop that leaves that write's projections queued
    leaves files behind the store until a service starts again, and an offline rollback refuses."""
    with http_replica(stored, tmp_path, "replica-a") as first:
        with http_replica(stored, tmp_path, "replica-b") as second:
            assert remember(first, "Lease writer A")["success"] is True
            refused = remember(second, "Lease follower B")
            assert refused["error"]["code"] == "WRITER_LEASE_REQUIRED", refused
            first.process.terminate()
            first.process.wait(60)
            deadline = time.monotonic() + 30
            while not (taken := remember(second, "Lease takeover B")).get("success"):
                assert time.monotonic() < deadline, taken
                time.sleep(0.25)
            second.process.terminate()
            second.process.wait(60)
    left = store_rows(stored, "SELECT path, revision, queued_at FROM store_projection_queue")
    assert left == [], (left, second.log.read_text(encoding="utf-8")[-6000:])
    # Each ends by the signal, as a supervisor that stops it expects.
    assert first.process.returncode == second.process.returncode == -signal.SIGTERM


def idle(service_state):
    """Wait until no process of the replica holds a command claim on its grant, as when a service
    has finished its last request and the background work that request started."""
    from filelock import ReadWriteLock, Timeout

    (claim,) = service_state.glob("writer-grant-*.db")
    deadline = time.monotonic() + 90
    while True:
        lock = ReadWriteLock(claim, is_singleton=False)
        try:
            lock.acquire_write(blocking=False)
            lock.release()
            return
        except Timeout:
            assert time.monotonic() < deadline, "the service never became idle"
            time.sleep(0.25)
        finally:
            lock.close()


@pytest.mark.timeout(600)
def test_an_idle_service_writes_after_a_cli_command_of_its_replica_hands_back_their_grant(
    vault, tmp_path  # noqa: F811
):
    """A service and a one-shot CLI command with one replica id share one grant. The command hands
    it back as it exits while the service is idle: a service that keeps trusting the grant refuses
    its next page or record write with a lease error, or stops serving its collection store."""
    import subprocess

    from test_collection_store_writer import CID

    client = writer_lease.LeaseCoordinatorClient(writer_lease.LeaseConfig.from_env())
    with http_replica(vault, tmp_path, "shared") as service:
        assert remember(service, "Before the command")["success"] is True
        # A command that exits while one of the service's background steps holds a claim keeps the
        # grant, as designed; the case needs an exit that lands while the service is idle.
        for attempt in range(5):
            idle(tmp_path / "lease-shared")
            command = subprocess.run(
                [sys.executable, "-m", "exomem", "remember", "--title", f"From command {attempt}",
                 "--content", "# From the command\n\n## Claim\n\nA one-shot write.\n",
                 "--field", "note_type=insight", "--field", "status=draft", "--json"],
                env={**os.environ, "EXOMEM_WRITER_LEASE_REPLICA_ID": "shared",
                     "EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp_path / "lease-shared"),
                     "EXOMEM_WRITER_LEASE_PREFERRED": "0", "EXOMEM_DISABLE_EMBEDDINGS": "1"},
                capture_output=True, text=True, timeout=300,
            )
            assert command.returncode == 0, command.stdout[-2000:] + command.stderr[-2000:]
            if client.status().holder is None:  # the command handed the shared grant back
                break
        else:
            pytest.fail("no command exited while the service was idle")

        assert remember(service, "After the command")["success"] is True
        appended = service.post("record_memory", {
            "action": "append", "collection": CID, "item": {"title": "After the command"},
            "why": "a record write after the command",
        })
        assert appended["success"] is True, appended
        rows = service.post("record_memory", {"action": "query", "collection": CID})
        assert "After the command" in [row["title"] for row in rows["data"]["rows"]], rows


STARTS_AND_WAITS = """
import asyncio, sys
from contextlib import asynccontextmanager
from pathlib import Path
from exomem.server import ExomemFastMCP

started = Path(sys.argv[1])

@asynccontextmanager
async def lifespan(_server):
    started.write_text("starting")
    while started.exists():  # a startup that awaits, as the schema publication can
        await asyncio.sleep(0.05)
    yield
    started.with_suffix(".torn-down").touch()

ExomemFastMCP("startup", lifespan=lifespan).run(
    transport="http", host="127.0.0.1", port=0, show_banner=False
)
"""


def test_a_sigterm_while_the_http_service_starts_ends_it_by_the_signal(tmp_path):
    """A SIGTERM that lands while the lifespan's startup awaits: met at once, it ends the process
    without the teardown of what the startup began, or, raised as an exception that lands in the
    event loop, with 1, which a supervisor reads as a crash."""
    import subprocess

    started = tmp_path / "started"
    process = subprocess.Popen(
        [sys.executable, "-c", STARTS_AND_WAITS, str(started)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 120
        while not started.exists():
            assert process.poll() is None, process.stdout.read().decode()[-3000:]
            assert time.monotonic() < deadline, "the service did not start its lifespan"
            time.sleep(0.05)
        process.terminate()
        started.unlink()  # the startup finishes
        output, _ = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)
    torn_down = started.with_suffix(".torn-down").exists()
    assert (torn_down, process.returncode) == (True, -signal.SIGTERM), output.decode()[-3000:]


SERVES_UNDER_AN_EARLIER_ACTION = """
import asyncio, signal, sys, threading
from contextlib import asynccontextmanager
from pathlib import Path

marks, port, action, startup = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]
starting, raised = marks.with_suffix(".starting"), marks.with_suffix(".raised")
blocked = startup in ("masked", "all blocked")
if blocked:  # the main thread blocks SIGTERM, as an embedding caller can, and so every later thread
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
from exomem.server import ExomemFastMCP

def mark(word):
    with marks.open("a", encoding="utf-8") as out:
        out.write(word + "\\n")

joining = threading.Event()

def raise_on_a_worker():
    if startup == "all blocked":
        joining.wait()
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
    signal.raise_signal(signal.SIGTERM)
    if startup == "all blocked":
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    raised.touch()
    if startup == "masked":
        threading.Event().wait()  # it stays, a thread that takes the process's SIGTERM

def replacing(_signum, _frame):
    mark("handler")
    signal.signal(signal.SIGTERM, signal.SIG_DFL)

@asynccontextmanager
async def lifespan(_server):
    if startup != "free":  # the startup awaits until the test removes the file
        starting.touch()
        if blocked:
            worker = threading.Thread(target=raise_on_a_worker, daemon=True)
            switching = sys.getswitchinterval()
            if startup == "all blocked":
                # No forced thread switch: the worker raises only once the main thread waits in
                # the join, so the main thread meets the signal once every thread blocks it.
                sys.setswitchinterval(1e6)
            worker.start()
            if startup == "all blocked":
                joining.set()
                worker.join()
                sys.setswitchinterval(switching)
        while starting.exists():
            await asyncio.sleep(0.05)
        if blocked:
            if signal.SIGTERM in signal.sigpending():
                mark("pending")
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
    mark("started")
    yield
    mark("torn down")

earlier = {"handler": lambda _signum, _frame: mark("handler"), "replacing": replacing,
           "ignore": signal.SIG_IGN, "default": signal.SIG_DFL}[action]
signal.signal(signal.SIGTERM, earlier)
ExomemFastMCP("earlier", lifespan=lifespan).run(
    transport="http", host="127.0.0.1", port=port, show_banner=False
)
mark("returned")
"""


@pytest.mark.parametrize(
    ("action", "startup", "marks", "status"),
    [
        # The handler runs once and the runner returns, as on main; main ran it before the outer
        # teardown, and here it runs after the teardown.
        ("handler", "free", ["started", "torn down", "handler", "returned"], 0),
        # As on main: the ignored signal is ignored, and the runner returns.
        ("ignore", "free", ["started", "torn down", "returned"], 0),
        # Main ends here without the teardown; this change runs it, then ends by the signal.
        ("default", "free", ["started", "torn down"], -signal.SIGTERM),
        # A SIGTERM while the startup awaits stops the service once the startup has finished: the
        # teardown runs, then the handler, once.
        ("handler", "held", ["started", "torn down", "handler", "returned"], 0),
        # Also when the handler replaces the action as it runs.
        ("replacing", "held", ["started", "torn down", "handler", "returned"], 0),
        # So does a worker's SIGTERM while the main thread blocks it.
        ("handler", "masked", ["started", "torn down", "handler", "returned"], 0),
        ("default", "masked", ["started", "torn down"], -signal.SIGTERM),
        # And one that a worker raised before every thread blocked it.
        ("default", "all blocked", ["started", "torn down"], -signal.SIGTERM),
    ],
)
def test_a_sigterm_meets_the_action_installed_before_the_http_service_ran(
    tmp_path, action, startup, marks, status
):
    """A service that stops on SIGTERM without its teardown, or whose stop drops the action that
    its caller installed: the handler never runs or runs before the teardown, or an ignored signal
    ends the process. A SIGTERM while the startup awaits is the same stop once the startup has
    finished, also when a worker raised it while the main thread or every thread blocks it. Met at
    once, it skips the teardown, waits pending, or leaves a service that serves on and whose
    handler may have replaced the action that the stop meets."""
    import socket
    import subprocess

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    written = tmp_path / "marks"
    process = subprocess.Popen(
        [sys.executable, "-c", SERVES_UNDER_AN_EARLIER_ACTION, str(written), str(port), action,
         startup],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )

    def marked():
        return written.read_text(encoding="utf-8").splitlines() if written.exists() else []

    def listens():
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return True
        except OSError:
            return False

    def until(condition, failure):
        """Whether ``condition`` came true; False once the process ended, as the final assertion
        then shows."""
        deadline = time.monotonic() + 120
        while not condition():
            if process.poll() is not None:
                return False
            assert time.monotonic() < deadline, failure
            time.sleep(0.05)
        return True

    try:
        starting = written.with_suffix(".starting")
        if startup == "held":
            until(starting.exists, "the startup did not begin")
            process.terminate()
        elif startup != "free":
            until(written.with_suffix(".raised").exists, "the worker raised no SIGTERM")
        starting.unlink(missing_ok=True)
        serves = until(listens, "the service did not start")  # it listens once it took the signals
        if serves and (startup == "free" or marked()[:1] == ["handler"]):
            process.terminate()  # a runner that met the startup's SIGTERM at once serves on
        output, _ = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)
    assert (marked(), process.returncode) == (marks, status), output.decode()[-3000:]


ENDS_WITHOUT_UVICORNS_SHUTDOWN = """
import asyncio, sys
from contextlib import asynccontextmanager
import uvicorn
from exomem.server import ExomemFastMCP

ending, teardown, marks = sys.argv[1], sys.argv[2], []
tearing = asyncio.Event()

@asynccontextmanager
async def lifespan(_server):
    marks.append("started")
    yield
    marks.append("tearing down")
    tearing.set()
    await asyncio.sleep(0.2)  # a teardown that awaits, as the stop drain does
    if teardown == "fails":  # as a worker that does not stop
        raise RuntimeError("the teardown failed")
    marks.append("torn down")

async def main():
    serving, serve = asyncio.Event(), uvicorn.Server.main_loop

    async def main_loop(server):
        serving.set()
        if ending == "the server loop raises":
            raise ValueError("the server loop failed")
        if ending == "the exit is forced":  # as a second SIGINT does
            server.force_exit = True
            return
        await serve(server)

    uvicorn.Server.main_loop = main_loop
    runner = asyncio.create_task(ExomemFastMCP("embedded", lifespan=lifespan).run_http_async(
        transport="http", host="127.0.0.1", port=0, show_banner=False))
    if ending.startswith("the caller cancels"):
        await serving.wait()
        runner.cancel()
    if ending == "the caller cancels twice":
        await tearing.wait()
        runner.cancel()
    try:
        await runner
    except BaseException as error:
        marks.append(type(error).__name__)
        if teardown == "fails":
            marks.append(f"context {type(error.__context__).__name__}")
    await asyncio.sleep(0.5)  # the caller's event loop lives on

asyncio.run(main())
print(marks)  # after the loop closed, so a teardown left to that close shows last
"""


@pytest.mark.parametrize(
    ("ending", "teardown", "raised"),
    [
        ("the caller cancels", "returns", ["torn down", "CancelledError"]),
        ("the server loop raises", "returns", ["torn down", "ValueError"]),
        # The runner stays until the teardown has ended, and then raises the cancellation.
        ("the caller cancels twice", "returns", ["torn down", "CancelledError"]),
        # A failed teardown reaches the caller, with how the runner ended as its context.
        ("the caller cancels", "fails", ["RuntimeError", "context CancelledError"]),
        ("the server loop raises", "fails", ["RuntimeError", "context ValueError"]),
        ("the exit is forced", "fails", ["RuntimeError", "context NoneType"]),
    ],
)
def test_an_http_runner_that_ends_without_uvicorns_shutdown_tears_down_before_it_raises(
    ending, teardown, raised
):
    """An embedding caller whose awaited runner was cancelled or failed while the teardown has not
    run: the workers and the writer lease outlive the runner until the event loop closes, the
    teardown runs twice, a second cancellation returns before the teardown has ended, or the
    caller sees no failure of the teardown and takes the workers as stopped."""
    import subprocess

    finished = subprocess.run(
        [sys.executable, "-c", ENDS_WITHOUT_UVICORNS_SHUTDOWN, ending, teardown],
        capture_output=True, text=True, timeout=120,
    )
    lines = finished.stdout.splitlines()
    expected = ["started", "tearing down", *raised]
    assert lines[-1:] == [repr(expected)], finished.stderr[-3000:]


def trashed_once(root, rel, body):
    """Where a trashed binary's bytes are now: every file under the trash that holds them."""
    return [
        path.relative_to(root).as_posix()
        for path in (root / "Knowledge Base/_trash").rglob("*")
        if path.is_file() and path.read_bytes() == body
    ]


def test_a_served_projector_that_renames_a_trashed_binary_before_its_writer_does_converges(
    stored, service, monkeypatch
):
    """The writer's rename after its commit racing a served projector that renames the same file
    first: the trash refuses or warns, the file ends at both paths or neither, or a record or an
    intent is left naming the wrong path."""
    projector_moved = threading.Event()
    real_rename = binaries._rename

    def after_the_projector(root, entry, move, seen):
        if entry.src == DIAGRAM:  # the writer waits until the served projector has moved the file
            deadline = time.monotonic() + 10
            while (stored / DIAGRAM).exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not (stored / DIAGRAM).exists():
                projector_moved.set()
        return real_rename(root, entry, move, seen)

    monkeypatch.setattr(binaries, "_rename", after_the_projector)
    loop = idle_service_projector(stored, poll_seconds=0.02)
    try:
        result = call(
            stored, "manage_memory_file", operation="delete", path=DIAGRAM, confirm=True,
            force_orphan=True,
        )
        deadline = time.monotonic() + 30
        while queued(stored):
            assert time.monotonic() < deadline, "the projector drained the queue"
            time.sleep(0.05)
    finally:
        loop.stop()

    assert projector_moved.is_set() and not (stored / DIAGRAM).exists(), result
    (trashed,) = trashed_once(stored, DIAGRAM, DIAGRAM_BYTES)
    assert result.get("ok") is not False and not result.get("warnings")
    assert store_rows(stored, "SELECT sha256 FROM store_binaries WHERE path = ?", trashed) == [
        (sha(DIAGRAM_BYTES),)
    ]
    assert store_rows(stored, "SELECT path FROM store_binaries WHERE path = ?", DIAGRAM) == []
    assert store_rows(stored, "SELECT path FROM store_held") == []


def test_a_writer_that_renames_a_trashed_binary_while_the_projector_is_about_to_converges(
    stored, service, monkeypatch, caplog
):
    """The served projector judging a queued binary move, then finding that the writer moved the
    file between its look and its rename: it defers the move, logs it, or renames something."""
    picture = f"{NOTES}/raced.png"
    (stored / picture).write_bytes(PICTURE_BYTES)
    importer.Importer(stored).import_paths([picture])
    at_rename, writer_moved = threading.Event(), threading.Event()
    real_rename, real_move = projector._rename, reserved_paths.move_generic_path

    def renaming_late(filesystem, old, new, expected):
        if old == picture:  # the projector has judged the move; the writer now moves the file
            at_rename.set()
            writer_moved.wait(10)
        return real_rename(filesystem, old, new, expected)

    def renaming_first(root, source, destination, **options):
        if source == picture:
            at_rename.wait(10)
            try:
                return real_move(root, source, destination, **options)
            finally:
                writer_moved.set()
        return real_move(root, source, destination, **options)

    monkeypatch.setattr(projector, "_rename", renaming_late)
    monkeypatch.setattr(reserved_paths, "move_generic_path", renaming_first)
    caplog.set_level("WARNING", logger="exomem.vault_store")
    loop = idle_service_projector(stored, poll_seconds=0.02)
    try:
        result = call(
            stored, "manage_memory_file", operation="delete", path=picture, confirm=True,
            force_orphan=True,
        )
        deadline = time.monotonic() + 30
        while queued(stored):
            assert time.monotonic() < deadline, "the projector drained the queue"
            time.sleep(0.05)
    finally:
        loop.stop()

    assert at_rename.is_set() and writer_moved.is_set(), result
    (trashed,) = trashed_once(stored, picture, PICTURE_BYTES)
    assert result.get("ok") is not False and not (stored / picture).exists()
    assert store_rows(stored, "SELECT sha256 FROM store_binaries WHERE path = ?", trashed) == [
        (sha(PICTURE_BYTES),)
    ]
    assert store_rows(stored, "SELECT path FROM store_binaries WHERE path = ?", picture) == []
    assert store_rows(stored, "SELECT path FROM store_held") == []
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


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
        # The waits are sampled back to back, so their count follows the drain's length in samples
        # of a few milliseconds, not in fixed sleeps. The two writes come after the first three
        # samples: a write that ran first, or beside the samples, could outlast a fast drain.
        while set(drained_pages) & queued(stored):
            assert time.monotonic() - began < 780, "the drain finished"
            if len(during_waits) >= 3 and len(during) < 2:
                during.append(foreground(f"during {len(during)}"))
            during_waits.append(waits())
        drain_seconds = time.monotonic() - began
    finally:
        loop.stop()
    projection = holds.get("vault_store_projection", [])
    assert len(projection) >= intents, "one hold for each record, none for the whole drain"
    assert len(during_waits) >= 3, "foreground waits were measured while the projector drained"

    # One importer batch: an outside edit of as many pages as one store-thread job settles.
    batch = drained_pages[: importer.APPLY_BATCH]
    for rel in batch:
        (stored / rel).write_bytes(f"# {rel}\n\nEdited outside.\n".encode())
    imported = importer.Importer(stored).import_paths(batch)

    def ms(values, how):
        return round(how(values) * 1000, 1)

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
    assert sorted(imported.imported) == sorted(batch)
    # A hold is one record or one batch: far inside the timeout that a foreground writer waits.
    timeout = get_manager()._mutation_timeout_seconds
    assert max(max(times) for times in holds.values()) < timeout / 4
    assert max(b_busy) < timeout / 4 and max(q_busy) < timeout / 4
