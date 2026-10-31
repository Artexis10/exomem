"""The remote media worker against the real service routes, over real HTTP on a loopback listener.

The service is the real app, ledger and parent publication. Only the speech engine is absent: a test injects the
worker's compute, and the real-model proof is the real-machine run. A fault is injected at the transport, in front
of the real app. All names, tokens and bytes are synthetic.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import sys
import threading
import time
import types
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from test_media_worker_routes import (
    AUDIO,
    EXTRACTION,
    OTHER_TOKEN,
    REST_KEY,
    TOKEN,
    UPLOAD_TOKEN,
    _audio,
    _auth,
    _credential,
    _publish,
    _route,
    _row,
    _voiceprint,
)
from test_upload_sessions import _listening

from exomem import (
    __main__ as cli,
)
from exomem import (
    asr_runtime,
    cuda_containment,
    extract,
    media_engines,
    media_jobs,
    media_worker_client,
    server,
    server_media_worker,
)
from exomem.media_worker_client import Claimed, RemoteWorker, WorkerConfig

CAPABILITIES = {"media_types": ["audio", "video"], "engines": ["speech"], "device": "cpu", "diarize": False}
TRANSCRIPT = "[0:01] tere maailm"


@pytest.fixture(autouse=True)
def _runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state"))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")  # a proxy in the environment must not stand between worker and service
    monkeypatch.setenv(asr_runtime.CUDA_RUNTIME_READY_ENV, "1")  # a test never replaces its own process
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)
    monkeypatch.setattr(server_media_worker, "CLAIM_WAIT_SECONDS", 0.3)  # an idle claim answers soon
    monkeypatch.setattr(server_media_worker, "CLAIM_POLL_SECONDS", 0.05)


# ---- the service, the faults and the worker ----------------------------------------------------------


class _Spy:
    """The real app behind a wrapper that records every request and may break the one that `fault` picks."""

    def __init__(self, app, fault: Callable[[str], Callable | None] | None) -> None:
        self.app, self.fault = app, fault
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {name.decode(): value.decode() for name, value in scope["headers"]}
        self.requests.append((scope["method"], scope["path"], headers))
        broken = self.fault(scope["path"]) if self.fault else None
        if broken is None:
            return await self.app(scope, receive, send)
        await broken(self.app, scope, receive, send)

    def calls(self, suffix: str) -> list[dict[str, str]]:
        """The headers of every request to a path that ends with `suffix`."""
        return [headers for _method, path, headers in self.requests if path.endswith(suffix)]


@contextlib.contextmanager
def _service(vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, fault=None, **block):
    """The real app with audio routed to remote workers, listening on loopback; yields its URL and a `_Spy`."""
    _route(tmp_path, monkeypatch, vault, {"remote_workers": {"media_types": ["audio"], **block}})
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_UPLOAD_TOKEN", UPLOAD_TOKEN)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", REST_KEY)
    spy = _Spy(server.build_server(require_auth=False).http_app(), fault)
    with _listening(spy) as (host, port):
        yield f"http://{host}:{port}", spy


def _on_first_call(suffix: str, broken: Callable) -> Callable[[str], Callable | None]:
    """Break the first request to a path that ends with `suffix`, and none after it."""
    seen: list[str] = []

    def pick(path: str) -> Callable | None:
        if not path.endswith(suffix):
            return None
        seen.append(path)
        return broken if len(seen) == 1 else None

    return pick


def _cut_body_after(limit: int) -> Callable:
    """Send `limit` bytes of the body and then break the connection, as a network that drops mid-download does."""

    async def broken(app, scope, receive, send) -> None:
        async def send_part(message) -> None:
            if message["type"] == "http.response.body":
                await send({**message, "body": message["body"][:limit], "more_body": True})
                raise ConnectionAbortedError
            await send(message)

        await app(scope, receive, send_part)

    return broken


def _alter_body(change: Callable[[bytes], bytes]) -> Callable:
    """Send the body that `change` returns, with a length header that fits it, as a faulty middlebox does."""

    async def broken(app, scope, receive, send) -> None:
        start, body = {}, b""

        async def collect(message) -> None:
            nonlocal start, body
            if message["type"] == "http.response.start":
                start = message
            else:
                body += message.get("body", b"")

        await app(scope, receive, collect)
        altered = change(body)
        headers = [(name, value) for name, value in start["headers"] if name != b"content-length"]
        await send({**start, "headers": [*headers, (b"content-length", str(len(altered)).encode())]})
        await send({"type": "http.response.body", "body": altered})

    return broken


async def _lose_the_answer(app, scope, receive, send) -> None:
    """The service does the work, and then the connection breaks before the worker reads the answer."""

    async def discard(_message) -> None:
        return None

    await app(scope, receive, discard)
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"100")]})
    await send({"type": "http.response.body", "body": b"{", "more_body": True})
    raise ConnectionAbortedError


async def _answer_unavailable(app, scope, receive, send) -> None:
    """The service is restarting: it answers 503 and does nothing."""
    await send({"type": "http.response.start", "status": 503, "headers": [(b"content-length", b"2")]})
    await send({"type": "http.response.body", "body": b"{}"})


def _config(url: str, tmp_path: Path, **changes) -> WorkerConfig:
    return WorkerConfig(
        service=url, token=TOKEN, work_dir=tmp_path / "work", capabilities=CAPABILITIES,
        retry_first=0.01, retry_ceiling=0.05, wind_down_seconds=3, **changes,
    )


@contextlib.contextmanager
def _worker(url: str, tmp_path: Path, compute: media_worker_client.Compute, **changes):
    """A worker running in its own thread; leaving the block stops it."""
    worker = RemoteWorker(_config(url, tmp_path, **changes), compute)
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    try:
        yield worker
    finally:
        worker.stop()
        thread.join(15)
        assert not thread.is_alive(), "the worker did not stop"


def _until(condition: Callable[[], object], *, seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def _downloads(tmp_path: Path) -> list[Path]:
    return list((tmp_path / "work").glob("original-*"))


def _published(vault: Path, sidecar: Path) -> str:
    """Wait for a handed-in result, let the parent publish it, and return the sidecar."""
    assert _until(lambda: media_jobs.MediaJobStore(vault).has_parent_work()), "no result reached the ledger"
    _publish(vault)
    return sidecar.read_text(encoding="utf-8")


def _returned(vault: Path, name: str) -> dict:
    (job,) = [job for job in media_jobs.status(vault)["jobs"] if job["path"].endswith(name)]
    return job


# ---- the loop ----------------------------------------------------------------------------------------


def test_a_worker_leases_downloads_computes_and_hands_in_a_job_that_the_parent_publishes(
    vault, tmp_path, monkeypatch
) -> None:
    _credential(vault)
    _binary, sidecar = _audio(vault)
    seen: dict[str, object] = {}

    def compute(path: Path, job: Claimed, cancelled: threading.Event) -> dict[str, object]:
        seen.update(bytes=path.read_bytes(), mode=path.stat().st_mode & 0o777, media_type=job.media_type)
        return EXTRACTION

    with _service(vault, tmp_path, monkeypatch) as (url, _spy), _worker(url, tmp_path, compute):
        content = _published(vault, sidecar)
        assert _until(lambda: not _downloads(tmp_path)), "the original stayed on the worker"

    assert seen == {"bytes": AUDIO, "mode": 0o600, "media_type": "audio"}
    assert TRANSCRIPT in content and "[1:05] head aega" in content
    assert media_jobs.status(vault)["jobs"] == []


def test_an_interrupted_download_resumes_from_the_bytes_it_has_and_the_result_is_published(
    vault, tmp_path, monkeypatch
) -> None:
    _credential(vault)
    _binary, sidecar = _audio(vault)
    seen: dict[str, object] = {}

    def compute(path: Path, job: Claimed, cancelled: threading.Event) -> dict[str, object]:
        seen["bytes"] = path.read_bytes()
        return EXTRACTION

    fault = _on_first_call("/original", _cut_body_after(10))
    with _service(vault, tmp_path, monkeypatch, fault=fault) as (url, spy), _worker(url, tmp_path, compute):
        content = _published(vault, sidecar)

    assert [headers.get("range") for headers in spy.calls("/original")] == [None, "bytes=10-"]
    assert seen["bytes"] == AUDIO
    assert TRANSCRIPT in content


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda body: b"X" + body[1:], id="same size, other digest"),
        pytest.param(lambda body: body + b"EXTRA", id="larger than the claim says"),
    ],
)
def test_an_original_that_does_not_match_the_claim_goes_back_as_unsupported_input_and_nothing_is_computed(
    vault, tmp_path, monkeypatch, change
) -> None:
    _credential(vault)
    _audio(vault)
    computed: list[Path] = []

    def compute(path: Path, job: Claimed, cancelled: threading.Event) -> dict[str, object]:
        computed.append(path)
        return EXTRACTION

    fault = _on_first_call("/original", _alter_body(change))
    with _service(vault, tmp_path, monkeypatch, fault=fault) as (url, spy), _worker(url, tmp_path, compute):
        assert _until(lambda: spy.calls("/release")), "the lease was never handed back"
        assert _until(lambda: not _downloads(tmp_path))

    assert computed == []
    assert len(spy.calls("/release")) == 1
    assert "unsupported_input" in _returned(vault, "talk.mp3")["return_reason"]


def test_heartbeats_keep_a_lease_alive_while_compute_runs_past_its_lifetime(vault, tmp_path, monkeypatch) -> None:
    _credential(vault)
    _credential(vault, OTHER_TOKEN, name="worker-two")
    _binary, sidecar = _audio(vault)
    probes: list[int] = []
    computing, finished = threading.Event(), threading.Event()

    def compute(path: Path, job: Claimed, cancelled: threading.Event) -> dict[str, object]:
        computing.set()
        time.sleep(2.5)  # two and a half lifetimes of the lease
        finished.set()
        return EXTRACTION

    def competing_worker(url: str) -> None:
        # Every claim sweeps lapsed leases first, so a lease that nobody renewed goes to this worker.
        body = {"protocol": 1, "slots": 1, "capabilities": CAPABILITIES}
        while not finished.is_set():
            probes.append(httpx.post(f"{url}/media-worker/v1/claim", json=body, headers=_auth(OTHER_TOKEN)).status_code)
            time.sleep(0.2)

    with _service(vault, tmp_path, monkeypatch, lease_seconds=1.0, heartbeat_seconds=0.2) as (url, _spy):
        with _worker(url, tmp_path, compute):
            assert computing.wait(20)
            competing_worker(url)
            content = _published(vault, sidecar)

    assert len(probes) >= 5 and set(probes) == {204}  # no other worker was given the job
    assert TRANSCRIPT in content


def test_a_lease_lost_during_compute_posts_no_result_and_deletes_the_original(vault, tmp_path, monkeypatch) -> None:
    _credential(vault)
    _audio(vault)
    leased: list[Claimed] = []
    seen: dict[str, object] = {}

    def compute(path: Path, job: Claimed, cancelled: threading.Event) -> dict[str, object]:
        leased.append(job)
        seen["stopped"] = cancelled.wait(15)
        return EXTRACTION  # a result, to show that the worker discards it

    with _service(vault, tmp_path, monkeypatch, lease_seconds=2.0, heartbeat_seconds=0.1) as (url, spy):
        with _worker(url, tmp_path, compute):
            assert _until(lambda: leased)
            (job,) = leased
            took = httpx.post(  # the lease goes to the queue, as a sweep or an operator does it
                f"{url}/media-worker/v1/leases/{job.job_id}/release",
                json={"claim_revision": job.claim_revision, "reason": "shutdown"},
                headers=_auth(),
            )
            assert took.status_code == 200
            assert _until(lambda: "stopped" in seen), "compute never heard that the lease was lost"
            assert _until(lambda: not _downloads(tmp_path))
            time.sleep(0.3)  # long enough for a result post to arrive, if one was going to

    assert seen["stopped"] is True
    assert spy.calls("/result") == []
    assert not media_jobs.MediaJobStore(vault).has_parent_work()


def test_a_busy_device_hands_the_job_back_and_the_job_keeps_its_pin(vault, tmp_path, monkeypatch) -> None:
    _credential(vault)
    _audio(vault)
    attempts: list[int] = []

    def compute(path: Path, job: Claimed, cancelled: threading.Event) -> dict[str, object]:
        attempts.append(job.job_id)
        raise asr_runtime.ASRGpuBusy("another process holds the GPU")

    with _service(vault, tmp_path, monkeypatch) as (url, _spy), _worker(url, tmp_path, compute):
        assert _until(lambda: attempts)
        (job_id,) = attempts
        assert _until(lambda: media_jobs.MediaJobStore(vault).get_lease(job_id) is None)
        time.sleep(0.5)  # a handed-back job waits its retry time instead of looping at once

    row = _row(vault, job_id)
    assert attempts == [job_id]
    assert (row["state"], row["pinned"]) == ("pending", 1)
    assert "device_busy" in _returned(vault, "talk.mp3")["return_reason"]


def test_a_failed_compute_is_logged_and_handed_back_as_shutdown_and_the_slot_takes_the_next_job(
    vault, tmp_path, monkeypatch, caplog
) -> None:
    _credential(vault)
    _audio(vault, "first.mp3", b"first-recording")
    _binary, second = _audio(vault, "second.mp3", b"second-recording")
    calls: list[int] = []

    def compute(path: Path, job: Claimed, cancelled: threading.Event) -> dict[str, object]:
        calls.append(job.job_id)
        if len(calls) == 1:
            raise RuntimeError("the decoder gave up")
        return EXTRACTION

    caplog.set_level(logging.INFO, logger=media_worker_client.log.name)
    with _service(vault, tmp_path, monkeypatch) as (url, _spy), _worker(url, tmp_path, compute):
        content = _published(vault, second)

    assert TRANSCRIPT in content
    assert "shutdown" in _returned(vault, "first.mp3")["return_reason"]
    assert any(
        record.levelno == logging.ERROR and "compute failed" in record.getMessage() and record.exc_info
        for record in caplog.records
    )


def test_a_language_the_engine_cannot_do_fails_the_job_in_the_sidecar_instead_of_looping(
    vault, tmp_path, monkeypatch
) -> None:
    _credential(vault)
    _binary, sidecar = _audio(vault)
    leased: list[int] = []

    def compute(path: Path, job: Claimed, cancelled: threading.Event) -> dict[str, object]:
        leased.append(job.job_id)
        raise extract.UnsupportedLanguage("xx", ["en", "et"])

    with _service(vault, tmp_path, monkeypatch) as (url, _spy), _worker(url, tmp_path, compute):
        assert _until(lambda: media_jobs.MediaJobStore(vault).has_parent_work())
        _publish(vault)

    assert leased and _row(vault, leased[0])["state"] == "failed"
    assert "language 'xx' is not supported" in sidecar.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "lost", [pytest.param(_lose_the_answer, id="connection broke after the service did the work"),
             pytest.param(_answer_unavailable, id="service answered 503 and did nothing")]
)
def test_a_result_that_was_not_acknowledged_is_posted_again_and_published_once(
    vault, tmp_path, monkeypatch, lost
) -> None:
    _credential(vault)
    _binary, sidecar = _audio(vault)

    def compute(path: Path, job: Claimed, cancelled: threading.Event) -> dict[str, object]:
        return EXTRACTION

    fault = _on_first_call("/result", lost)
    with _service(vault, tmp_path, monkeypatch, fault=fault) as (url, spy), _worker(url, tmp_path, compute):
        content = _published(vault, sidecar)
        assert _until(lambda: len(spy.calls("/result")) == 2)
        time.sleep(0.3)  # nothing else follows an acknowledged result

    assert len(spy.calls("/result")) == 2
    assert content.count("tere maailm") == 1
    assert spy.calls("/release") == []
    assert not media_jobs.MediaJobStore(vault).has_parent_work()  # published once: the queue of results is empty
    assert media_jobs.status(vault)["jobs"] == []


def test_with_two_slots_two_jobs_run_at_once_and_the_third_waits(vault, tmp_path, monkeypatch) -> None:
    _credential(vault, max_leases=8)  # the worker's two slots, not the credential, limit the leases
    sidecars = [_audio(vault, f"{name}.mp3", name.encode())[1] for name in ("first", "second", "third")]
    started: list[int] = []
    release = threading.Event()

    def compute(path: Path, job: Claimed, cancelled: threading.Event) -> dict[str, object]:
        started.append(job.job_id)
        release.wait(20)
        return EXTRACTION

    with _service(vault, tmp_path, monkeypatch) as (url, _spy), _worker(url, tmp_path, compute, slots=2):
        assert _until(lambda: len(started) == 2)
        time.sleep(0.7)  # time enough for a third claim, if the worker made one
        assert len(started) == 2
        assert len(media_jobs.status(vault)["remote_leases"]) == 2
        release.set()
        assert _until(lambda: len(started) == 3)
        assert _until(lambda: not media_jobs.status(vault)["remote_leases"])
        _publish(vault)

    assert all(TRANSCRIPT in sidecar.read_text(encoding="utf-8") for sidecar in sidecars)


# ---- stopping and refusal ----------------------------------------------------------------------------


def test_sigterm_releases_every_held_lease_with_shutdown_and_deletes_the_originals(
    vault, tmp_path, monkeypatch
) -> None:
    _credential(vault, max_leases=8)
    _audio(vault, "first.mp3", b"first-recording")
    _audio(vault, "second.mp3", b"second-recording")
    held: list[Path] = []
    leased: list[int] = []
    workers: list[RemoteWorker] = []
    stopped, gave_up = threading.Event(), threading.Event()
    real = media_worker_client.RemoteWorker

    def keep(*args, **kwargs) -> RemoteWorker:
        workers.append(real(*args, **kwargs))
        return workers[0]

    def compute(path: Path, job: Claimed, cancelled: threading.Event) -> dict[str, object]:
        held.append(path)
        leased.append(job.job_id)
        cancelled.wait(30)
        return EXTRACTION

    def terminate() -> None:
        if _until(lambda: len(held) == 2):
            os.kill(os.getpid(), signal.SIGTERM)
        if not stopped.wait(15):  # a worker that ignores SIGTERM must fail this test, not hang it
            gave_up.set()
            workers[0].stop()

    monkeypatch.setattr(media_worker_client, "RemoteWorker", keep)
    with _service(vault, tmp_path, monkeypatch) as (url, _spy):
        threading.Thread(target=terminate, daemon=True).start()
        code = media_worker_client.run_worker(_config(url, tmp_path, slots=2), compute)
        stopped.set()

    assert not gave_up.is_set() and code == 0
    assert all(not path.exists() for path in held) and _downloads(tmp_path) == []
    store = media_jobs.MediaJobStore(vault)
    for name in ("first.mp3", "second.mp3"):
        job = _returned(vault, name)
        assert job["id"] in leased and job["state"] == "pending" and "shutdown" in job["return_reason"]
        assert store.get_lease(job["id"]) is None


@pytest.mark.parametrize(
    ("token", "protocol", "message"),
    [
        pytest.param("mw1_a-token-the-service-never-issued", 1, "(401)", id="credential refused"),
        pytest.param(TOKEN, 2, "(426)", id="protocol not spoken"),
    ],
)
def test_the_command_exits_non_zero_with_a_clear_message_when_the_service_refuses_the_worker(
    vault, tmp_path, monkeypatch, capsys, token, protocol, message
) -> None:
    _credential(vault)
    credential_file = tmp_path / "worker-credential"
    credential_file.write_text(token + "\n", encoding="utf-8")
    monkeypatch.setattr(media_worker_client, "PROTOCOL_VERSION", protocol)
    monkeypatch.setattr(media_worker_client, "detect_capabilities", lambda *, diarize: CAPABILITIES)

    with _service(vault, tmp_path, monkeypatch) as (url, _spy):
        code = cli.main(
            ["media-worker", "run", "--service", url, "--credential-file", str(credential_file),
             "--work-dir", str(tmp_path / "work")]
        )

    assert code == 1
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("content", [None, "  \n"], ids=["file missing", "file empty"])
def test_the_command_without_a_readable_credential_exits_with_a_message_and_contacts_nobody(
    tmp_path, capsys, content
) -> None:
    credential_file = tmp_path / "worker-credential"
    if content is not None:
        credential_file.write_text(content, encoding="utf-8")

    code = cli.main(["media-worker", "run", "--service", "http://127.0.0.1:9", "--credential-file", str(credential_file)])

    assert code == 2
    assert str(credential_file) in capsys.readouterr().err


def test_the_command_refuses_a_service_address_without_a_scheme_instead_of_retrying_it_forever(
    tmp_path, capsys
) -> None:
    credential_file = tmp_path / "worker-credential"
    credential_file.write_text(TOKEN, encoding="utf-8")

    code = cli.main(["media-worker", "run", "--service", "memory.example", "--credential-file", str(credential_file)])

    assert code == 2
    assert "http://" in capsys.readouterr().err


def test_a_worker_configuration_never_shows_its_credential() -> None:
    config = WorkerConfig(
        service="https://memory.example", token=TOKEN, work_dir=Path("/work"), capabilities=CAPABILITIES
    )

    assert TOKEN not in repr(config)


# ---- what a failed compute means ---------------------------------------------------------------------


def _stub_av(monkeypatch: pytest.MonkeyPatch) -> type[Exception]:
    """PyAV is optional in the test environment; a stub names the base class of its decode errors."""
    error = types.ModuleType("av.error")
    error.FFmpegError = type("FFmpegError", (Exception,), {})
    package = types.ModuleType("av")
    package.error = error
    monkeypatch.setitem(sys.modules, "av", package)
    monkeypatch.setitem(sys.modules, "av.error", error)
    return error.FFmpegError


@pytest.mark.parametrize(
    ("raised", "reason"),
    [
        pytest.param(asr_runtime.ASRGpuBusy("held"), "device_busy", id="gpu held by another process"),
        pytest.param(asr_runtime.ASRRuntimeRefusal("no gpu"), "device_busy", id="gpu withheld by the containment check"),
        pytest.param(RuntimeError("the engine crashed"), "shutdown", id="any other failure"),
    ],
)
def test_a_failed_compute_is_handed_back_for_the_reason_that_names_its_cause(monkeypatch, raised, reason) -> None:
    refusal = isinstance(raised, asr_runtime.ASRRuntimeRefusal)
    monkeypatch.setattr(cuda_containment, "preflight_failure", lambda env=None: "driver missing" if refusal else None)

    assert media_worker_client.release_reason(raised) == reason


def test_a_runtime_refusal_with_a_healthy_gpu_is_not_a_busy_device(monkeypatch) -> None:
    monkeypatch.setattr(cuda_containment, "preflight_failure", lambda env=None: None)

    assert media_worker_client.release_reason(asr_runtime.ASRRuntimeRefusal("the model is unsafe")) == "shutdown"


def test_an_input_that_the_decoder_rejects_is_handed_back_as_unsupported_input(monkeypatch) -> None:
    decode_error = _stub_av(monkeypatch)

    assert media_worker_client.release_reason(decode_error("invalid data")) == "unsupported_input"


# ---- what this machine declares ----------------------------------------------------------------------


class _Backend:
    name, device = "onnx-asr", "cpu"


def _machine(monkeypatch: pytest.MonkeyPatch, *, sidecar_ready: bool) -> None:
    monkeypatch.setattr(media_engines, "stage_available", lambda kind: True)
    monkeypatch.setattr(extract, "get_transcriber", lambda: _Backend())
    monkeypatch.setattr(
        extract, "diarization_prerequisites", lambda: extract.DiarizationPrerequisites("/venv", sidecar_ready)
    )


def test_a_worker_declares_the_kinds_the_engine_and_the_device_this_machine_runs(monkeypatch) -> None:
    _machine(monkeypatch, sidecar_ready=False)

    declared = media_worker_client.detect_capabilities(diarize=False)

    assert declared == {
        "media_types": ["audio", "video"], "engines": ["speech"], "device": "cpu",
        "asr_backend": "onnx-asr", "diarize": False,
    }


@pytest.mark.parametrize(
    ("flag", "sidecar_ready", "declared"),
    [(False, True, False), (True, False, False), (True, True, True)],
    ids=["ready but not asked", "asked but not ready", "asked and ready"],
)
def test_a_worker_declares_diarization_only_when_asked_and_ready(monkeypatch, flag, sidecar_ready, declared) -> None:
    _machine(monkeypatch, sidecar_ready=sidecar_ready)

    assert media_worker_client.detect_capabilities(diarize=flag)["diarize"] is declared


def test_a_machine_with_no_speech_engine_cannot_run_the_worker(monkeypatch) -> None:
    monkeypatch.setattr(media_engines, "stage_available", lambda kind: False)

    with pytest.raises(media_worker_client.CannotRun):
        media_worker_client.detect_capabilities(diarize=False)


def test_the_worker_relaunches_itself_once_under_the_cuda_libraries(monkeypatch) -> None:
    launched: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.delenv(asr_runtime.CUDA_RUNTIME_READY_ENV)
    monkeypatch.setattr(asr_runtime, "cuda_runtime_child_env", lambda parent: {**parent, "LD_LIBRARY_PATH": "/wheels/cublas"})
    monkeypatch.setattr(cuda_containment, "check_gpu", lambda env, *, driver_present: cuda_containment.Preflight(True))
    monkeypatch.setattr(os, "execve", lambda path, argv, env: launched.append((argv, env)))

    asr_runtime.relaunch_with_cuda_runtime(["media-worker", "run", "--service", "https://memory.example"])
    (argv, env) = launched[0]
    monkeypatch.setenv(asr_runtime.CUDA_RUNTIME_READY_ENV, env[asr_runtime.CUDA_RUNTIME_READY_ENV])  # the new process
    asr_runtime.relaunch_with_cuda_runtime(["media-worker", "run", "--service", "https://memory.example"])

    assert argv[1:] == ["-m", "exomem", "media-worker", "run", "--service", "https://memory.example"]
    assert env["LD_LIBRARY_PATH"] == "/wheels/cublas"
    assert len(launched) == 1  # the relaunched process does not relaunch again


# ---- the compute -------------------------------------------------------------------------------------


class _Segment:
    def __init__(self, start: float, end: float, text: str) -> None:
        self.start, self.end, self.text = start, end, text


class _Transcriber:
    """Records what the job's settings did to the engine call."""

    def __init__(self) -> None:
        self.segments, self.calls = [_Segment(1.0, 3.0, "tere maailm")], []

    def transcribe(self, path, *, language, multilingual, report, policy):
        self.calls.append({"language": language, "multilingual": multilingual, "policy": policy})
        report.language, report.duration_seconds, report.policy = "et", 61.5, "fw-policy-2"
        return self.segments, "faster-whisper:test"


@pytest.mark.parametrize(
    ("job_language", "forced", "source"),
    [("et", "et", "requested"), (None, None, "detected")],
    ids=["the job names a language", "the job names none: the engine detects"],
)
def test_the_compute_uses_the_language_and_the_policy_of_the_job_and_not_the_local_configuration(
    tmp_path, monkeypatch, job_language, forced, source
) -> None:
    engine = _Transcriber()
    monkeypatch.setattr(extract, "get_transcriber", lambda: engine)
    monkeypatch.setenv(extract.ASR_LANGUAGE_ENV, "fi")  # this machine's own setting
    policy = {"revision": "fw-policy-2", "options": {"beam_size": 3}}
    job = Claimed(
        job_id=7, claim_revision=1, heartbeat_seconds=1.0, media_type="audio", language=job_language,
        language_mode="single", asr_policy=policy, size=0, sha256="",
    )

    payload = media_worker_client.speech_compute(diarize=False)(tmp_path / "talk.mp3", job, threading.Event())

    assert engine.calls == [{"language": forced, "multilingual": False, "policy": policy}]
    assert payload["engine"] == "faster-whisper:test"
    assert payload["segments"] == [{"start": 1.0, "end": 3.0, "text": "tere maailm"}]
    assert payload["asr"]["language_source"] == source and payload["asr"]["language_requested"] == forced
    assert "diarization" not in payload


def test_the_compute_adds_the_turns_and_one_voiceprint_per_speaker_when_asked(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(extract, "get_transcriber", lambda: _Transcriber())
    turns = [(0.0, 10.0, "c0"), (60.0, 70.0, "c1")]
    monkeypatch.setattr(extract, "_run_diarization", lambda path: turns)
    monkeypatch.setattr(extract, "_embed_clusters", lambda path, found: {"c0": _voiceprint(0), "c1": _voiceprint(1)})

    payload = extract.worker_speech_payload(tmp_path / "talk.mp3", "audio", language=None, multilingual=False, diarize=True)

    assert payload["diarization"]["turns"] == [
        {"start": 0.0, "end": 10.0, "cluster": "c0"}, {"start": 60.0, "end": 70.0, "cluster": "c1"}
    ]
    assert set(payload["diarization"]["clusters"]) == {"c0", "c1"}


def test_a_voiceprint_that_cannot_be_computed_leaves_the_transcript_without_speakers(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(extract, "get_transcriber", lambda: _Transcriber())
    monkeypatch.setattr(extract, "_run_diarization", lambda path: [(0.0, 10.0, "c0")])
    monkeypatch.setattr(extract, "_embed_clusters", lambda path, found: None)

    payload = extract.worker_speech_payload(tmp_path / "talk.mp3", "audio", language=None, multilingual=False, diarize=True)

    assert payload["segments"] and "diarization" not in payload


def test_a_compute_that_the_lease_cancels_stops_between_segments(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(extract, "get_transcriber", lambda: _Transcriber())

    with pytest.raises(extract.ExtractionCancelled):
        extract.worker_speech_payload(
            tmp_path / "talk.mp3", "audio", language=None, multilingual=False, cancelled=lambda: True
        )
