"""Resumable Cloud speech: the worker keeps a stopped transcript and drops it with its job.

The engine is stubbed at its boundary: a fake that leaves the progress record the real
engine writes after each 30 s window. The ledger, the supervisor and the child loop are real.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from exomem import extract, media_brakes, media_jobs, media_worker, onnx_asr_runtime, preserve

_WINDOW = 16_000 * 30  # samples in one engine window


def _enqueue_speech(vault: Path):
    result = preserve.preserve_bytes(
        vault, scope="Rental", category="audio", filename="talk.mp3", data=b"first recording"
    )
    binary, sidecar = vault / result.path, vault / result.sidecar_path
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(media_jobs.MediaJob(binary_path=binary, sidecar_path=sidecar, media_type="audio"))
    return store, binary, sidecar


def _transcribed(vault: Path, binary: Path, windows: int) -> Path:
    """Leave the progress record the engine writes after `windows` windows of `binary`."""
    path = onnx_asr_runtime._progress_path(vault, onnx_asr_runtime.binary_rel(vault, binary))
    onnx_asr_runtime._write_progress(
        vault,
        path,
        {
            "format": onnx_asr_runtime.PROGRESS_FORMAT,
            "input_sha256": onnx_asr_runtime._file_sha256(binary),
            "engine": "fake",
            "language": "en",
            "next_offset": windows * _WINDOW,
            "segments": [],
        },
    )
    return path


def _cloud_brakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media_brakes, "enabled", lambda env=None: True)
    monkeypatch.setattr(
        media_brakes,
        "read_cell",
        lambda **_: media_brakes.CellMemory(anon=1 << 30, max=3 << 30, high=None, pressure_avg10=0.0),
    )
    monkeypatch.setattr(media_brakes, "apply_hard_limit", lambda budget, config: None)
    monkeypatch.setattr(media_brakes, "lift_hard_limit", lambda: None)


def _child_loop_runs_unattended(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)
    monkeypatch.setattr(media_worker, "_writer_authority_available", lambda: True)


def _run_child(vault: Path) -> int:
    return media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.01)


def _hanging_child() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)


def _supervised(vault: Path, monkeypatch: pytest.MonkeyPatch, launch) -> media_worker.MediaWorker:
    """A real supervisor whose children hang past a 1 s watchdog; `launch` claims and starts one."""
    _cloud_brakes(monkeypatch)
    monkeypatch.setenv("EXOMEM_MEDIA_JOB_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr(media_worker, "_probe_writer_authority", lambda: None)
    monkeypatch.setattr(media_worker, "_SUPERVISE_POLL_SECONDS", 0.05)
    monkeypatch.setattr(media_worker, "_MEMORY_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(media_worker, "_MEMORY_RECOVERY_SECONDS", 1.0)
    monkeypatch.setattr(media_worker.MediaWorker, "_launch_child", launch)
    return media_worker.MediaWorker(vault, execution_mode="process")


def _wait_until(predicate, *, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_a_timeout_stop_that_advanced_the_transcript_spends_no_stop(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A long recording needs more 15-minute runs than the stop limit; each run saves
    # more of it, so none of its stops may count.
    store, binary, _sidecar = _enqueue_speech(vault)
    children: list[subprocess.Popen] = []
    claims: list[int] = []

    def launch(self):
        assert self._store.claim_next() is not None
        claims.append(len(claims) + 1)
        _transcribed(vault, binary, claims[-1])
        children.append(_hanging_child())
        self._store.set_worker(children[-1].pid, 1.0)
        return children[-1]

    worker = _supervised(vault, monkeypatch, launch)
    worker.start()
    try:
        ran_past_the_limit = _wait_until(lambda: len(claims) >= 5, seconds=40.0)
    finally:
        worker.stop()
        for child in children:
            child.kill()

    assert ran_past_the_limit, f"claims {claims}; jobs {media_jobs.status(vault)['jobs']}"
    assert media_jobs.status(vault)["over_budget_count"] == 0


def test_timeout_stops_without_progress_still_end_over_budget_at_the_stop_limit(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The engine saves two windows on the first run and then hangs without saving more.
    store, binary, _sidecar = _enqueue_speech(vault)
    children: list[subprocess.Popen] = []
    claims: list[int] = []

    def launch(self):
        assert self._store.claim_next() is not None
        if not claims:
            _transcribed(vault, binary, 2)
        claims.append(len(claims) + 1)
        children.append(_hanging_child())
        self._store.set_worker(children[-1].pid, 1.0)
        return children[-1]

    worker = _supervised(vault, monkeypatch, launch)
    worker.start()
    try:
        settled = _wait_until(
            lambda: store.pending_result_count() == 0
            and media_jobs.status(vault)["over_budget_count"] == 1,
            seconds=40.0,
        )
    finally:
        worker.stop()
        for child in children:
            child.kill()

    assert settled, f"claims {claims}; jobs {media_jobs.status(vault)['jobs']}"
    # The advancing first stop is free; the default three stops without progress follow.
    assert len(claims) == 4


def test_a_pressure_stop_that_advanced_the_transcript_spends_no_stop(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cloud_brakes(monkeypatch)
    monkeypatch.setenv(media_brakes.STOP_LIMIT_ENV, "2")
    store, binary, _sidecar = _enqueue_speech(vault)
    worker = media_worker.MediaWorker(vault, execution_mode="process")

    for windows in (1, 2, 3):
        assert store.claim_next() is not None
        _transcribed(vault, binary, windows)
        worker._stop_child(_hanging_child(), media_brakes.PRESSURE)

    [row] = media_jobs.status(vault)["jobs"]
    assert row["state"] == media_jobs.PENDING


def test_a_hard_limit_stop_that_advanced_the_transcript_spends_no_stop(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cloud_brakes(monkeypatch)
    _child_loop_runs_unattended(monkeypatch)
    monkeypatch.setenv(media_brakes.STOP_LIMIT_ENV, "2")
    _store, binary, _sidecar = _enqueue_speech(vault)
    runs: list[int] = []

    def out_of_memory_after_a_window(path: Path, **_kwargs):
        runs.append(len(runs) + 1)
        _transcribed(vault, path, runs[-1])
        raise MemoryError

    monkeypatch.setattr(extract, "extract_text", out_of_memory_after_a_window)

    exits = [_run_child(vault) for _ in range(3)]

    assert exits == [media_worker._MEMORY_RECYCLE_EXIT_CODE] * 3
    [row] = media_jobs.status(vault)["jobs"]
    assert row["state"] == media_jobs.PENDING


def _stopped_speech_job(vault: Path):
    """A speech job that stopped once with two windows saved."""
    store, binary, sidecar = _enqueue_speech(vault)
    job = store.claim_next()
    assert job is not None
    progress = _transcribed(vault, binary, 2)
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    store.record_timeout_stop(
        job, stop_limit=3, context="{}", progress=worker._speech_progress(job)
    )
    return binary, sidecar, progress


def test_a_purged_speech_job_discards_its_saved_transcript(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _child_loop_runs_unattended(monkeypatch)
    _binary, sidecar, progress = _stopped_speech_job(vault)
    sidecar.unlink()

    _run_child(vault)

    assert not progress.exists()


def test_a_speech_job_that_fails_for_good_discards_its_saved_transcript(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _child_loop_runs_unattended(monkeypatch)
    _binary, _sidecar, progress = _stopped_speech_job(vault)

    def corrupt(*_args, **_kwargs):
        raise ValueError("corrupt audio")

    monkeypatch.setattr(extract, "extract_text", corrupt)

    _run_child(vault)

    assert not progress.exists()


def test_a_worker_restart_mid_job_resumes_from_the_saved_offset(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _child_loop_runs_unattended(monkeypatch)
    _store, _binary, sidecar = _enqueue_speech(vault)
    resumed_from: list[int] = []

    def engine(path: Path, *, vault_root: Path, **_kwargs):
        rel = onnx_asr_runtime.binary_rel(vault_root, path)
        done = onnx_asr_runtime.progress_offset(vault_root, rel) // _WINDOW
        resumed_from.append(done)
        for window in range(done + 1, 7):
            _transcribed(vault_root, path, window)
            if len(resumed_from) == 1 and window == 3:
                raise SystemExit("the worker process is killed")
        onnx_asr_runtime.discard_progress(vault_root, rel)
        return extract.ExtractResult(text="six windows of speech", media_type="audio", engine="test")

    monkeypatch.setattr(extract, "extract_text", engine)

    with pytest.raises(SystemExit):
        _run_child(vault)
    assert _run_child(vault) == 0

    assert resumed_from == [0, 3]
    [result] = media_jobs.MediaJobStore(vault).pending_results()
    assert result.payload["text"] == "six windows of speech"
    assert sidecar.exists()
