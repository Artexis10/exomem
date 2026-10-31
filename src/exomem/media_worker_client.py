"""The remote media worker (D2, D8, D8a): it leases speech jobs from a service and computes them on this machine.

The worker holds no vault. It claims a job with its credential, downloads the original into a private
directory, checks the size and SHA-256 that the claim names, and computes the ASR segments (with diarization,
also the turns and one voiceprint per cluster). It hands in that structure only: the service renders the text
and matches the voiceprints against its own voice profiles. The worker deletes the original when the lease
ends, whatever the outcome, and keeps no cache of originals.

The wire constants live here, beside the caller, and the service (`server_media_worker`) imports them.
This module imports no engine at load time; the engines load when a job needs them.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

#: The one protocol version this worker speaks.
PROTOCOL_VERSION = 1
BASE_PATH = "/media-worker/v1"
# The reasons that a worker gives when it hands a lease back, as the protocol fixes them.
DEVICE_BUSY = "device_busy"
INSUFFICIENT_SPACE = "insufficient_space"
SHUTDOWN = "shutdown"
UNSUPPORTED_INPUT = "unsupported_input"
LEASE_LOST_CODE = "MEDIA_LEASE_LOST"

# Transport bounds, not policy. A claim waits on the service for about 25 s, so a read must wait longer.
_CONNECT_SECONDS = 10.0
_READ_SECONDS = 60.0
#: The 4xx statuses of a service that is busy or restarting, which a worker retries like a 5xx.
_BUSY = frozenset({408, 425, 429})


class FatalServiceResponse(Exception):
    """The service refuses this worker in a way that no retry changes; the worker stops."""


class _Transient(Exception):
    """The service is busy or restarting: the request is worth repeating."""


class CannotRun(Exception):
    """This machine cannot run the worker; the message says why."""


class _HandBack(Exception):
    """The job cannot be computed here: give the lease back for `reason`."""

    def __init__(self, reason: str, why: str) -> None:
        super().__init__(why)
        self.reason = reason


class _LeaseOver(Exception):
    """The lease ended while this thread worked on it, so nothing more is sent for it."""


@dataclass(frozen=True)
class Claimed:
    """A job that the service leased to this worker, and what the vault decided about its computation."""

    job_id: int
    claim_revision: int
    heartbeat_seconds: float
    media_type: str
    language: str | None
    language_mode: str | None
    asr_policy: dict
    size: int
    sha256: str

    @classmethod
    def from_response(cls, body: dict) -> Claimed:
        lease, job = body["lease"], body["job"]
        return cls(
            job_id=int(lease["job_id"]),
            claim_revision=int(lease["claim_revision"]),
            heartbeat_seconds=float(lease["heartbeat_seconds"]),
            media_type=str(job["media_type"]),
            language=job["language"],
            language_mode=job["language_mode"],
            asr_policy=dict(job["asr_policy"]),
            size=int(job["original"]["size"]),
            sha256=str(job["original"]["sha256"]),
        )


#: What a job computes: the original's path, the job and an event that is set when the lease is over.
#: It returns the extraction payload, and may stop early by raising when the event is set.
Compute = Callable[[Path, Claimed, threading.Event], dict[str, object]]


@dataclass(frozen=True)
class WorkerConfig:
    service: str
    token: str = field(repr=False)  # a credential never reaches a log line, even through a repr
    work_dir: Path
    capabilities: dict[str, object]
    slots: int = 1
    #: The pause after a failed request: doubles up to the ceiling.
    retry_first: float = 1.0
    retry_ceiling: float = 60.0
    #: The tries of a download, a result post and a release before the worker gives up on one.
    attempts: int = 5
    #: How long a stopping worker waits for its slots before it exits.
    wind_down_seconds: float = 10.0


class _Held:
    """One lease that this worker holds, from the claim until the original is gone."""

    def __init__(self, job: Claimed, path: Path) -> None:
        self.job = job
        self.path = path
        #: Set when the lease is lost or the worker stops; a compute that can stop early watches it.
        self.cancelled = threading.Event()
        #: Set when the work on the lease is over, which ends its heartbeat.
        self.done = threading.Event()
        self._lock = threading.Lock()
        self._ended = False

    def claim_end(self) -> bool:
        """Whether this caller sends the one final message of the lease: a result or a release."""
        with self._lock:
            ended, self._ended = self._ended, True
            return not ended

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)


class RemoteWorker:
    """One worker process: a loop for each slot, each holding one lease at a time."""

    def __init__(self, config: WorkerConfig, compute: Compute) -> None:
        self._config = config
        self._compute = compute
        self._stop = threading.Event()
        self._refusal: str | None = None
        self._lock = threading.Lock()
        self._held: dict[int, _Held] = {}
        self._http = httpx.Client(
            base_url=config.service.rstrip("/") + BASE_PATH + "/",
            headers={"authorization": f"Bearer {config.token}"},
            timeout=httpx.Timeout(_READ_SECONDS, connect=_CONNECT_SECONDS),
        )

    def stop(self) -> None:
        """Ask the worker to release its leases and exit. Safe in a signal handler."""
        self._stop.set()

    def run(self) -> int:
        """Work until `stop` or a fatal refusal; the exit code is 0 after a stop and 1 after a refusal."""
        self._config.work_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        for stale in self._config.work_dir.glob("original-*"):
            stale.unlink(missing_ok=True)  # a crashed run's partial original: its lease lapsed long ago
        loops = [
            threading.Thread(target=self._loop, args=(slot,), name=f"media-worker-slot-{slot}", daemon=True)
            for slot in range(self._config.slots)
        ]
        for loop in loops:
            loop.start()
        self._stop.wait()
        self._wind_down(loops)
        if not any(loop.is_alive() for loop in loops):  # a claim still in flight ends with the process
            self._http.close()
        if self._refusal is not None:
            print(f"exomem media-worker: {self._refusal}", file=sys.stderr)
            return 1
        return 0

    # ---- the loop of one slot ---------------------------------------------------------------------

    def _loop(self, slot: int) -> None:
        pause = self._config.retry_first
        while not self._stop.is_set():
            try:
                claimed = self._claim()
            except FatalServiceResponse as refusal:
                self._refuse(str(refusal))
                return
            except (httpx.TransportError, _Transient) as error:
                log.warning("slot %d: claim failed (%s); retrying in %g s", slot, _describe(error), pause)
                self._stop.wait(pause)
                pause = min(pause * 2, self._config.retry_ceiling)
                continue
            pause = self._config.retry_first
            if claimed is None:
                continue
            try:
                self._work(claimed)
            except Exception:  # noqa: BLE001 - a bug in one job must not end the slot; _work has released the lease
                log.exception("slot %d: job %d failed unexpectedly", slot, claimed.job_id)

    def _claim(self) -> Claimed | None:
        response = self._http.post(
            "claim",
            json={
                "protocol": PROTOCOL_VERSION,
                "slots": self._config.slots,
                "capabilities": self._config.capabilities,
            },
        )
        self._check(response)
        if _transient(response):
            raise _Transient(f"the service answered {response.status_code}")
        if response.status_code == 204:
            return None
        if response.status_code == 200:
            try:
                return Claimed.from_response(response.json())
            except (KeyError, TypeError, ValueError) as error:
                raise _Transient(f"the claim answer is malformed ({type(error).__name__})") from error
        raise _unexpected(response)

    def _work(self, claimed: Claimed) -> None:
        if self._stop.is_set():  # granted as the worker stopped: hand it straight back
            self._release(claimed, SHUTDOWN)
            return
        held = _Held(claimed, self._config.work_dir / f"original-{claimed.job_id}-{claimed.claim_revision}")
        with self._lock:
            self._held[claimed.job_id] = held
        threading.Thread(target=self._beat, args=(held,), name=f"media-worker-beat-{claimed.job_id}", daemon=True).start()
        started = time.monotonic()
        try:
            log.info("job %d: leased (%s, %d bytes)", claimed.job_id, claimed.media_type, claimed.size)
            self._fetch(held)
            payload = self._compute(held.path, claimed, held.cancelled)
            if held.cancelled.is_set():
                raise _LeaseOver
            log.info("job %d: computed in %.1f s", claimed.job_id, time.monotonic() - started)
            self._hand_in(held, payload)
        except _LeaseOver:
            log.info("job %d: the lease is over; nothing is handed in", claimed.job_id)
        except _HandBack as handback:
            self._give_back(held, handback.reason, str(handback))
        except FatalServiceResponse as refusal:
            self._refuse(str(refusal))
        except Exception as error:  # noqa: BLE001 - classified below; the lease is released, never dropped
            if held.cancelled.is_set():
                log.info("job %d: stopped with the lease (%s)", claimed.job_id, type(error).__name__)
            elif (failure := _job_failure(error)) is not None:
                self._hand_in_failure(held, failure)
            else:
                reason = release_reason(error)
                log.error("job %d: compute failed (%s)", claimed.job_id, type(error).__name__, exc_info=True)
                self._give_back(held, reason, type(error).__name__)
        finally:
            held.done.set()
            held.delete()
            with self._lock:
                self._held.pop(claimed.job_id, None)

    # ---- heartbeat ---------------------------------------------------------------------------------

    def _beat(self, held: _Held) -> None:
        job = held.job
        while not held.done.wait(job.heartbeat_seconds):
            try:
                response = self._http.post(
                    f"leases/{job.job_id}/heartbeat", json={"claim_revision": job.claim_revision}
                )
                self._check(response)
            except FatalServiceResponse as refusal:
                self._refuse(str(refusal))
                return
            except (httpx.TransportError, _Transient) as error:
                log.warning("job %d: heartbeat failed (%s)", job.job_id, _describe(error))
                continue
            if response.status_code == 409 and _code(response) == LEASE_LOST_CODE:
                log.warning("job %d: the service says the lease is lost; stopping the work", job.job_id)
                held.cancelled.set()
                return
            if response.status_code != 200:
                log.warning("job %d: heartbeat answered %d", job.job_id, response.status_code)

    # ---- download and verification -----------------------------------------------------------------

    def _fetch(self, held: _Held) -> None:
        """Download the original with `Range` resume, then verify its size and digest against the claim.

        A download that ends larger than the claim says, or with another digest, is not the original the vault
        leased: the job goes back as an input this worker cannot use.
        """
        job = held.job
        failures, pause = 0, self._config.retry_first
        while True:
            if held.cancelled.is_set():
                raise _LeaseOver
            have = held.path.stat().st_size if held.path.exists() else 0
            if have > job.size:
                raise _HandBack(UNSUPPORTED_INPUT, "the downloaded original is larger than the claim says")
            if have == job.size:
                break
            try:
                progressed = self._download_from(held, have)
            except (httpx.TransportError, _Transient) as error:
                log.warning("job %d: the download broke at %d of %d bytes (%s)", job.job_id, have, job.size, _describe(error))
                progressed = False
            if progressed:
                continue
            failures += 1
            if failures >= self._config.attempts:
                raise _HandBack(SHUTDOWN, "the download kept failing")
            time.sleep(pause)
            pause = min(pause * 2, self._config.retry_ceiling)
        if not _matches(held.path, job.sha256):
            raise _HandBack(UNSUPPORTED_INPUT, "the downloaded original does not match the digest of the claim")
        log.info("job %d: original downloaded and verified", job.job_id)

    def _download_from(self, held: _Held, have: int) -> bool:
        """Append the original from byte `have`; whether any new byte arrived. A broken stream raises."""
        job = held.job
        # The bytes as stored: an encoding would change them, and a `Range` counts stored bytes.
        headers = {"accept-encoding": "identity", **({"range": f"bytes={have}-"} if have else {})}
        url = f"leases/{job.job_id}/original"
        with self._http.stream("GET", url, params={"claim_revision": job.claim_revision}, headers=headers) as response:
            self._check(response)
            status = response.status_code
            if status == 409:
                response.read()
                if _code(response) == LEASE_LOST_CODE:
                    held.cancelled.set()
                    raise _LeaseOver
                raise _HandBack(SHUTDOWN, "the service cannot serve the original as it was leased")
            if status == 404:
                raise _HandBack(SHUTDOWN, "the service no longer offers the original")
            if status == 416:
                held.delete()  # the partial file does not fit the original: start again
                return False
            if _transient(response):
                raise _Transient(f"the original answered {status}")
            if status not in (200, 206):
                raise _unexpected(response)
            resume = status == 206
            if resume and not response.headers.get("content-range", "").startswith(f"bytes {have}-"):
                held.delete()
                return False
            flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if resume else os.O_TRUNC)
            received = 0
            with os.fdopen(os.open(held.path, flags, 0o600), "ab" if resume else "wb") as target:
                # `iter_raw`, not `iter_bytes(size)`: the latter holds back a partial block when the stream breaks.
                for block in response.iter_raw():
                    if held.cancelled.is_set():
                        raise _LeaseOver
                    target.write(block)
                    received += len(block)
            return received > 0

    # ---- result, failure and release ---------------------------------------------------------------

    def _hand_in(self, held: _Held, payload: dict[str, object]) -> None:
        if not held.claim_end():
            raise _LeaseOver  # the worker is stopping and has released the lease
        self._post_result(held, {"kind": "extraction", "payload": payload})

    def _hand_in_failure(self, held: _Held, error: str) -> None:
        log.error("job %d: the job fails for good (%s)", held.job.job_id, error)
        if held.claim_end():
            self._post_result(held, {"kind": "failure", "payload": {"error": error, "retryable": False}})

    def _post_result(self, held: _Held, body: dict[str, object]) -> None:
        """Post the result, retrying a lost connection or a 5xx. A repeated post is safe: the ledger knows the first."""
        job, pause = held.job, self._config.retry_first
        url, body = f"leases/{job.job_id}/result", {"claim_revision": job.claim_revision, **body}
        for attempt in range(1, self._config.attempts + 1):
            try:
                response = self._http.post(url, json=body)
                self._check(response)
                status = response.status_code
                if status == 200:
                    log.info("job %d: result handed in", job.job_id)
                    return
                if status == 409:
                    log.warning("job %d: result given up (%s)", job.job_id, _code(response))
                    return
                if status == 400:
                    log.error("job %d: the service refused the result: %s", job.job_id, _reason(response))
                    self._release(job, SHUTDOWN)
                    return
                if not _transient(response):
                    raise _unexpected(response)
                error = f"answered {status}"
            except httpx.TransportError as transport:
                error = _describe(transport)
            log.warning("job %d: result post %d of %d failed (%s)", job.job_id, attempt, self._config.attempts, error)
            if attempt < self._config.attempts:
                time.sleep(pause)
                pause = min(pause * 2, self._config.retry_ceiling)
        log.error("job %d: result not handed in; the lease lapses and the job returns to the queue", job.job_id)

    def _give_back(self, held: _Held, reason: str, why: str) -> None:
        log.warning("job %d: handed back (%s): %s", held.job.job_id, reason, why)
        if held.claim_end():
            self._release(held.job, reason)

    def _release(self, job: Claimed, reason: str) -> None:
        pause = self._config.retry_first
        for attempt in range(1, self._config.attempts + 1):
            try:
                response = self._http.post(
                    f"leases/{job.job_id}/release", json={"claim_revision": job.claim_revision, "reason": reason}
                )
                self._check(response)
            except FatalServiceResponse:
                return  # the credential is refused; the lease lapses on its own
            except httpx.TransportError as error:
                log.warning("job %d: release failed (%s)", job.job_id, _describe(error))
            else:
                if not _transient(response):
                    if response.status_code != 200:
                        log.warning("job %d: release answered %d (%s)", job.job_id, response.status_code, _code(response))
                    return
                log.warning("job %d: release answered %d", job.job_id, response.status_code)
            if attempt < self._config.attempts:
                time.sleep(pause)
                pause = min(pause * 2, self._config.retry_ceiling)

    # ---- stopping ----------------------------------------------------------------------------------

    def _check(self, response: httpx.Response) -> None:
        if response.status_code == 401:
            raise FatalServiceResponse(
                "the service refused the worker credential (401); check the file, or issue a new credential"
            )
        if response.status_code == 426:
            raise FatalServiceResponse(
                f"the service does not speak media worker protocol {PROTOCOL_VERSION} (426); "
                "update this worker or the service"
            )

    def _refuse(self, message: str) -> None:
        with self._lock:
            self._refusal = self._refusal or message
        self._stop.set()

    def _wind_down(self, loops: list[threading.Thread]) -> None:
        """Release every held lease, delete the originals and give the slots a short time to finish."""
        with self._lock:
            leases = list(self._held.values())
        for held in leases:
            held.cancelled.set()
            held.done.set()
            if held.claim_end():
                log.info("job %d: releasing the lease (shutdown)", held.job.job_id)
                self._release(held.job, SHUTDOWN)
            held.delete()
        deadline = time.monotonic() + self._config.wind_down_seconds
        for loop in loops:
            loop.join(max(0.0, deadline - time.monotonic()))
        for stale in self._config.work_dir.glob("original-*"):
            stale.unlink(missing_ok=True)


def _transient(response: httpx.Response) -> bool:
    return response.status_code >= 500 or response.status_code in _BUSY


def _unexpected(response: httpx.Response) -> FatalServiceResponse:
    if not response.is_closed:
        response.read()
    return FatalServiceResponse(
        f"the service answered {response.status_code} ({_code(response)}): {_reason(response)}"
    )


def _json(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _code(response: httpx.Response) -> str:
    return str(_json(response).get("code", ""))


def _reason(response: httpx.Response) -> str:
    return str(_json(response).get("reason", ""))[:200]


def _describe(error: BaseException) -> str:
    """An error as a log line carries it: the class and no message, since a message can hold a URL."""
    return type(error).__name__


def _matches(path: Path, sha256: str) -> bool:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == sha256


def release_reason(error: BaseException) -> str:
    """Why a failed compute hands its lease back: a busy device, an input that cannot be decoded, or any other failure."""
    from . import asr_runtime, cuda_containment

    if isinstance(error, asr_runtime.ASRGpuBusy):
        return DEVICE_BUSY
    # A GPU that the containment preflight withheld is unavailable to this worker, as a busy one is.
    if isinstance(error, asr_runtime.ASRRuntimeRefusal) and cuda_containment.preflight_failure():
        return DEVICE_BUSY
    try:
        from av.error import FFmpegError  # PyAV's base class of every decode failure
    except ImportError:
        return SHUTDOWN
    return UNSUPPORTED_INPUT if isinstance(error, FFmpegError) else SHUTDOWN


def _job_failure(error: BaseException) -> str | None:
    """The text of a failure that the job's own settings cause, which no other worker or later try changes, or None."""
    from . import extract

    if isinstance(error, extract.UnsupportedLanguage | extract.MultilingualUnsupported):
        return str(error)
    return None


# ---- the command --------------------------------------------------------------------------------------


def detect_capabilities(*, diarize: bool) -> dict[str, object]:
    """What this machine can run, as a claim declares it. Raises `CannotRun` when it can run no speech at all."""
    from . import asr_runtime, extract, media_engines

    kinds = sorted(kind for kind in media_engines.SPEECH_KINDS if media_engines.stage_available(kind))
    if not kinds:
        raise CannotRun(f"this machine cannot run the speech engine ({media_engines.status()[media_engines.SPEECH]})")
    backend = extract.get_transcriber()
    device = backend.device
    if device is None:  # faster-whisper decides between CUDA and CPU by the same rule as the service
        try:
            device = asr_runtime.select_asr_runtime().device
        except asr_runtime.ASRGpuBusy:
            device = "cuda"  # a healthy device that another process holds right now
        except asr_runtime.ASRRuntimeRefusal:
            device = "unavailable"
    prerequisites = extract.diarization_prerequisites()
    if diarize and not prerequisites.ready:
        log.warning(
            "diarization is off: sidecar_venv=%s hf_token=%s; transcripts go in without speakers",
            prerequisites.sidecar_venv, prerequisites.hf_token,
        )
    return {
        "media_types": kinds,
        "engines": [media_engines.SPEECH],
        "device": device,
        "asr_backend": backend.name,
        "diarize": diarize and prerequisites.ready,
    }


def speech_compute(*, diarize: bool) -> Compute:
    """The compute of a speech job on this machine's engines."""

    def compute(path: Path, job: Claimed, cancelled: threading.Event) -> dict[str, object]:
        from . import extract
        from .asr_facts import LANGUAGE_MODE_MULTILINGUAL

        return extract.worker_speech_payload(
            path,
            job.media_type,
            language=job.language,
            multilingual=job.language_mode == LANGUAGE_MODE_MULTILINGUAL,
            policy=job.asr_policy,
            diarize=diarize,
            cancelled=cancelled.is_set,
        )

    return compute


def run_worker(config: WorkerConfig, compute: Compute) -> int:
    """Run a worker until SIGTERM, SIGINT or a refusal, and return the exit code."""
    worker = RemoteWorker(config, compute)
    previous: dict[int, object] = {}
    if threading.current_thread() is threading.main_thread():
        for number in (signal.SIGTERM, signal.SIGINT):
            previous[number] = signal.signal(number, lambda _number, _frame: worker.stop())
    try:
        return worker.run()
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def run_command(
    *, service: str, credential_file: str, slots: int, work_dir: str | None, diarize: bool, arguments: list[str]
) -> int:
    """`exomem media-worker run`: the arguments, once more, are what a relaunch under the CUDA libraries repeats."""
    from . import asr_runtime

    # The two schemes of an HTTP client (a closed protocol set); any other address would only fail and retry forever.
    if httpx.URL(service).scheme not in ("http", "https"):
        print(f"exomem media-worker: --service must start with http:// or https://, got {service}", file=sys.stderr)
        return 2
    try:
        token = Path(credential_file).expanduser().read_text(encoding="utf-8").strip()
    except OSError as error:
        print(f"exomem media-worker: cannot read {credential_file}: {error.strerror or type(error).__name__}", file=sys.stderr)
        return 2
    if not token:
        print(f"exomem media-worker: {credential_file} holds no credential", file=sys.stderr)
        return 2
    asr_runtime.relaunch_with_cuda_runtime(["media-worker", "run", *arguments])
    try:
        capabilities = detect_capabilities(diarize=diarize)
    except CannotRun as error:
        print(f"exomem media-worker: {error}", file=sys.stderr)
        return 2
    own_dir = work_dir is None
    directory = Path(tempfile.mkdtemp(prefix="exomem-media-worker-")) if own_dir else Path(work_dir).expanduser()
    log.info("media worker: %d slot(s), %s", slots, {key: value for key, value in capabilities.items() if key != "engines"})
    try:
        return run_worker(
            WorkerConfig(
                service=service, token=token, work_dir=directory, capabilities=capabilities, slots=slots
            ),
            speech_compute(diarize=bool(capabilities["diarize"])),
        )
    finally:
        if own_dir:
            shutil.rmtree(directory, ignore_errors=True)
