"""Exclusive provider-capability broker and invocation-receipt audit."""

from __future__ import annotations

import ast
import builtins
import contextvars
import hashlib
import hmac
import json
import os
import secrets
import selectors
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, field_validator

from .schema import PrivilegedEndpointMatrixEntry
from .snapshot import StrictModel

_SHA256 = r"^[0-9a-f]{64}$"
_BROKER_ACTIVE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "epistemic_broker_active", default=False
)
_IPC_PROTOCOL = "epistemic-driver-ipc.v1"
_MAX_IPC_MESSAGE_BYTES = 64 * 1024
_MAX_IPC_MESSAGES = 256
_MAX_IPC_DEPTH = 32
_MAX_DRIVER_SOURCE_BYTES = 512 * 1024
_MAX_STDERR_BYTES = 64 * 1024
_MAX_PENDING_IPC_BYTES = 64 * 1024
_TRUSTED_BWRAP = Path("/usr/bin/bwrap")
_SYSTEM_PYTHON = Path("/usr/bin/python3.12")
_SYSTEM_STDLIB = Path("/usr/lib/python3.12")
_SYSTEM_LIBS = Path("/lib/x86_64-linux-gnu")
_SYSTEM_LOADER = Path("/lib64/ld-linux-x86-64.so.2")
_NO_DRIVER_RESULT = object()


class BrokerContractError(ValueError):
    """Provider access or receipt evidence violates the broker contract."""


class BrokerSurfaceTimeout(BrokerContractError):
    """A trusted provider surface exceeded the sandbox execution deadline."""


def _require_surface_timer_ownership() -> None:
    """Refuse provider work unless this process can own a real-time timer safely."""

    if threading.current_thread() is not threading.main_thread():
        raise BrokerContractError(
            "provider surfaces require POSIX timer ownership on the main thread"
        )
    required = ("SIGALRM", "ITIMER_REAL", "getitimer", "setitimer", "getsignal", "signal")
    if any(not hasattr(signal, name) for name in required):
        raise BrokerContractError("POSIX provider surface deadline primitives are unavailable")
    try:
        delay, interval = signal.getitimer(signal.ITIMER_REAL)
    except (OSError, ValueError) as exc:
        raise BrokerContractError("POSIX provider surface timer state is unavailable") from exc
    if delay > 0.0 or interval > 0.0:
        raise BrokerContractError("provider surface deadline timer ownership is unavailable")


@contextmanager
def _surface_deadline(expires_at: float) -> Iterator[None]:
    """Temporarily bound one synchronous parent-side provider callback."""

    _require_surface_timer_ownership()
    remaining = expires_at - time.monotonic()
    if remaining <= 0.0:
        raise BrokerSurfaceTimeout("sandbox driver provider surface deadline exceeded")
    previous_handler = signal.getsignal(signal.SIGALRM)
    handler_installed = False
    timer_armed = False

    def deadline_handler(_signum: int, _frame: object) -> None:
        raise BrokerSurfaceTimeout("sandbox driver provider surface deadline exceeded")

    try:
        signal.signal(signal.SIGALRM, deadline_handler)
        handler_installed = True
        signal.setitimer(signal.ITIMER_REAL, remaining)
        timer_armed = True
        yield
    finally:
        restoration_error: Exception | None = None
        if timer_armed:
            try:
                signal.setitimer(signal.ITIMER_REAL, 0.0)
            except (OSError, ValueError) as exc:
                restoration_error = exc
        if handler_installed:
            try:
                signal.signal(signal.SIGALRM, previous_handler)
            except (OSError, ValueError) as exc:
                restoration_error = restoration_error or exc
        if restoration_error is not None:
            raise BrokerContractError(
                "POSIX provider surface deadline state could not be restored"
            ) from restoration_error


def _canonical_path(value: str) -> str:
    if not value or "\\" in value:
        raise ValueError("path must be a canonical relative POSIX path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("path must be a canonical relative POSIX path")
    return path.as_posix()


class InvocationReceiptRef(StrictModel):
    path: str
    sha256: str = Field(pattern=_SHA256)

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        return _canonical_path(value)


class InvocationReceipt(StrictModel):
    ordinal: int = Field(ge=1)
    provider: str = Field(min_length=1)
    variant: str = Field(min_length=1)
    driver_surface_id: str = Field(min_length=1)
    arguments_sha256: str = Field(pattern=_SHA256)
    outcome: Literal["success", "exception"]
    result_sha256: str = Field(pattern=_SHA256)
    exception_type: str | None
    previous_receipt_sha256: str | None = Field(pattern=_SHA256)
    sealed: Literal[True]
    receipt_sha256: str = Field(pattern=_SHA256)


class InvocationReceiptLog(StrictModel):
    artifact_type: Literal["driver-invocation-receipts.v1"]
    schema_version: Literal[1]
    provider: str
    variant: str
    session_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    driver_source_sha256: str = Field(pattern=_SHA256)
    worker_sha256: str = Field(pattern=_SHA256)
    bwrap_binary_sha256: str = Field(pattern=_SHA256)
    bwrap_profile_sha256: str = Field(pattern=_SHA256)
    receipts: tuple[InvocationReceipt, ...]
    log_hmac_sha256: str = Field(pattern=_SHA256)


class EndpointAudit(StrictModel):
    comparable: bool
    exclusions: tuple[str, ...] = ()
    invoked_surfaces: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SandboxExecutionAttestation:
    """Live parent-minted proof that one exact sandbox execution produced a log."""

    provider: str
    variant: str
    receipt_ref: InvocationReceiptRef
    driver_source_sha256: str
    worker_sha256: str
    bwrap_binary_sha256: str
    bwrap_profile_sha256: str
    session_id: str
    log_hmac_sha256: str
    attestation_hmac_sha256: str
    _broker_marker: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class SandboxDriverResult:
    """A driver value coupled to its non-serializable live execution proof."""

    value: object
    receipt_ref: InvocationReceiptRef
    attestation: SandboxExecutionAttestation


@dataclass(slots=True)
class _SandboxSession:
    secret: bytes
    driver_source_sha256: str
    worker_sha256: str
    bwrap_binary_sha256: str
    bwrap_profile_sha256: str
    receipts: list[InvocationReceipt] = field(default_factory=list)
    complete: bool = False
    receipt_ref: InvocationReceiptRef | None = None
    log_hmac_sha256: str | None = None
    attestation_hmac_sha256: str | None = None


@runtime_checkable
class BrokerInterface(Protocol):
    def invoke(self, driver_surface_id: str, *args: object, **kwargs: object) -> object: ...


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hmac_sha256(secret: bytes, value: object) -> str:
    return hmac.new(secret, _canonical_json(value), hashlib.sha256).hexdigest()


def _receipt_digest(payload: Mapping[str, object]) -> str:
    return _digest(dict(payload))


def _json_depth_is_bounded(value: object) -> bool:
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > _MAX_IPC_DEPTH:
            return False
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return True


def _decode_ipc_message(raw: bytes) -> dict[str, object]:
    if not raw or len(raw) > _MAX_IPC_MESSAGE_BYTES or not raw.endswith(b"\n"):
        raise BrokerContractError("driver IPC message framing or size is invalid")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise BrokerContractError("driver IPC message has duplicate JSON members")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw,
            object_pairs_hook=unique,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                BrokerContractError("driver IPC message has a nonfinite value")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise BrokerContractError("driver IPC message is malformed") from exc
    if not isinstance(value, dict) or not _json_depth_is_bounded(value):
        raise BrokerContractError("driver IPC message shape or depth is invalid")
    if value.get("protocol") != _IPC_PROTOCOL:
        raise BrokerContractError("driver IPC protocol confusion")
    if value.get("type") not in {"invoke", "complete", "driver_exception"}:
        raise BrokerContractError("driver IPC message envelope is unknown")
    return value


def _encode_ipc_message(payload: Mapping[str, object]) -> bytes:
    try:
        encoded = (
            json.dumps(
                dict(payload),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BrokerContractError("broker result is not serializable for driver IPC") from exc
    if len(encoded) > _MAX_IPC_MESSAGE_BYTES or not _json_depth_is_bounded(dict(payload)):
        raise BrokerContractError("broker result exceeds driver IPC bounds")
    return encoded


def _resolve_bwrap() -> Path:
    candidate = shutil.which("bwrap", path=os.defpath)
    if candidate is None:
        raise BrokerContractError("bwrap sandbox is unavailable before provider execution")
    try:
        resolved = Path(candidate).resolve(strict=True)
        expected = _TRUSTED_BWRAP.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise BrokerContractError("bwrap sandbox executable cannot be verified") from exc
    if resolved != expected or not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise BrokerContractError("bwrap sandbox executable is not the trusted system binary")
    if metadata.st_mode & 0o022:
        raise BrokerContractError("bwrap sandbox executable is group/world writable")
    return resolved


def _sandbox_command(bwrap: Path, worker: Path, driver: Path) -> tuple[str, ...]:
    runtime_paths = (_SYSTEM_PYTHON, _SYSTEM_STDLIB, _SYSTEM_LIBS, _SYSTEM_LOADER, worker, driver)
    if any(not path.exists() for path in runtime_paths):
        raise BrokerContractError("sandbox system runtime or bound worker bytes are unavailable")
    return (
        str(bwrap),
        "--unshare-all",
        "--new-session",
        "--die-with-parent",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--setenv",
        "PATH",
        "/runtime",
        "--setenv",
        "PYTHONDONTWRITEBYTECODE",
        "1",
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--dir",
        "/runtime",
        "--dir",
        "/usr",
        "--dir",
        "/usr/lib",
        "--dir",
        "/lib",
        "--dir",
        "/lib/x86_64-linux-gnu",
        "--dir",
        "/lib64",
        "--dir",
        "/worker",
        "--ro-bind",
        str(_SYSTEM_PYTHON),
        "/runtime/python",
        "--ro-bind",
        str(_SYSTEM_STDLIB),
        str(_SYSTEM_STDLIB),
        "--ro-bind",
        str(_SYSTEM_LIBS),
        str(_SYSTEM_LIBS),
        "--ro-bind",
        str(_SYSTEM_LOADER),
        str(_SYSTEM_LOADER),
        "--ro-bind",
        str(worker),
        "/worker/driver_worker.py",
        "--ro-bind",
        str(driver),
        "/worker/driver.py",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/work",
        "--chdir",
        "/work",
        "--",
        "/runtime/python",
        "-I",
        "-S",
        "-B",
        "/worker/driver_worker.py",
        "/worker/driver.py",
    )


class ProviderBroker:
    """Parent-owned provider capabilities and sealed invocation receipts."""

    __slots__ = (
        "_ProviderBroker__provider",
        "_ProviderBroker__variant",
        "_ProviderBroker__surfaces",
        "_ProviderBroker__receipt_path",
        "_ProviderBroker__run_root",
        "_ProviderBroker__broker_marker",
        "_ProviderBroker__sessions",
        "_ProviderBroker__active_session_id",
        "_ProviderBroker__credentials",
        "_ProviderBroker__sockets",
        "_ProviderBroker__sdk_clients",
        "_ProviderBroker__cli_commands",
        "_ProviderBroker__filesystem_roots",
    )

    def __init__(
        self,
        *,
        provider: str,
        variant: str,
        surfaces: Mapping[str, Callable[..., object]],
        receipt_path: Path | str,
        run_root: Path | str | None = None,
        credentials: Mapping[str, object] | None = None,
        sockets: Mapping[str, object] | None = None,
        sdk_clients: Mapping[str, object] | None = None,
        cli_commands: Mapping[str, object] | None = None,
        filesystem_roots: Mapping[str, object] | None = None,
    ) -> None:
        path = Path(receipt_path).resolve()
        inferred_root = path.parent.parent if path.parent.name == "receipts" else path.parent
        root = Path(run_root).resolve() if run_root is not None else inferred_root
        if not path.is_relative_to(root):
            raise BrokerContractError("receipt path must be beneath the run root")
        self.__provider = provider
        self.__variant = variant
        self.__surfaces = dict(surfaces)
        self.__receipt_path = path
        self.__run_root = root
        self.__broker_marker = object()
        self.__sessions: dict[str, _SandboxSession] = {}
        self.__active_session_id: str | None = None
        self.__credentials = dict(credentials or {})
        self.__sockets = dict(sockets or {})
        self.__sdk_clients = dict(sdk_clients or {})
        self.__cli_commands = dict(cli_commands or {})
        self.__filesystem_roots = dict(filesystem_roots or {})

    def __invoke_session(
        self,
        session_id: str,
        expires_at: float,
        driver_surface_id: str,
        *args: object,
        **kwargs: object,
    ) -> object:
        session = self.__sessions.get(session_id)
        if (
            session is None
            or self.__active_session_id != session_id
            or session.complete
        ):
            raise BrokerContractError(
                "provider invocation is outside a broker-minted sandbox session"
            )
        try:
            operation = self.__surfaces[driver_surface_id]
        except KeyError as exc:
            raise BrokerContractError(f"broker surface is not provisioned: {driver_surface_id}") from exc
        arguments_sha256 = _digest({"args": list(args), "kwargs": kwargs})
        token = _BROKER_ACTIVE.set(True)
        try:
            with _surface_deadline(expires_at):
                result = operation(*args, **kwargs)
        except Exception as exc:
            self.__append(
                session_id=session_id,
                driver_surface_id=driver_surface_id,
                arguments_sha256=arguments_sha256,
                outcome="exception",
                result_sha256=_digest({"type": type(exc).__name__, "message": str(exc)}),
                exception_type=type(exc).__name__,
            )
            raise
        finally:
            _BROKER_ACTIVE.reset(token)
        self.__append(
            session_id=session_id,
            driver_surface_id=driver_surface_id,
            arguments_sha256=arguments_sha256,
            outcome="success",
            result_sha256=_digest(result),
            exception_type=None,
        )
        return result

    def __append(
        self,
        *,
        session_id: str,
        driver_surface_id: str,
        arguments_sha256: str,
        outcome: Literal["success", "exception"],
        result_sha256: str,
        exception_type: str | None,
    ) -> None:
        session = self.__sessions.get(session_id)
        if session is None or self.__active_session_id != session_id or session.complete:
            raise BrokerContractError("receipt append is outside an active sandbox session")
        unsigned: dict[str, object] = {
            "ordinal": len(session.receipts) + 1,
            "provider": self.__provider,
            "variant": self.__variant,
            "driver_surface_id": driver_surface_id,
            "arguments_sha256": arguments_sha256,
            "outcome": outcome,
            "result_sha256": result_sha256,
            "exception_type": exception_type,
            "previous_receipt_sha256": (
                session.receipts[-1].receipt_sha256 if session.receipts else None
            ),
            "sealed": True,
        }
        session.receipts.append(
            InvocationReceipt(**unsigned, receipt_sha256=_receipt_digest(unsigned))
        )
        self.__persist_session(session_id)

    def __unsigned_log(self, session_id: str) -> dict[str, object]:
        session = self.__sessions.get(session_id)
        if session is None:
            raise BrokerContractError("sandbox session is unknown")
        return {
            "artifact_type": "driver-invocation-receipts.v1",
            "schema_version": 1,
            "provider": self.__provider,
            "variant": self.__variant,
            "session_id": session_id,
            "driver_source_sha256": session.driver_source_sha256,
            "worker_sha256": session.worker_sha256,
            "bwrap_binary_sha256": session.bwrap_binary_sha256,
            "bwrap_profile_sha256": session.bwrap_profile_sha256,
            "receipts": tuple(
                receipt.model_dump(mode="json") for receipt in session.receipts
            ),
        }

    def __persist_session(self, session_id: str) -> InvocationReceiptRef:
        session = self.__sessions.get(session_id)
        if session is None:
            raise BrokerContractError("sandbox session is unknown")
        unsigned = self.__unsigned_log(session_id)
        log_hmac_sha256 = _hmac_sha256(session.secret, unsigned)
        payload = InvocationReceiptLog(
            **unsigned,
            log_hmac_sha256=log_hmac_sha256,
        )
        token = _BROKER_ACTIVE.set(True)
        try:
            self.__receipt_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.__receipt_path.with_name(self.__receipt_path.name + ".tmp")
            temporary.write_text(payload.model_dump_json(indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.__receipt_path)
        finally:
            _BROKER_ACTIVE.reset(token)
        data = self.__receipt_path.read_bytes()
        reference = InvocationReceiptRef(
            path=self.__receipt_path.relative_to(self.__run_root).as_posix(),
            sha256=_sha256_bytes(data),
        )
        session.receipt_ref = reference
        session.log_hmac_sha256 = log_hmac_sha256
        return reference

    def receipt_ref(self) -> InvocationReceiptRef:
        try:
            data = self.__receipt_path.read_bytes()
        except OSError as exc:
            raise BrokerContractError("no sandbox invocation receipt exists") from exc
        return InvocationReceiptRef(
            path=self.__receipt_path.relative_to(self.__run_root).as_posix(),
            sha256=_sha256_bytes(data),
        )

    def __attestation_payload(
        self, session_id: str, reference: InvocationReceiptRef
    ) -> dict[str, object]:
        session = self.__sessions.get(session_id)
        if session is None or session.log_hmac_sha256 is None:
            raise BrokerContractError("sandbox session has no authenticated receipt log")
        return {
            "provider": self.__provider,
            "variant": self.__variant,
            "receipt_ref": reference.model_dump(mode="json"),
            "driver_source_sha256": session.driver_source_sha256,
            "worker_sha256": session.worker_sha256,
            "bwrap_binary_sha256": session.bwrap_binary_sha256,
            "bwrap_profile_sha256": session.bwrap_profile_sha256,
            "session_id": session_id,
            "log_hmac_sha256": session.log_hmac_sha256,
        }

    def _verify_sandbox_driver_result(
        self,
        *,
        run_root: Path | str,
        driver_result: SandboxDriverResult,
        provider: str,
        variant: str,
    ) -> InvocationReceiptLog:
        if not isinstance(driver_result, SandboxDriverResult):
            raise BrokerContractError("sandbox driver result attestation is required")
        if Path(run_root).resolve() != self.__run_root:
            raise BrokerContractError("audit run root differs from the broker binding")
        attestation = driver_result.attestation
        if attestation._broker_marker is not self.__broker_marker:
            raise BrokerContractError("sandbox execution attestation belongs to another broker")
        if driver_result.receipt_ref != attestation.receipt_ref:
            raise BrokerContractError("sandbox result and attestation receipt references differ")
        if (provider, variant) != (self.__provider, self.__variant):
            raise BrokerContractError("audit provider/variant differs from broker binding")
        if (attestation.provider, attestation.variant) != (provider, variant):
            raise BrokerContractError("sandbox attestation provider/variant binding differs")
        session = self.__sessions.get(attestation.session_id)
        if session is None or not session.complete:
            raise BrokerContractError("sandbox execution session is unknown or incomplete")
        if session.receipt_ref != driver_result.receipt_ref:
            raise BrokerContractError("sandbox session receipt reference differs")
        expected_fields = (
            session.driver_source_sha256,
            session.worker_sha256,
            session.bwrap_binary_sha256,
            session.bwrap_profile_sha256,
            session.log_hmac_sha256,
        )
        actual_fields = (
            attestation.driver_source_sha256,
            attestation.worker_sha256,
            attestation.bwrap_binary_sha256,
            attestation.bwrap_profile_sha256,
            attestation.log_hmac_sha256,
        )
        if expected_fields != actual_fields:
            raise BrokerContractError("sandbox execution attestation binding differs")
        expected_attestation_hmac = _hmac_sha256(
            session.secret,
            self.__attestation_payload(attestation.session_id, driver_result.receipt_ref),
        )
        if not hmac.compare_digest(
            expected_attestation_hmac, attestation.attestation_hmac_sha256
        ) or not hmac.compare_digest(
            expected_attestation_hmac, session.attestation_hmac_sha256 or ""
        ):
            raise BrokerContractError("sandbox execution attestation authentication differs")

        data = _read_no_follow(Path(run_root), driver_result.receipt_ref.path)
        if not hmac.compare_digest(
            _sha256_bytes(data), driver_result.receipt_ref.sha256
        ):
            raise BrokerContractError("invocation receipt changed: digest mismatch")
        try:
            log = InvocationReceiptLog.model_validate_json(data)
        except Exception as exc:  # noqa: BLE001
            raise BrokerContractError("invocation receipt is unsealed or schema-invalid") from exc
        if (
            log.provider,
            log.variant,
            log.session_id,
            log.driver_source_sha256,
            log.worker_sha256,
            log.bwrap_binary_sha256,
            log.bwrap_profile_sha256,
        ) != (
            attestation.provider,
            attestation.variant,
            attestation.session_id,
            attestation.driver_source_sha256,
            attestation.worker_sha256,
            attestation.bwrap_binary_sha256,
            attestation.bwrap_profile_sha256,
        ):
            raise BrokerContractError("invocation receipt and execution attestation differ")
        unsigned_log = log.model_dump(mode="json", exclude={"log_hmac_sha256"})
        expected_log_hmac = _hmac_sha256(session.secret, unsigned_log)
        if not hmac.compare_digest(expected_log_hmac, log.log_hmac_sha256):
            raise BrokerContractError("invocation receipt log authentication differs")
        if not hmac.compare_digest(expected_log_hmac, attestation.log_hmac_sha256):
            raise BrokerContractError("attested invocation receipt authentication differs")
        return log

    def run_driver(self, source: str, *, timeout_s: float = 30.0) -> SandboxDriverResult:
        """Execute driver bytes in a fail-closed Bubblewrap capability boundary."""

        if not isinstance(source, str) or not source:
            raise BrokerContractError("sandbox driver source must be non-empty text")
        source_bytes = source.encode("utf-8")
        if len(source_bytes) > _MAX_DRIVER_SOURCE_BYTES:
            raise BrokerContractError("sandbox driver source exceeds the size bound")
        if not isinstance(timeout_s, int | float) or isinstance(timeout_s, bool) or timeout_s <= 0:
            raise BrokerContractError("sandbox driver deadline must be positive")
        expires_at = time.monotonic() + float(timeout_s)
        _require_surface_timer_ownership()
        if self.__active_session_id is not None:
            raise BrokerContractError("a sandbox driver session is already active")
        validate_driver_source(source, filename="sandboxed_driver.py")
        bwrap = _resolve_bwrap()
        worker = Path(__file__).with_name("driver_worker.py").resolve(strict=True)
        try:
            worker_bytes = worker.read_bytes()
            bwrap_bytes = bwrap.read_bytes()
        except OSError as exc:
            raise BrokerContractError("sandbox runtime bytes cannot be authenticated") from exc
        self.__run_root.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(
            prefix=".epistemic-driver-", suffix=".py", dir=self.__run_root
        )
        driver_path = Path(raw_path)
        process: subprocess.Popen[bytes] | None = None
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(source_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            command = _sandbox_command(bwrap, worker, driver_path.resolve(strict=True))
            session_id = secrets.token_hex(16)
            session = _SandboxSession(
                secret=secrets.token_bytes(32),
                driver_source_sha256=_sha256_bytes(source_bytes),
                worker_sha256=_sha256_bytes(worker_bytes),
                bwrap_binary_sha256=_sha256_bytes(bwrap_bytes),
                bwrap_profile_sha256=_digest({"argv": list(command)}),
            )
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd="/",
                    env={},
                    close_fds=True,
                    bufsize=0,
                )
            except OSError as exc:
                raise BrokerContractError(
                    "bwrap sandbox namespace failed before provider execution"
                ) from exc
            assert process.stdin is not None
            assert process.stdout is not None
            assert process.stderr is not None
            self.__sessions[session_id] = session
            self.__active_session_id = session_id
            value = self._serve_driver_process(
                process, expires_at=expires_at, session_id=session_id
            )
            reference = self.__persist_session(session_id)
            session.complete = True
            attestation_payload = self.__attestation_payload(session_id, reference)
            attestation_hmac = _hmac_sha256(session.secret, attestation_payload)
            session.attestation_hmac_sha256 = attestation_hmac
            attestation = SandboxExecutionAttestation(
                provider=self.__provider,
                variant=self.__variant,
                receipt_ref=reference,
                driver_source_sha256=session.driver_source_sha256,
                worker_sha256=session.worker_sha256,
                bwrap_binary_sha256=session.bwrap_binary_sha256,
                bwrap_profile_sha256=session.bwrap_profile_sha256,
                session_id=session_id,
                log_hmac_sha256=session.log_hmac_sha256 or "",
                attestation_hmac_sha256=attestation_hmac,
                _broker_marker=self.__broker_marker,
            )
            return SandboxDriverResult(
                value=value,
                receipt_ref=reference,
                attestation=attestation,
            )
        finally:
            self.__active_session_id = None
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            driver_path.unlink(missing_ok=True)

    def _serve_driver_process(
        self,
        process: subprocess.Popen[bytes],
        *,
        expires_at: float,
        session_id: str,
    ) -> object:
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        os.set_blocking(process.stderr.fileno(), False)
        stdout_buffer = bytearray()
        pending_reply = bytearray()
        stderr_bytes = 0
        message_count = 0
        next_request_id = 1
        completed: object = _NO_DRIVER_RESULT
        try:
            while selector.get_map():
                remaining = expires_at - time.monotonic()
                if remaining <= 0:
                    raise BrokerContractError("sandbox driver deadline exceeded")
                events = selector.select(min(remaining, 0.1))
                if not events:
                    if process.poll() is not None:
                        for registered in tuple(selector.get_map().values()):
                            selector.unregister(registered.fileobj)
                        break
                    continue
                for key, _mask in events:
                    if key.data == "stdin":
                        try:
                            written = os.write(process.stdin.fileno(), pending_reply)
                        except BlockingIOError:
                            continue
                        except (BrokenPipeError, OSError) as exc:
                            raise BrokerContractError(
                                "sandbox driver IPC reply channel closed"
                            ) from exc
                        if written <= 0:
                            raise BrokerContractError("sandbox driver IPC reply made no progress")
                        del pending_reply[:written]
                        if not pending_reply:
                            selector.unregister(process.stdin)
                        continue
                    try:
                        chunk = os.read(key.fileobj.fileno(), 4096)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == "stderr":
                        stderr_bytes += len(chunk)
                        if stderr_bytes > _MAX_STDERR_BYTES:
                            raise BrokerContractError("sandbox driver stderr exceeded bound")
                        continue
                    stdout_buffer.extend(chunk)
                    if b"\n" not in stdout_buffer and len(stdout_buffer) > _MAX_IPC_MESSAGE_BYTES:
                        raise BrokerContractError("driver IPC message exceeds size bound")
                    while b"\n" in stdout_buffer:
                        newline = stdout_buffer.index(b"\n")
                        raw = bytes(stdout_buffer[: newline + 1])
                        del stdout_buffer[: newline + 1]
                        message_count += 1
                        if message_count > _MAX_IPC_MESSAGES:
                            raise BrokerContractError("driver IPC message count exceeds bound")
                        if completed is not _NO_DRIVER_RESULT:
                            raise BrokerContractError("driver IPC emitted data after completion")
                        message = _decode_ipc_message(raw)
                        kind = message["type"]
                        if kind == "invoke":
                            if set(message) != {
                                "protocol", "type", "id", "surface", "args", "kwargs"
                            }:
                                raise BrokerContractError("driver IPC invoke envelope differs")
                            request_id = message["id"]
                            surface = message["surface"]
                            args = message["args"]
                            kwargs = message["kwargs"]
                            if (
                                not isinstance(request_id, int)
                                or isinstance(request_id, bool)
                                or request_id != next_request_id
                                or not isinstance(surface, str)
                                or not surface
                                or not isinstance(args, list)
                                or not isinstance(kwargs, dict)
                            ):
                                raise BrokerContractError("driver IPC invoke binding is invalid")
                            next_request_id += 1
                            if surface not in self.__surfaces:
                                raise BrokerContractError(
                                    f"sandbox driver requested undeclared surface: {surface}"
                                )
                            try:
                                result = self.__invoke_session(
                                    session_id, expires_at, surface, *args, **kwargs
                                )
                            except BrokerContractError:
                                raise
                            except Exception:  # noqa: BLE001 - receipt sealed by broker
                                reply = {
                                    "protocol": _IPC_PROTOCOL,
                                    "type": "result",
                                    "id": request_id,
                                    "ok": False,
                                    "error": "provider_exception",
                                }
                            else:
                                reply = {
                                    "protocol": _IPC_PROTOCOL,
                                    "type": "result",
                                    "id": request_id,
                                    "ok": True,
                                    "result": result,
                                }
                            encoded_reply = _encode_ipc_message(reply)
                            if len(pending_reply) + len(encoded_reply) > _MAX_PENDING_IPC_BYTES:
                                raise BrokerContractError(
                                    "sandbox driver IPC pending reply bound exceeded"
                                )
                            pending_reply.extend(encoded_reply)
                            if process.stdin not in (
                                registered.fileobj
                                for registered in selector.get_map().values()
                            ):
                                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                        elif kind == "complete":
                            if set(message) != {"protocol", "type", "result"}:
                                raise BrokerContractError("driver IPC completion envelope differs")
                            if pending_reply:
                                raise BrokerContractError(
                                    "sandbox driver completed before reading broker replies"
                                )
                            completed = message["result"]
                            process.stdin.close()
                        else:
                            if set(message) != {"protocol", "type", "exception_type"} or not isinstance(
                                message.get("exception_type"), str
                            ):
                                raise BrokerContractError("driver IPC exception envelope differs")
                            raise BrokerContractError(
                                "sandbox driver exited with an exception: "
                                f"{message['exception_type']}"
                            )
            if stdout_buffer:
                raise BrokerContractError("driver IPC ended with a partial message")
            remaining = max(0.0, expires_at - time.monotonic())
            try:
                return_code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                raise BrokerContractError("sandbox driver deadline exceeded") from exc
            if return_code != 0:
                raise BrokerContractError("bwrap sandbox or driver exited abnormally")
            if completed is _NO_DRIVER_RESULT:
                raise BrokerContractError("sandbox driver exited without a sealed completion")
            return completed
        finally:
            selector.close()


def _read_no_follow(root: Path, relative: str) -> bytes:
    # `dir_fd` is the whole mechanism here: each component is opened
    # relative to the previous descriptor so no symlink can redirect the
    # walk between checks. Windows supports no directory descriptors at all
    # (`os.supports_dir_fd` is empty), so the flags degrade to a plain
    # directory open that Windows then refuses outright. Declare that rather
    # than surface it as a bare PermissionError, which reads like a broken
    # ACL and would hide real permission defects behind it.
    if os.open not in os.supports_dir_fd:
        raise BrokerContractError(
            "no-follow directory traversal requires POSIX directory descriptors"
        )
    parts = PurePosixPath(_canonical_path(relative)).parts
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(root.resolve(), directory_flags)
    current_fd = root_fd
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            except FileNotFoundError as exc:
                raise BrokerContractError(f"receipt path component is missing: {part}") from exc
            except OSError as exc:
                raise BrokerContractError("receipt path uses a symlink; no-follow required") from exc
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd
        try:
            receipt_fd = os.open(parts[-1], file_flags, dir_fd=current_fd)
        except FileNotFoundError as exc:
            raise BrokerContractError("receipt path component is missing") from exc
        except OSError as exc:
            raise BrokerContractError("receipt path uses a symlink; no-follow required") from exc
        try:
            if not stat.S_ISREG(os.fstat(receipt_fd).st_mode):
                raise BrokerContractError("receipt is not a regular file")
            chunks: list[bytes] = []
            while chunk := os.read(receipt_fd, 1024 * 1024):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(receipt_fd)
    finally:
        if current_fd != root_fd:
            os.close(current_fd)
        os.close(root_fd)


def audit_invocation_receipts(
    *,
    run_root: Path | str,
    matrix: tuple[PrivilegedEndpointMatrixEntry, ...],
    provider: str,
    variant: str,
    broker: ProviderBroker | None = None,
    driver_result: SandboxDriverResult | None = None,
    receipt_ref: InvocationReceiptRef | None = None,
) -> EndpointAudit:
    if receipt_ref is not None or broker is None or driver_result is None:
        raise BrokerContractError(
            "live broker sandbox driver_result attestation is required; raw receipt_ref is refused"
        )
    selected = tuple(row for row in matrix if row.provider == provider and row.variant == variant)
    if not selected:
        raise BrokerContractError(f"no endpoint matrix row for {provider}/{variant}")
    gaps = tuple(row for row in selected if row.disposition == "capability_gap")
    equivalent = tuple(row for row in selected if row.disposition == "equivalent")
    if not isinstance(broker, ProviderBroker):
        raise BrokerContractError("provider broker is required for live receipt authentication")
    log = broker._verify_sandbox_driver_result(
        run_root=run_root,
        driver_result=driver_result,
        provider=provider,
        variant=variant,
    )
    previous: str | None = None
    for ordinal, receipt in enumerate(log.receipts, 1):
        unsigned = receipt.model_dump(exclude={"receipt_sha256"})
        if receipt.ordinal != ordinal:
            raise BrokerContractError("invocation receipts are not ordered")
        if receipt.previous_receipt_sha256 != previous:
            raise BrokerContractError("invocation receipt digest chain is broken")
        if _receipt_digest(unsigned) != receipt.receipt_sha256:
            raise BrokerContractError("invocation receipt seal digest differs")
        previous = receipt.receipt_sha256
    actual = tuple(receipt.driver_surface_id for receipt in log.receipts)
    declared = tuple(row.driver_surface_id for row in equivalent)
    undeclared = tuple(surface for surface in actual if surface not in declared)
    missing = tuple(surface for surface in declared if surface not in actual)
    if undeclared or missing:
        detail = ", ".join((*undeclared, *missing))
        raise BrokerContractError(f"receipt-bound endpoint inventory differs: {detail}")
    if gaps:
        return EndpointAudit(
            comparable=False,
            exclusions=tuple(
                f"{provider}/{variant}: capability_gap for {row.driver_surface_id} — {row.reason}"
                for row in gaps
            ),
            invoked_surfaces=actual,
        )
    return EndpointAudit(comparable=True, invoked_surfaces=actual)


_FORBIDDEN_IMPORTS = {"socket", "subprocess", "requests", "httpx", "urllib", "pathlib"}
_FORBIDDEN_CALLS = {"open", "connect", "create_connection", "Popen", "run", "system"}


def validate_driver_source(source: str, *, filename: str = "<driver>") -> None:
    """Reject direct imports/calls for every provider-visible capability class."""

    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        raise BrokerContractError(f"driver source is not parseable: {filename}") from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = {alias.name.split(".", 1)[0] for alias in node.names}
            if imported & _FORBIDDEN_IMPORTS:
                raise BrokerContractError(f"driver capability bypass import: {sorted(imported & _FORBIDDEN_IMPORTS)}")
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".", 1)[0] in _FORBIDDEN_IMPORTS:
            raise BrokerContractError(f"driver capability bypass import: {node.module}")
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute) else ""
            )
            if name in _FORBIDDEN_CALLS:
                raise BrokerContractError(f"driver capability bypass call: {name}")


def _require_broker(_name: str) -> None:
    if not _BROKER_ACTIVE.get():
        raise BrokerContractError("provider-visible capability must be invoked through the broker")


@contextmanager
def runtime_capability_isolation() -> Iterator[None]:
    """Defense-in-depth guard for cooperative in-process synthetic fixtures.

    Exclusive driver execution uses :meth:`ProviderBroker.run_driver`; Python
    monkeypatching cannot provide a security boundary and is never a fallback.
    """

    original_open = builtins.open
    original_path_open = Path.open
    original_connect = socket.socket.connect
    original_create_connection = socket.create_connection
    original_run = subprocess.run
    original_popen = subprocess.Popen
    original_system = os.system

    def guarded_open(*args, **kwargs):
        _require_broker("open")
        return original_open(*args, **kwargs)

    def guarded_path_open(self, *args, **kwargs):
        _require_broker("Path.open")
        return original_path_open(self, *args, **kwargs)

    def guarded_connect(self, *args, **kwargs):
        _require_broker("socket.connect")
        return original_connect(self, *args, **kwargs)

    def guarded_create_connection(*args, **kwargs):
        _require_broker("socket.create_connection")
        return original_create_connection(*args, **kwargs)

    def guarded_run(*args, **kwargs):
        _require_broker("subprocess.run")
        return original_run(*args, **kwargs)

    def guarded_popen(*args, **kwargs):
        _require_broker("subprocess.Popen")
        return original_popen(*args, **kwargs)

    def guarded_system(*args, **kwargs):
        _require_broker("os.system")
        return original_system(*args, **kwargs)

    builtins.open = guarded_open
    Path.open = guarded_path_open
    socket.socket.connect = guarded_connect
    socket.create_connection = guarded_create_connection
    subprocess.run = guarded_run
    subprocess.Popen = guarded_popen
    os.system = guarded_system
    try:
        yield
    finally:
        builtins.open = original_open
        Path.open = original_path_open
        socket.socket.connect = original_connect
        socket.create_connection = original_create_connection
        subprocess.run = original_run
        subprocess.Popen = original_popen
        os.system = original_system


__all__ = [
    "BrokerContractError",
    "BrokerInterface",
    "EndpointAudit",
    "InvocationReceiptRef",
    "ProviderBroker",
    "SandboxDriverResult",
    "SandboxExecutionAttestation",
    "audit_invocation_receipts",
    "runtime_capability_isolation",
    "validate_driver_source",
]
