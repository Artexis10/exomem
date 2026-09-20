"""Bounded Unix-socket service for hosted activation acknowledgement."""

from __future__ import annotations

import errno
import math
import os
import secrets
import socket
import stat
import struct
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised by Windows collection
    fcntl = None  # type: ignore[assignment]

from .hosted_activation_ack_client import SOCKET_PATH
from .hosted_activation_ack_http import ActivationAckHttpError
from .hosted_activation_ack_protocol import PROTOCOL, ProtocolError, decode_message, encode_message
from .hosted_activation_delivery import (
    CustodyDeliveryUnavailable,
    CustodyKeyringUnavailable,
    VerifiedDeliveryBundle,
    require_serving_delivery,
)
from .hosted_activation_publisher import CustodyPublisher

_MAX_FRAME_BYTES = 8 * 1024
_MAX_INITIAL_SECONDS = 0.5
_MAX_EXCHANGE_SECONDS = 5.0
_LOCK_NAME = ".activation-ack-publisher.lock"


class HostedActivationAckServerUnavailable(RuntimeError):
    """Content-free refusal when the sidecar cannot safely serve."""

    code = "ACK_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("hosted activation acknowledgement server is unavailable")


def _activation(bundle: VerifiedDeliveryBundle) -> dict[str, object]:
    control = bundle.control
    if (
        control.activation_store_id is None
        or control.activation_epoch is None
        or control.activation_state_digest is None
    ):
        raise CustodyDeliveryUnavailable
    return {
        "activation_store_id": control.activation_store_id,
        "activation_epoch": control.activation_epoch,
        "activation_state_digest": control.activation_state_digest,
    }


class CustodyAcknowledgementService:
    """Reconcile one worker response through the serialized custody publisher."""

    def __init__(
        self,
        publisher: CustodyPublisher,
        http_client: object,
        *,
        expected_cell_id: str,
        expected_replica_id: str,
    ) -> None:
        if (
            not isinstance(publisher, CustodyPublisher)
            or not isinstance(expected_cell_id, str)
            or not expected_cell_id
            or not isinstance(expected_replica_id, str)
            or not expected_replica_id
            or not callable(getattr(http_client, "current", None))
            or not callable(getattr(http_client, "acknowledge", None))
        ):
            raise HostedActivationAckServerUnavailable
        self.publisher = publisher
        self._http = http_client
        self._expected_cell_id = expected_cell_id
        self._expected_replica_id = expected_replica_id
        self.publisher.fetch_current = self._fetch_current

    def _fetch_current(self, deadline: float) -> Mapping[str, object]:
        return self._http.current(secrets.token_hex(32), deadline=deadline)

    @staticmethod
    def _response(
        request_id: str,
        *,
        status: str,
        code: str,
        bundle: VerifiedDeliveryBundle | None = None,
        retry_after_ms: int = 0,
    ) -> dict[str, object]:
        result = {
            "protocol": PROTOCOL,
            "request_id": request_id,
            "status": status,
            "bundle_revision": None if bundle is None else bundle.revision,
            "activation": None if bundle is None else _activation(bundle),
            "code": code,
            "retry_after_ms": max(0, min(5000, retry_after_ms)),
        }
        encode_message(result, "udsResponse")
        return result

    def _require_runtime(self, bundle: VerifiedDeliveryBundle) -> None:
        require_serving_delivery(bundle)
        replica = bundle.membership.replicas[0]
        if (
            bundle.control.cell_id != self._expected_cell_id
            or replica.cell_id != self._expected_cell_id
            or replica.replica_id != self._expected_replica_id
        ):
            raise CustodyDeliveryUnavailable

    def _install(self, response: Mapping[str, object]) -> VerifiedDeliveryBundle:
        self.publisher.install_response(response)
        installed = self.publisher.current()
        self._require_runtime(installed)
        return installed

    def _error(
        self,
        request_id: str,
        error: ActivationAckHttpError | CustodyDeliveryUnavailable,
        *,
        uncertain: bool = False,
    ) -> dict[str, object]:
        code = getattr(error, "code", "ACK_UNAVAILABLE")
        retry_after_ms = getattr(error, "retry_after_ms", 0)
        if uncertain:
            return self._response(
                request_id,
                status="pending",
                code="ACK_PENDING",
                retry_after_ms=retry_after_ms,
            )
        if code in {"ACK_PENDING", "KEYRING_NOT_AVAILABLE"}:
            return self._response(
                request_id,
                status="pending",
                code=code,
                retry_after_ms=retry_after_ms,
            )
        if code == "ACTIVATION_CONFLICT":
            return self._response(
                request_id,
                status="conflict",
                code=code,
                retry_after_ms=retry_after_ms,
            )
        if code not in {
            "MALFORMED_REQUEST",
            "AUTHENTICATION_FAILED",
            "ACK_CAPACITY_EXCEEDED",
            "ACK_UNAVAILABLE",
            "ACK_DEADLINE_EXCEEDED",
        }:
            code = "ACK_UNAVAILABLE"
        return self._response(
            request_id,
            status="unavailable",
            code=code,
            retry_after_ms=retry_after_ms,
        )

    def handle(self, request: Mapping[str, object], deadline: float) -> dict[str, object]:
        """Handle one already framed request within its monotonic deadline."""
        try:
            operation = request["operation"]
            kind = "udsCheckRequest" if operation == "check" else "udsAckRequest"
            encode_message(request, kind)
            request_id = request["request_id"]
        except (KeyError, ProtocolError, TypeError):
            raise HostedActivationAckServerUnavailable from None
        now = time.monotonic()
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)
            or deadline <= now
        ):
            return self._response(request_id, status="unavailable", code="ACK_DEADLINE_EXCEEDED")
        acquired = self.publisher._lock.acquire(timeout=max(0.0, deadline - now))
        if not acquired:
            return self._response(request_id, status="unavailable", code="ACK_DEADLINE_EXCEEDED")
        try:
            if time.monotonic() >= deadline:
                return self._response(
                    request_id, status="unavailable", code="ACK_DEADLINE_EXCEEDED"
                )
            if operation == "check":
                try:
                    installed = self._install(self._fetch_current(deadline))
                    return self._response(
                        request_id,
                        status="ready",
                        code="ACK_READY",
                        bundle=installed,
                    )
                except (ActivationAckHttpError, CustodyDeliveryUnavailable) as error:
                    return self._error(request_id, error)

            publication = request["publication"]
            recovery_retry_after_ms = 0
            try:
                reply = self._http.acknowledge(
                    secrets.token_hex(32), publication, deadline=deadline
                )
            except ActivationAckHttpError as error:
                if error.code in {
                    "ACTIVATION_CONFLICT",
                    "AUTHENTICATION_FAILED",
                    "MALFORMED_REQUEST",
                }:
                    return self._error(request_id, error)
                recovery_retry_after_ms = error.retry_after_ms
                try:
                    if time.monotonic() >= deadline:
                        raise ActivationAckHttpError("ACK_DEADLINE_EXCEEDED")
                    reply = self._fetch_current(deadline)
                except ActivationAckHttpError:
                    return self._error(request_id, error, uncertain=True)
            try:
                installed = self._install(reply)
            except CustodyKeyringUnavailable as error:
                return self._error(request_id, error)
            except CustodyDeliveryUnavailable as error:
                return self._error(request_id, error, uncertain=True)
            actual = _activation(installed)
            if actual == publication["successor"]:
                return self._response(
                    request_id,
                    status="acknowledged",
                    code="ACKNOWLEDGED",
                    bundle=installed,
                )
            if actual == publication["predecessor"]:
                return self._response(
                    request_id,
                    status="pending",
                    code="ACK_PENDING",
                    bundle=installed,
                    retry_after_ms=recovery_retry_after_ms,
                )
            return self._response(
                request_id,
                status="conflict",
                code="ACTIVATION_CONFLICT",
                bundle=installed,
            )
        finally:
            self.publisher._lock.release()


def _read_exact(connection: socket.socket, count: int, deadline: float) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            raise HostedActivationAckServerUnavailable
        connection.settimeout(timeout)
        chunk = connection.recv(remaining)
        if not chunk:
            raise HostedActivationAckServerUnavailable
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class HostedActivationAckUnixServer:
    """One active same-UID Unix-socket exchange and one accept loop."""

    def __init__(
        self,
        service: object,
        *,
        socket_path: Path = SOCKET_PATH,
        publisher_destination: Path | None = None,
        expected_group_id: int | None = None,
    ) -> None:
        destination = publisher_destination
        if destination is None:
            destination = getattr(getattr(service, "publisher", None), "destination", None)
        if (
            not callable(getattr(service, "handle", None))
            or destination is None
            or fcntl is None
            or not hasattr(socket, "SO_PEERCRED")
            or isinstance(expected_group_id, bool)
            or (expected_group_id is not None and not isinstance(expected_group_id, int))
            or (expected_group_id is not None and expected_group_id < 0)
        ):
            raise HostedActivationAckServerUnavailable
        self._service = service
        self._path = Path(socket_path)
        self._publisher_destination = Path(destination)
        self._expected_gid = os.getegid() if expected_group_id is None else expected_group_id
        self.ready = threading.Event()
        self._stop = threading.Event()
        self._serve_finished = threading.Event()
        self._state_lock = threading.Lock()
        self._listener: socket.socket | None = None
        self._handler: threading.Thread | None = None
        self._serve_thread: threading.Thread | None = None
        self._lock_fd: int | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._cleaned = False

    def __enter__(self) -> HostedActivationAckUnixServer:
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    def _directory(self) -> None:
        try:
            info = self._path.parent.lstat()
        except OSError:
            raise HostedActivationAckServerUnavailable from None
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid not in {0, os.geteuid()}
            or info.st_gid != self._expected_gid
            or info.st_mode & stat.S_IWOTH
        ):
            raise HostedActivationAckServerUnavailable

    def _acquire_process_lock(self) -> None:
        if fcntl is None:
            raise HostedActivationAckServerUnavailable
        path = self._publisher_destination / _LOCK_NAME
        descriptor: int | None = None
        try:
            directory = self._publisher_destination.lstat()
            if (
                not stat.S_ISDIR(directory.st_mode)
                or directory.st_uid != os.geteuid()
                or stat.S_IMODE(directory.st_mode) != 0o700
            ):
                raise HostedActivationAckServerUnavailable
            descriptor = os.open(
                path,
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
            )
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise HostedActivationAckServerUnavailable
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_fd = descriptor
        except (OSError, HostedActivationAckServerUnavailable):
            if descriptor is not None:
                os.close(descriptor)
            raise HostedActivationAckServerUnavailable from None

    def _prepare_socket(self) -> socket.socket:
        self._directory()
        try:
            info = self._path.lstat()
        except FileNotFoundError:
            info = None
        except OSError:
            raise HostedActivationAckServerUnavailable from None
        if info is not None:
            if (
                not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise HostedActivationAckServerUnavailable
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.1)
                probe.connect(str(self._path))
            except OSError as error:
                if error.errno != errno.ECONNREFUSED:
                    raise HostedActivationAckServerUnavailable from None
            else:
                raise HostedActivationAckServerUnavailable
            finally:
                probe.close()
            try:
                current = self._path.lstat()
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    raise HostedActivationAckServerUnavailable
                self._path.unlink()
            except OSError:
                raise HostedActivationAckServerUnavailable from None
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self._path))
            self._path.chmod(0o600)
            bound = self._path.lstat()
            if (
                not stat.S_ISSOCK(bound.st_mode)
                or bound.st_uid != os.geteuid()
                or stat.S_IMODE(bound.st_mode) != 0o600
            ):
                raise HostedActivationAckServerUnavailable
            self._socket_identity = (bound.st_dev, bound.st_ino)
            listener.listen(1)
            listener.settimeout(0.1)
            return listener
        except (OSError, HostedActivationAckServerUnavailable):
            listener.close()
            self._unlink_own_socket()
            raise HostedActivationAckServerUnavailable from None

    def _unlink_own_socket(self) -> None:
        identity = self._socket_identity
        if identity is None:
            return
        try:
            current = self._path.lstat()
            if (current.st_dev, current.st_ino) == identity:
                self._path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        self._socket_identity = None

    def _close_listener(self) -> None:
        with self._state_lock:
            listener = self._listener
            self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass

    def _cleanup_if_idle(self) -> None:
        with self._state_lock:
            handler = self._handler
            if (
                self._cleaned
                or not self._serve_finished.is_set()
                or (handler is not None and handler.is_alive())
            ):
                return
            self._cleaned = True
            descriptor = self._lock_fd
            self._lock_fd = None
        self._unlink_own_socket()
        if descriptor is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _finish_worker(self) -> None:
        with self._state_lock:
            if self._handler is threading.current_thread():
                self._handler = None
        if self._serve_finished.is_set():
            self._cleanup_if_idle()

    def _handle(self, connection: socket.socket, accepted_at: float) -> None:
        try:
            _, peer_uid, _ = struct.unpack(
                "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            if peer_uid != os.geteuid():
                raise HostedActivationAckServerUnavailable
            initial_deadline = min(
                accepted_at + _MAX_INITIAL_SECONDS,
                accepted_at + _MAX_EXCHANGE_SECONDS,
            )
            size = struct.unpack("!I", _read_exact(connection, 4, initial_deadline))[0]
            if not 1 <= size <= _MAX_FRAME_BYTES:
                raise HostedActivationAckServerUnavailable
            raw = _read_exact(connection, size, initial_deadline)
            try:
                incoming = decode_message(raw, "udsCheckRequest")
            except ProtocolError:
                incoming = decode_message(raw, "udsAckRequest")
            deadline = min(
                accepted_at + _MAX_EXCHANGE_SECONDS,
                accepted_at + incoming["budget_ms"] / 1000,
            )
            try:
                result = self._service.handle(incoming, deadline)
                encoded = encode_message(result, "udsResponse")
            except Exception:  # noqa: BLE001 - the wire exposes only stable content-free codes
                encoded = encode_message(
                    {
                        "protocol": PROTOCOL,
                        "request_id": incoming["request_id"],
                        "status": "unavailable",
                        "bundle_revision": None,
                        "activation": None,
                        "code": "ACK_UNAVAILABLE",
                        "retry_after_ms": 0,
                    },
                    "udsResponse",
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            connection.settimeout(remaining)
            connection.sendall(struct.pack("!I", len(encoded)) + encoded)
        except (OSError, ProtocolError, HostedActivationAckServerUnavailable, struct.error):
            pass
        finally:
            connection.close()
            self._finish_worker()

    def _refresh(self, periodic_refresh: Callable[[], None]) -> None:
        try:
            periodic_refresh()
        except Exception:  # noqa: BLE001 - periodic refresh retries without ending service
            pass
        finally:
            self._finish_worker()

    def serve(
        self,
        stop_event: threading.Event,
        *,
        periodic_refresh: Callable[[], None] | None = None,
        refresh_interval_seconds: float = 15.0,
    ) -> None:
        """Serve until either the caller's event or this server's stop event is set."""
        if (
            not isinstance(stop_event, threading.Event)
            or (periodic_refresh is not None and not callable(periodic_refresh))
            or isinstance(refresh_interval_seconds, bool)
            or not isinstance(refresh_interval_seconds, (int, float))
            or not math.isfinite(refresh_interval_seconds)
            or refresh_interval_seconds <= 0
        ):
            raise HostedActivationAckServerUnavailable
        with self._state_lock:
            if self._serve_thread is not None:
                raise HostedActivationAckServerUnavailable
            self._serve_thread = threading.current_thread()
        try:
            self._acquire_process_lock()
            publisher = getattr(self._service, "publisher", None)
            if publisher is not None:
                publisher.recover_pending()
            listener = self._prepare_socket()
            with self._state_lock:
                self._listener = listener
            self.ready.set()
            next_refresh = time.monotonic() + refresh_interval_seconds
            while not self._stop.is_set() and not stop_event.is_set():
                now = time.monotonic()
                if periodic_refresh is not None and now >= next_refresh:
                    with self._state_lock:
                        handler = self._handler
                        if handler is None or not handler.is_alive():
                            handler = threading.Thread(
                                target=self._refresh,
                                args=(periodic_refresh,),
                                daemon=True,
                            )
                            self._handler = handler
                            next_refresh = now + refresh_interval_seconds
                            handler.start()
                timeout = 0.1
                if periodic_refresh is not None and next_refresh > now:
                    timeout = min(timeout, max(0.001, next_refresh - now))
                listener.settimeout(timeout)
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self._stop.is_set() or stop_event.is_set():
                        break
                    raise HostedActivationAckServerUnavailable from None
                accepted_at = time.monotonic()
                with self._state_lock:
                    handler = self._handler
                    if handler is not None and handler.is_alive():
                        connection.close()
                        continue
                    handler = threading.Thread(
                        target=self._handle,
                        args=(connection, accepted_at),
                        daemon=True,
                    )
                    self._handler = handler
                    handler.start()
        except (OSError, CustodyDeliveryUnavailable, HostedActivationAckServerUnavailable):
            raise HostedActivationAckServerUnavailable from None
        finally:
            self.ready.clear()
            self._close_listener()
            self._serve_finished.set()
            self._cleanup_if_idle()

    def stop(self) -> None:
        """Stop this listener and wait boundedly for only its active exchange."""
        self._stop.set()
        self._close_listener()
        with self._state_lock:
            handler = self._handler
            serve_thread = self._serve_thread
        current = threading.current_thread()
        if handler is not None and handler is not current:
            handler.join(_MAX_EXCHANGE_SECONDS + _MAX_INITIAL_SECONDS)
        if serve_thread is not None and serve_thread is not current:
            serve_thread.join(1)
        self._cleanup_if_idle()
