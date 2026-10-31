"""The media supervisor's call site of the node media-slot lease, over a real lease service."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading

import pytest
from test_media_slot_lease import (
    BIG,
    LEASE_SECONDS,
    SMALL,
    A,
    B,
    Clock,
    _config,
    _listening,
    _one_address_per_cell,
)
from test_media_worker import _cloud_brakes, _preserve_media_stub, _run_supervisor_for, _wait_until

from exomem import (
    cuda_containment,
    extract,
    media_jobs,
    media_slot_lease,
    media_worker,
    media_worker_child,
)
from exomem.media_slot_client import CELL_ID_ENV, URL_ENV, SlotClient

TOWER = 512  # MiB the CLIP tower reserves beside the stage's engine
BUDGETS = {
    "speech": {"anon_mib": BIG, "vmdata_mib": 4096},
    "clip": {"anon_mib": TOWER, "vmdata_mib": 1792},
    "default": {"anon_mib": SMALL, "vmdata_mib": 1024},
}


@pytest.fixture(autouse=True)
def _cloud_cell(vault, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state"))
    monkeypatch.setenv("EXOMEM_MEDIA_BUDGETS", json.dumps(BUDGETS))
    _cloud_brakes(monkeypatch, {"anon": 1 << 30, "pressure": 0.0})
    monkeypatch.setattr(media_worker, "_probe_writer_authority", lambda: None)
    monkeypatch.setenv(CELL_ID_ENV, A)


def _enqueue(vault, filename: str, media_type: str, **stages: bool) -> None:
    result = _preserve_media_stub(vault, filename=filename)
    media_jobs.MediaJobStore(vault).enqueue(
        media_jobs.MediaJob(
            binary_path=vault / result.path,
            sidecar_path=vault / result.sidecar_path,
            media_type=media_type,
            **stages,
        )
    )


class _Child:
    """Stands in for a media child that runs until `ended` is set, or has already exited."""

    pid = 4_194_000

    def __init__(self, returncode: int | None = None) -> None:
        self.ended = threading.Event()
        self.returncode = returncode

    def poll(self) -> int | None:
        if self.ended.is_set():
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.ended.set()

    def wait(self, timeout) -> None:
        pass


def test_an_unreachable_slot_service_leaves_the_job_pending_and_counts_no_stop(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enqueue(vault, "waits.mp3", "audio")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv(URL_ENV, f"http://127.0.0.1:{port}")  # nothing listens here
    launches: list[int] = []
    monkeypatch.setattr(media_worker.MediaWorker, "_launch_child", lambda self: launches.append(1) or None)

    _run_supervisor_for(media_worker.MediaWorker(vault, execution_mode="process"), 1.5)

    status = media_jobs.status(vault)
    with sqlite3.connect(media_jobs.job_store_path(vault)) as db:
        stops = db.execute("SELECT memory_stops, timeout_stops FROM jobs").fetchall()
    assert launches == []
    assert (status["jobs"][0]["state"], status["memory_blocked_count"], stops) == (media_jobs.PENDING, 0, [(0, 0)])


def _supervised_against_a_lease(vault, monkeypatch, children):
    """A supervisor whose children are `children`'s results, asking a real lease service."""
    clock = Clock()
    service = media_slot_lease.MediaSlotLease(_config(), clock)
    leases: list[int | None] = []

    def launch(self):
        leases.append(self._leased_anon_bytes)
        return children(self)

    monkeypatch.setattr(media_worker.MediaWorker, "_launch_child", launch)
    return _listening(_one_address_per_cell(service)), clock, leases


def test_the_worker_holds_a_slot_for_its_child_and_gives_it_back_when_the_child_exits(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enqueue(vault, "waits.mp3", "audio")
    child = _Child()
    listening, clock, leases = _supervised_against_a_lease(
        vault, monkeypatch, lambda _worker: child if len(leases) == 1 else None
    )
    with listening as (host, port):
        clock.now += LEASE_SECONDS  # past the service's restart hold
        monkeypatch.setenv(URL_ENV, f"http://{host}:{port}")
        other = SlotClient(f"http://{host}:{port}", B)  # another cell; speech fills the pool
        worker = media_worker.MediaWorker(vault, execution_mode="process")
        thread = threading.Thread(target=worker._supervise, daemon=True)
        thread.start()
        try:
            assert _wait_until(lambda: leases, seconds=5.0), "the granted worker never started its child"
            assert other.acquire("speech", BIG << 20, 900) is False  # the child holds the pool
            child.ended.set()
            freed = _wait_until(lambda: other.acquire("speech", BIG << 20, 900), seconds=10.0)
        finally:
            worker._stop_event.set()
            worker._wake.set()
            thread.join(timeout=5)

    assert freed, "the worker kept its slot after its child exited"


def test_a_job_that_loads_the_clip_tower_reserves_the_tower_as_well(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The tower's memory is real: a slot sized for the stage engine alone would let another
    # cell take the room that the tower fills.
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)  # the suite turns the tower off
    _enqueue(vault, "photo.png", "image", do_ocr=True, do_clip=True)
    child = _Child()
    listening, clock, leases = _supervised_against_a_lease(vault, monkeypatch, lambda _worker: child)
    with listening as (host, port):
        clock.now += LEASE_SECONDS
        monkeypatch.setenv(URL_ENV, f"http://{host}:{port}")
        worker = media_worker.MediaWorker(vault, execution_mode="process")
        thread = threading.Thread(target=worker._supervise, daemon=True)
        thread.start()
        try:
            assert _wait_until(lambda: leases, seconds=5.0), "the granted worker never started its child"
            other = SlotClient(f"http://{host}:{port}", B)
            # 640 (the stage) + 512 (the tower) leaves 128 of 1280: a second 640 would fit
            # beside the stage alone, but not beside the stage and the tower.
            granted = other.acquire("default", SMALL << 20, 900)
        finally:
            child.ended.set()
            worker._stop_event.set()
            worker._wake.set()
            thread.join(timeout=5)

    assert (leases, granted) == ([(SMALL + TOWER) << 20], False)


def _interrupted_speech_job_and_a_waiting_document(vault) -> media_jobs.MediaJobStore:
    """A speech job that a cell restart left running, and a lighter job queued after it."""
    _enqueue(vault, "interrupted.mp3", "audio")
    _enqueue(vault, "waiting.pdf", "pdf")
    store = media_jobs.MediaJobStore(vault)
    assert store.claim_next() is not None  # the speech job, then the cell restarted
    assert store.next_pending().media_type == "pdf"  # all a supervisor can see before a child runs
    return store


@pytest.fixture
def _child_runs_unattended(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)
    monkeypatch.setattr(media_worker, "_writer_authority_available", lambda: True)
    monkeypatch.setattr(
        media_worker.MediaWorker, "_process", lambda _self, _job: media_worker._ProcessOutcome("complete")
    )


def test_a_recovered_heavier_job_is_not_run_under_a_smaller_slot(vault, _child_runs_unattended) -> None:
    store = _interrupted_speech_job_and_a_waiting_document(vault)

    code = media_worker.run_child(
        vault, parent_pid=os.getpid(), idle_seconds=0.01, leased_anon_bytes=SMALL << 20
    )

    with sqlite3.connect(media_jobs.job_store_path(vault)) as db:
        jobs = db.execute("SELECT state, memory_stops, timeout_stops FROM jobs ORDER BY id").fetchall()
    assert code == media_worker._SLOT_TOO_SMALL_EXIT_CODE
    assert jobs == [("pending", 0, 0), ("pending", 0, 0)]  # nothing ran and no stop counted
    assert store.next_pending().media_type == "audio"  # the supervisor now sizes for the speech job


def test_a_supervisor_that_sized_a_slot_too_small_gives_it_back_and_asks_again_for_the_right_one(
    vault, monkeypatch: pytest.MonkeyPatch, _child_runs_unattended
) -> None:
    store = _interrupted_speech_job_and_a_waiting_document(vault)
    exits: list[int] = []

    def child_in_process(worker):
        code = media_worker.run_child(
            vault, parent_pid=os.getpid(), idle_seconds=0.01, leased_anon_bytes=worker._leased_anon_bytes
        )
        exits.append(code)
        return _Child(returncode=code)

    listening, clock, leases = _supervised_against_a_lease(vault, monkeypatch, child_in_process)
    with listening as (host, port):
        clock.now += LEASE_SECONDS
        monkeypatch.setenv(URL_ENV, f"http://{host}:{port}")
        worker = media_worker.MediaWorker(vault, execution_mode="process")
        thread = threading.Thread(target=worker._supervise, daemon=True)
        thread.start()
        try:
            ran = _wait_until(lambda: store.next_pending() is None and not store.running_jobs(), seconds=15.0)
        finally:
            worker._stop_event.set()
            worker._wake.set()
            thread.join(timeout=5)

    assert ran, "the interrupted speech job never ran"
    # The first slot fitted the waiting document; the speech job needed its own.
    assert leases[:2] == [SMALL << 20, BIG << 20]
    assert exits[:2] == [media_worker._SLOT_TOO_SMALL_EXIT_CODE, 0]


def test_the_child_is_started_with_the_bytes_its_slot_reserves(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[list[str]] = []
    monkeypatch.setattr(media_worker.subprocess, "Popen", lambda args, **_kw: started.append(args))
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    worker._leased_anon_bytes = BIG << 20
    worker._gpu_preflight = cuda_containment.Preflight(True)  # the host's GPU is not under test

    worker._launch_child()

    assert media_worker_child.arguments(started[0][3:]).leased_anon_bytes == BIG << 20
