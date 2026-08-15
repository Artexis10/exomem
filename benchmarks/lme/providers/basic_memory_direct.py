"""Controlled-direct row for Basic Memory, served by the pinned §4.4 sidecar.

The competitor's provider class runs under the competitor's own uv environment
(design decision 1), which is what makes this row out of process.  Nothing of
Basic Memory is imported here: this module owns the transport, the exact-limit
pass-through, and the process-absence proof its execution model owes, and the
pinned sidecar owns everything that touches Basic Memory itself.
"""

from __future__ import annotations

import errno
import json
import os
import secrets
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from membench.adapters.base import AdapterEnvironmentError, Profile
from protocol.models import CaseHandle, LaneReadiness, ProtocolEvent

from .base import ProviderHit, ProviderSessionContext, RetrievalPurpose, require_neutral

PROVIDER_ID = "basic-memory-direct"
EXECUTION_MODEL = "owned-subprocess-terminated-at-cleanup"
SIDECAR_PROTOCOL_VERSION = 1
SIDECAR_RELATIVE = Path("benchmarks/memorybench/providers/basic-memory/sidecar.py")

#: Where the pinned, read-only Basic Memory checkout lives on this machine.
BASIC_HOME_ENV = "EXOMEM_BM_DIRECT_BASIC_HOME"

_READY_TIMEOUT_SECONDS = 60.0
_REQUEST_TIMEOUT_SECONDS = 300.0
_TERMINATE_GRACE_SECONDS = 10.0


class SidecarLaunchError(AdapterEnvironmentError):
    """The pinned sidecar could not be started or did not announce itself."""


class SidecarTransportError(RuntimeError):
    """A round trip failed, or the sidecar answered something unusable."""


@dataclass(frozen=True)
class SidecarLaunch:
    """How to start the owned sidecar.

    Hermetic contract tests inject the exact public seam here rather than
    reaching for Basic Memory, which is not present on every machine.
    """

    command: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)


def default_launch(basic_home: Path, repo_root: Path) -> SidecarLaunch:
    """Run the pinned sidecar under Basic Memory's own interpreter."""

    interpreter = basic_home / ".venv" / "bin" / "python"
    if not interpreter.exists():
        raise SidecarLaunchError(
            f"pinned Basic Memory interpreter is absent under {BASIC_HOME_ENV}"
        )
    sidecar = repo_root / SIDECAR_RELATIVE
    if not sidecar.exists():
        raise SidecarLaunchError("pinned sidecar module is absent from this checkout")
    benchmarks_src = basic_home / "benchmarks" / "src"
    if not benchmarks_src.exists():
        raise SidecarLaunchError(
            "pinned Basic Memory benchmarks package is absent under " + BASIC_HOME_ENV
        )
    return SidecarLaunch(
        command=(str(interpreter), str(sidecar)),
        env={"PYTHONPATH": str(benchmarks_src), "BASIC_MEMORY_HOME": str(basic_home)},
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


class OwnedSidecar:
    """One loopback sidecar process owned by exactly one provider instance.

    Ownership is the point: it is started in its own process group so that
    cleanup can terminate the whole group and then observe that nothing of it
    survived.  The provider never reports absence it has not looked for.
    """

    def __init__(self, launch: SidecarLaunch, *, work_root: Path, evidence_root: Path) -> None:
        self._launch = launch
        self._work_root = work_root
        self._evidence_root = evidence_root
        self._process: subprocess.Popen[str] | None = None
        self._base_url: str | None = None
        self._token = secrets.token_urlsafe(32)
        self._port: int | None = None
        # Retained past teardown so absence can be probed, not asserted.
        self._retired_pid: int | None = None
        self._retired_port: int | None = None

    @property
    def token(self) -> str:
        return self._token

    @property
    def port(self) -> int | None:
        return self._port

    def process_id(self) -> int | None:
        return None if self._process is None else self._process.pid

    def start(self) -> str:
        if self._process is not None:
            raise SidecarLaunchError("sidecar is already running")
        environment = dict(os.environ)
        environment.update(self._launch.env)
        environment.update({
            "MEMORYBENCH_GUEST_BEARER_TOKEN": self._token,
            "MEMORYBENCH_GUEST_WORK_ROOT": str(self._work_root),
            "MEMORYBENCH_GUEST_EVIDENCE_ROOT": str(self._evidence_root),
        })
        self._work_root.mkdir(parents=True, exist_ok=True)
        self._evidence_root.mkdir(parents=True, exist_ok=True)
        try:
            self._process = subprocess.Popen(  # noqa: S603 - fixed command, no shell
                list(self._launch.command),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise SidecarLaunchError("pinned sidecar could not be started") from exc
        self._base_url = self._await_ready()
        self._port = int(self._base_url.rsplit(":", 1)[1])
        return self._base_url

    def _await_ready(self) -> str:
        assert self._process is not None and self._process.stdout is not None
        deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            line = self._process.stdout.readline()
            if not line:
                break
            try:
                announcement = json.loads(line)
            except json.JSONDecodeError:
                continue
            if announcement.get("event") != "ready":
                continue
            if announcement.get("protocol_version") != SIDECAR_PROTOCOL_VERSION:
                self.terminate()
                raise SidecarLaunchError("sidecar announced an unsupported protocol version")
            base_url = announcement.get("base_url")
            if not isinstance(base_url, str) or not base_url.startswith("http://127.0.0.1:"):
                self.terminate()
                raise SidecarLaunchError("sidecar announced a non-loopback endpoint")
            return base_url
        self.terminate()
        raise SidecarLaunchError("sidecar did not announce readiness")

    def request(self, route: str, payload: dict[str, object]) -> dict[str, object]:
        if self._base_url is None:
            raise SidecarTransportError("sidecar is not running")
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - fixed loopback scheme
            self._base_url + route,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise SidecarTransportError(f"sidecar refused {route}: {exc.code} {detail}") from exc
        except OSError as exc:
            raise SidecarTransportError(f"sidecar is unreachable for {route}") from exc
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SidecarTransportError(f"sidecar returned non-JSON for {route}") from exc
        if not isinstance(envelope, dict) or envelope.get("ok") is not True:
            raise SidecarTransportError(f"sidecar reported failure for {route}")
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise SidecarTransportError(f"sidecar returned no data for {route}")
        return data

    def live_process_count(self) -> int:
        """Probe the owned PID; never infer death from our own bookkeeping.

        The PID and port are deliberately retained after termination so that
        post-cleanup absence is an observation of the real thing rather than a
        restatement of the fact that we set a field to None.
        """

        process = self._process
        if process is not None:
            return 0 if process.poll() is not None else 1
        pid = self._retired_pid
        if pid is None:
            return 0
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return 0
        except PermissionError:  # pragma: no cover - PID reused by another user
            return 1
        return 0 if _is_zombie(pid) else 1

    def listener_bound(self) -> bool:
        port = self._port if self._port is not None else self._retired_port
        if port is None:
            return False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            try:
                probe.connect(("127.0.0.1", port))
            except OSError:
                return False
        return True

    def terminate(self) -> None:
        """Terminate and reap the whole owned group; never report unverified death."""

        process = self._process
        if process is None:
            return
        try:
            group = os.getpgid(process.pid)
        except (ProcessLookupError, OSError):
            group = None
        for sender in (signal.SIGTERM, signal.SIGKILL):
            if process.poll() is not None:
                break
            if group is not None:
                try:
                    os.killpg(group, sender)
                except (ProcessLookupError, PermissionError):
                    pass
                except OSError as exc:  # pragma: no cover - platform specific
                    if exc.errno != errno.ESRCH:
                        raise
            else:  # pragma: no cover - group lookup only fails post-mortem
                process.send_signal(sender)
            try:
                process.wait(timeout=_TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                continue
        if process.poll() is None:  # pragma: no cover - SIGKILL is not refusable
            raise SidecarTransportError("owned sidecar could not be terminated")
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        self._retired_pid = process.pid
        self._retired_port = self._port
        self._process = None
        self._base_url = None
        self._port = None


class BasicMemoryDirectProvider:
    """Direct-lane row wrapping the pinned, unmodified competitor provider."""

    def __init__(self, launch: SidecarLaunch | None = None) -> None:
        # Construction happens before the started manifest and must not need
        # the competitor environment, so resolution is deferred to setup.
        self._explicit_launch = launch
        self._sidecar: OwnedSidecar | None = None
        self._context: ProviderSessionContext | None = None
        self._container_tag: str | None = None
        self._documents: list[str] = []
        self._readiness: list[LaneReadiness] = []
        self._transport_log: list[dict[str, object]] = []

    # -- DirectProvider -------------------------------------------------

    def setup(self, profile: Profile | None, context: ProviderSessionContext) -> None:
        del profile
        self._context = context
        self._container_tag = context.namespace
        launch = self._explicit_launch or self._resolve_launch()
        sidecar = OwnedSidecar(
            launch, work_root=context.work_root, evidence_root=context.evidence_root
        )
        sidecar.start()
        self._sidecar = sidecar

    def ingest_case(self, events: Sequence[ProtocolEvent], handle: CaseHandle) -> tuple[str, ...]:
        require_neutral(events, handle)
        self._require_sidecar()
        documents: list[str] = []
        for position, (session_ordinal, session_events) in enumerate(_sessions(events)):
            payload = {
                "protocol_version": SIDECAR_PROTOCOL_VERSION,
                "request_id": str(uuid.uuid4()),
                "container_tag": self._container_tag,
                "session": {
                    "session_id": f"{handle.case_id}-s{session_ordinal}",
                    "position": position,
                    "messages": [
                        {"role": event.role, "content": event.content} for event in session_events
                    ],
                    **_session_date(session_events),
                },
            }
            data = self._round_trip("/v1/ingest", payload)
            document = data.get("neutral_document_id")
            if not isinstance(document, str) or not document:
                raise SidecarTransportError("sidecar ingest returned no document identity")
            documents.append(document)
            self._record_readiness(data)
        self._documents = documents
        return tuple(documents)

    def retrieve(self, question_text: str, top_k: int, purpose: RetrievalPurpose) -> list[ProviderHit]:
        del purpose
        self._require_sidecar()
        # The audited defect on the competitor's own MemoryBench provider was a
        # hardcoded wider limit; the requested top_k crosses once, unaltered.
        payload = {
            "protocol_version": SIDECAR_PROTOCOL_VERSION,
            "request_id": str(uuid.uuid4()),
            "container_tag": self._container_tag,
            "query": question_text,
            "limit": top_k,
        }
        data = self._round_trip("/v1/search", payload)
        raw_hits = data.get("hits")
        if not isinstance(raw_hits, list):
            raise SidecarTransportError("sidecar search returned no hits list")
        if len(raw_hits) > top_k:
            raise SidecarTransportError("sidecar search exceeded the requested limit")
        return [_hit(index, row) for index, row in enumerate(raw_hits)]

    def export_state(self) -> tuple[object, ...]:
        return tuple(self._transport_log)

    def cleanup(self) -> None:
        sidecar = self._sidecar
        try:
            if sidecar is not None and sidecar.live_process_count():
                try:
                    self._round_trip("/v1/cleanup", {
                        "protocol_version": SIDECAR_PROTOCOL_VERSION,
                        "request_id": str(uuid.uuid4()),
                        "container_tag": self._container_tag,
                    })
                except SidecarTransportError:
                    # Teardown proceeds regardless; absence is proven by
                    # observation afterwards, never by this call succeeding.
                    pass
        finally:
            if sidecar is not None:
                sidecar.terminate()
            self._context = None
            self._container_tag = None
            self._documents = []

    def variant_id(self) -> str:
        return PROVIDER_ID

    def readiness(self) -> list[LaneReadiness]:
        if self._readiness:
            return list(self._readiness)
        return [LaneReadiness(
            lane="semantic",
            requested=True,
            verified=False,
            method="readiness-unverifiable",
            evidence="no sidecar ingest readiness receipt has been recorded yet",
        )]

    # -- Runner-observable surfaces -------------------------------------

    def live_process_count(self) -> int:
        return 0 if self._sidecar is None else self._sidecar.live_process_count()

    def listener_bound(self) -> bool:
        return False if self._sidecar is None else self._sidecar.listener_bound()

    def owned_process_id(self) -> int | None:
        return None if self._sidecar is None else self._sidecar.process_id()

    def endpoint_port(self) -> int | None:
        return None if self._sidecar is None else self._sidecar.port

    def bearer_token(self) -> str | None:
        return None if self._sidecar is None else self._sidecar.token

    def transport_log(self) -> tuple[dict[str, object], ...]:
        return tuple(self._transport_log)

    def transport_log_document_ids(self) -> tuple[str, ...]:
        """Documents this instance still claims; cleared only by cleanup."""

        return tuple(self._documents)

    # -- internals ------------------------------------------------------

    def _resolve_launch(self) -> SidecarLaunch:
        raw = os.environ.get(BASIC_HOME_ENV)
        if not raw:
            raise SidecarLaunchError(
                f"{BASIC_HOME_ENV} must name the pinned read-only Basic Memory checkout"
            )
        basic_home = Path(raw)
        if not basic_home.is_dir():
            raise SidecarLaunchError(f"{BASIC_HOME_ENV} does not name a directory")
        return default_launch(basic_home, _repo_root())

    def _require_sidecar(self) -> OwnedSidecar:
        if self._sidecar is None:
            raise SidecarTransportError("provider method called before setup")
        return self._sidecar

    def _round_trip(self, route: str, payload: dict[str, object]) -> dict[str, object]:
        sidecar = self._require_sidecar()
        data = sidecar.request(route, payload)
        self._transport_log.append({"route": route, "payload": payload})
        return data

    def _record_readiness(self, data: dict[str, object]) -> None:
        raw = data.get("readiness")
        if not isinstance(raw, dict):
            return
        fallback = bool(raw.get("fallback_detected"))
        self._readiness = [LaneReadiness(
            lane="semantic",
            requested=True,
            verified=not fallback,
            method="index-count" if not fallback else "readiness-unverifiable",
            evidence=_readiness_evidence(raw),
            fallback_detected=fallback,
        )]


def _readiness_evidence(raw: dict[str, object]) -> str:
    """Quote the sidecar's own receipt; say so plainly when it carried none."""

    facts = {key: raw[key] for key in sorted(raw) if key != "fallback_detected"}
    if not facts:
        return "sidecar ingest receipt carried no positive index evidence"
    return json.dumps(facts, sort_keys=True)


def _is_zombie(pid: int) -> bool:
    """A reaped-but-unwaited child is not a surviving process."""

    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            return handle.read().rsplit(") ", 1)[1].split(" ", 1)[0] == "Z"
    except (FileNotFoundError, IndexError, PermissionError):  # pragma: no cover
        return False


def _sessions(events: Sequence[ProtocolEvent]) -> list[tuple[int, list[ProtocolEvent]]]:
    grouped: dict[int, list[ProtocolEvent]] = {}
    for event in events:
        grouped.setdefault(event.session_ordinal, []).append(event)
    return [
        (ordinal, sorted(grouped[ordinal], key=lambda item: (item.sequence, item.turn_ordinal)))
        for ordinal in sorted(grouped)
    ]


def _session_date(events: Sequence[ProtocolEvent]) -> dict[str, str]:
    for event in events:
        if event.original_timestamp:
            return {"date": event.original_timestamp}
    return {}


def _hit(index: int, row: object) -> ProviderHit:
    if not isinstance(row, dict):
        raise SidecarTransportError("sidecar search hit is not an object")
    text = row.get("content")
    if not isinstance(text, str):
        text = json.dumps(row, sort_keys=True)
    raw_score = row.get("score")
    score = float(raw_score) if isinstance(raw_score, (int, float)) else 0.0
    identity = row.get("id")
    hit_id = identity if isinstance(identity, str) and identity else f"basic-memory-{index}"
    return ProviderHit(hit_id, text, score)
