from __future__ import annotations

import os
import socket
import struct
import threading
import time

import pytest
from test_hosted_activation_delivery import NOW, bundle, response

from exomem import hosted_activation_ack_server as server_module
from exomem.governance import authorization_custody as custody
from exomem.hosted_activation_ack_client import (
    ActivationAcknowledgementClient,
    ActivationAcknowledgementUnavailable,
)
from exomem.hosted_activation_ack_http import ActivationAckHttpError
from exomem.hosted_activation_ack_protocol import PROTOCOL, encode_message
from exomem.hosted_activation_ack_server import (
    CustodyAcknowledgementService,
    HostedActivationAckServerUnavailable,
    HostedActivationAckUnixServer,
)
from exomem.hosted_activation_publisher import CustodyPublisher

pytestmark = pytest.mark.skipif(
    os.name == "nt" or not hasattr(socket, "SO_PEERCRED"),
    reason="hosted acknowledgement sidecar is Linux-only",
)


def publication(epoch: int = 2) -> dict[str, object]:
    predecessor = {
        "activation_store_id": "store-1",
        "activation_epoch": epoch - 1,
        "activation_state_digest": str((epoch - 1) % 10) * 64,
    }
    return {
        "publication_event_id": f"event-{epoch}",
        "predecessor": predecessor,
        "successor": {
            "activation_store_id": "store-1",
            "activation_epoch": epoch,
            "activation_state_digest": str(epoch % 10) * 64,
        },
    }


def request(operation: str, target: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "protocol": PROTOCOL,
        "request_id": "a" * 64,
        "operation": operation,
        "budget_ms": 5000,
        "publication": target,
    }


@pytest.fixture
def publisher(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "private"
    source.mkdir(mode=0o700)
    destination.mkdir(mode=0o700)
    for name, raw in bundle().items():
        path = destination / name
        path.write_bytes(raw)
        path.chmod(0o600)
    return CustodyPublisher(source, destination, now=lambda: NOW)


class WorkerHttp:
    def __init__(self, current_files, ack_files=None) -> None:
        self.current_files = current_files
        self.ack_files = current_files if ack_files is None else ack_files
        self.calls: list[tuple[str, str, object, float]] = []
        self.ack_error: ActivationAckHttpError | None = None

    @staticmethod
    def _reply(files, request_id):
        result = response(files)
        result["request_id"] = request_id
        return result

    def current(self, request_id, *, deadline):
        self.calls.append(("current", request_id, None, deadline))
        return self._reply(self.current_files, request_id)

    def acknowledge(self, request_id, target, *, deadline):
        self.calls.append(("ack", request_id, target, deadline))
        if self.ack_error is not None:
            raise self.ack_error
        return self._reply(self.ack_files, request_id)


def service(publisher, http=None, *, cell_id="cell-1", replica_id="replica-1"):
    return CustodyAcknowledgementService(
        publisher,
        http or WorkerHttp(bundle()),
        expected_cell_id=cell_id,
        expected_replica_id=replica_id,
    )


def test_check_fetches_authority_installs_and_rereads_ready_custody(publisher):
    http = WorkerHttp(bundle(membership=2))
    result = service(publisher, http).handle(request("check"), time.monotonic() + 1)

    assert result == {
        "protocol": PROTOCOL,
        "request_id": "a" * 64,
        "status": "ready",
        "bundle_revision": publisher.current().revision,
        "activation": publication()["predecessor"],
        "code": "ACK_READY",
        "retry_after_ms": 0,
    }
    assert [call[0] for call in http.calls] == ["current"]
    assert publisher.current().membership.epoch == 2


@pytest.mark.parametrize(("cell_id", "replica_id"), [("other", "replica-1"), ("cell-1", "other")])
def test_check_refuses_custody_for_another_runtime(publisher, cell_id, replica_id):
    result = service(publisher, cell_id=cell_id, replica_id=replica_id).handle(
        request("check"), time.monotonic() + 1
    )
    assert (result["status"], result["code"]) == ("unavailable", "ACK_UNAVAILABLE")
    assert result["activation"] is None


@pytest.mark.parametrize("invalid", ["draining", "wrong-software-version"])
def test_check_requires_current_serving_release_custody(publisher, monkeypatch, invalid):
    if invalid == "wrong-software-version":
        monkeypatch.setattr(custody, "runtime_software_version", lambda: "other-version")
        files = bundle()
    else:
        files = bundle(state="DRAINING")
    for name, raw in files.items():
        (publisher.destination / name).write_bytes(raw)

    result = service(publisher, WorkerHttp(files)).handle(request("check"), time.monotonic() + 1)
    assert (result["status"], result["code"]) == ("unavailable", "ACK_UNAVAILABLE")


def test_ack_posts_exact_publication_and_returns_only_installed_successor(publisher):
    target = publication()
    http = WorkerHttp(bundle(), bundle(activation=2))
    result = service(publisher, http).handle(request("ack", target), time.monotonic() + 1)

    assert (result["status"], result["code"], result["activation"]) == (
        "acknowledged",
        "ACKNOWLEDGED",
        target["successor"],
    )
    assert http.calls[0][0:3] == ("ack", http.calls[0][1], target)
    assert publisher.current().control.activation_epoch == 2


def test_lost_ack_reply_recovers_with_one_fresh_current_request(publisher):
    target = publication()
    http = WorkerHttp(bundle(activation=2))
    http.ack_error = ActivationAckHttpError("ACK_UNAVAILABLE")
    result = service(publisher, http).handle(request("ack", target), time.monotonic() + 1)

    assert (result["status"], result["code"]) == ("acknowledged", "ACKNOWLEDGED")
    assert [call[0] for call in http.calls] == ["ack", "current"]
    assert http.calls[0][1] != http.calls[1][1]


def test_lost_ack_reply_stays_pending_when_current_is_still_predecessor(publisher):
    http = WorkerHttp(bundle())
    http.ack_error = ActivationAckHttpError("ACK_UNAVAILABLE", 75)
    result = service(publisher, http).handle(request("ack", publication()), time.monotonic() + 1)
    assert (result["status"], result["code"], result["retry_after_ms"]) == (
        "pending",
        "ACK_PENDING",
        75,
    )


def test_expired_ack_budget_never_starts_current_recovery(publisher):
    class ExpiringHttp(WorkerHttp):
        def acknowledge(self, request_id, target, *, deadline):
            self.calls.append(("ack", request_id, target, deadline))
            time.sleep(max(0, deadline - time.monotonic()) + 0.01)
            raise ActivationAckHttpError("ACK_UNAVAILABLE")

    http = ExpiringHttp(bundle(activation=2))
    result = service(publisher, http).handle(request("ack", publication()), time.monotonic() + 0.03)
    assert (result["status"], result["code"]) == ("pending", "ACK_PENDING")
    assert [call[0] for call in http.calls] == ["ack"]


def test_publisher_authority_fetch_uses_fresh_request_ids_and_caller_deadline(publisher):
    http = WorkerHttp(bundle())
    service(publisher, http)
    deadline = time.monotonic() + 1
    publisher.fetch_current(deadline)
    publisher.fetch_current(deadline)
    assert http.calls[0][1] != http.calls[1][1]
    assert [call[3] for call in http.calls] == [deadline, deadline]


def test_waiting_for_publisher_lock_is_bounded_by_request_deadline(publisher):
    acquired = threading.Event()
    release = threading.Event()

    def hold():
        with publisher._lock:
            acquired.set()
            release.wait(2)

    holder = threading.Thread(target=hold)
    holder.start()
    assert acquired.wait(1)
    started = time.monotonic()
    result = service(publisher).handle(request("check"), started + 0.05)
    release.set()
    holder.join(1)
    assert (result["status"], result["code"]) == ("unavailable", "ACK_DEADLINE_EXCEEDED")
    assert time.monotonic() - started < 0.5


def start_server(server, **serve_options):
    stopped = threading.Event()
    errors: list[BaseException] = []

    def run():
        try:
            server.serve(stopped, **serve_options)
        except BaseException as error:  # noqa: BLE001 - preserve thread startup failure
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    assert server.ready.wait(2), errors
    return stopped, thread, errors


def stop_server(server, stopped, thread, errors):
    stopped.set()
    server.stop()
    thread.join(2)
    assert not thread.is_alive()
    assert errors == []


def test_real_unix_socket_serves_check_and_ack_with_mode_0600(publisher, tmp_path):
    path = tmp_path / "socket" / "ack.sock"
    path.parent.mkdir(mode=0o700)
    http = WorkerHttp(bundle(), bundle(activation=2))
    server = HostedActivationAckUnixServer(service(publisher, http), socket_path=path)
    stopped, thread, errors = start_server(server)
    try:
        client = ActivationAcknowledgementClient(path)
        assert client.check()["code"] == "ACK_READY"
        assert client.acknowledge(publication())["code"] == "ACKNOWLEDGED"
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        stop_server(server, stopped, thread, errors)
    assert not path.exists()


class SlowService:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def handle(self, incoming, deadline):
        self.calls += 1
        self.entered.set()
        self.release.wait(2)
        return {
            "protocol": PROTOCOL,
            "request_id": incoming["request_id"],
            "status": "ready",
            "bundle_revision": "b" * 64,
            "activation": publication()["predecessor"],
            "code": "ACK_READY",
            "retry_after_ms": 0,
        }


def test_slow_or_disconnected_exchange_keeps_the_only_active_slot(publisher, tmp_path):
    path = tmp_path / "socket" / "ack.sock"
    path.parent.mkdir(mode=0o700)
    slow = SlowService()
    server = HostedActivationAckUnixServer(
        slow, socket_path=path, publisher_destination=publisher.destination
    )
    stopped, thread, errors = start_server(server)
    first = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        first.connect(str(path))
        raw = encode_message(request("check"), "udsCheckRequest")
        first.sendall(struct.pack("!I", len(raw)) + raw)
        assert slow.entered.wait(1)
        first.close()
        started = time.monotonic()
        with pytest.raises(ActivationAcknowledgementUnavailable):
            ActivationAcknowledgementClient(path).check(timeout_seconds=0.2)
        assert time.monotonic() - started < 0.5
        assert slow.calls == 1
    finally:
        first.close()
        slow.release.set()
        stop_server(server, stopped, thread, errors)


def test_oversized_or_slow_initial_frame_does_not_occupy_a_service_slot(publisher, tmp_path):
    path = tmp_path / "socket" / "ack.sock"
    path.parent.mkdir(mode=0o700)
    server = HostedActivationAckUnixServer(service(publisher), socket_path=path)
    stopped, thread, errors = start_server(server)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as oversized:
            oversized.connect(str(path))
            oversized.sendall(struct.pack("!I", 8193))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as partial:
            partial.connect(str(path))
            partial.sendall(b"\x00")
            time.sleep(0.6)
        assert ActivationAcknowledgementClient(path).check()["code"] == "ACK_READY"
    finally:
        stop_server(server, stopped, thread, errors)


def test_server_replaces_only_a_safe_refused_stale_socket(publisher, tmp_path):
    directory = tmp_path / "socket"
    directory.mkdir(mode=0o700)
    path = directory / "ack.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    path.chmod(0o600)
    stale.close()

    server = HostedActivationAckUnixServer(service(publisher), socket_path=path)
    stopped, thread, errors = start_server(server)
    stop_server(server, stopped, thread, errors)


@pytest.mark.parametrize("unsafe", ["world-writable-directory", "foreign-mode-socket"])
def test_server_refuses_unsafe_socket_ownership_surfaces(publisher, tmp_path, unsafe):
    directory = tmp_path / "socket"
    directory.mkdir(mode=0o700)
    path = directory / "ack.sock"
    if unsafe == "world-writable-directory":
        directory.chmod(0o707)
    else:
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(path))
        path.chmod(0o660)
        stale.close()

    server = HostedActivationAckUnixServer(service(publisher), socket_path=path)
    with pytest.raises(HostedActivationAckServerUnavailable):
        server.serve(threading.Event())


def test_live_socket_is_never_unlinked(publisher, tmp_path):
    directory = tmp_path / "socket"
    directory.mkdir(mode=0o700)
    path = directory / "ack.sock"
    live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    live.bind(str(path))
    path.chmod(0o600)
    live.listen(1)
    inode = path.stat().st_ino
    try:
        server = HostedActivationAckUnixServer(service(publisher), socket_path=path)
        with pytest.raises(HostedActivationAckServerUnavailable):
            server.serve(threading.Event())
        assert path.stat().st_ino == inode
    finally:
        live.close()
        path.unlink()


def test_stop_does_not_unlink_a_replacement_socket(publisher, tmp_path):
    directory = tmp_path / "socket"
    directory.mkdir(mode=0o700)
    path = directory / "ack.sock"
    server = HostedActivationAckUnixServer(service(publisher), socket_path=path)
    stopped, thread, errors = start_server(server)
    path.unlink()
    replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    replacement.bind(str(path))
    path.chmod(0o600)
    inode = path.stat().st_ino
    try:
        stop_server(server, stopped, thread, errors)
        assert path.stat().st_ino == inode
    finally:
        replacement.close()
        path.unlink()


def test_process_lock_refuses_a_second_publisher_before_socket_rebind(publisher, tmp_path):
    first_path = tmp_path / "one" / "ack.sock"
    second_path = tmp_path / "two" / "ack.sock"
    first_path.parent.mkdir(mode=0o700)
    second_path.parent.mkdir(mode=0o700)
    first = HostedActivationAckUnixServer(service(publisher), socket_path=first_path)
    stopped, thread, errors = start_server(first)
    try:
        lock = publisher.destination / ".activation-ack-publisher.lock"
        assert lock.stat().st_mode & 0o777 == 0o600
        second = HostedActivationAckUnixServer(service(publisher), socket_path=second_path)
        with pytest.raises(HostedActivationAckServerUnavailable):
            second.serve(threading.Event())
        assert not second_path.exists()
    finally:
        stop_server(first, stopped, thread, errors)


def test_unsafe_process_lock_is_refused_before_socket_creation(publisher, tmp_path):
    socket_path = tmp_path / "socket" / "ack.sock"
    socket_path.parent.mkdir(mode=0o700)
    lock = publisher.destination / ".activation-ack-publisher.lock"
    lock.write_text("foreign")
    lock.chmod(0o644)
    server = HostedActivationAckUnixServer(service(publisher), socket_path=socket_path)
    with pytest.raises(HostedActivationAckServerUnavailable):
        server.serve(threading.Event())
    assert not socket_path.exists()


def test_server_construction_refuses_when_posix_locking_is_unavailable(
    publisher, tmp_path, monkeypatch
):
    monkeypatch.setattr(server_module, "fcntl", None)
    with pytest.raises(HostedActivationAckServerUnavailable):
        HostedActivationAckUnixServer(
            service(publisher), socket_path=tmp_path / "socket" / "ack.sock"
        )


def test_periodic_refresh_never_overlaps_an_active_exchange(publisher, tmp_path):
    path = tmp_path / "socket" / "ack.sock"
    path.parent.mkdir(mode=0o700)
    slow = SlowService()
    refresh_entered = threading.Event()
    server = HostedActivationAckUnixServer(
        slow, socket_path=path, publisher_destination=publisher.destination
    )
    stopped, thread, errors = start_server(
        server,
        periodic_refresh=refresh_entered.set,
        refresh_interval_seconds=0.2,
    )
    client_errors: list[BaseException] = []

    def check():
        try:
            ActivationAcknowledgementClient(path).check(timeout_seconds=1)
        except BaseException as error:  # noqa: BLE001 - preserve background failure
            client_errors.append(error)

    client = threading.Thread(target=check)
    client.start()
    try:
        assert slow.entered.wait(1)
        assert not refresh_entered.wait(0.3)
        slow.release.set()
        client.join(1)
        assert not client.is_alive()
        assert client_errors == []
        assert refresh_entered.wait(1)
    finally:
        slow.release.set()
        stop_server(server, stopped, thread, errors)


def test_stop_keeps_process_lock_until_blocked_periodic_refresh_finishes(publisher, tmp_path):
    first_path = tmp_path / "one" / "ack.sock"
    second_path = tmp_path / "two" / "ack.sock"
    first_path.parent.mkdir(mode=0o700)
    second_path.parent.mkdir(mode=0o700)
    refresh_entered = threading.Event()
    release_refresh = threading.Event()

    def blocked_refresh():
        refresh_entered.set()
        release_refresh.wait(2)

    first = HostedActivationAckUnixServer(service(publisher), socket_path=first_path)
    stopped, thread, errors = start_server(
        first,
        periodic_refresh=blocked_refresh,
        refresh_interval_seconds=0.01,
    )
    assert refresh_entered.wait(1)
    started = time.monotonic()
    with pytest.raises(ActivationAcknowledgementUnavailable):
        ActivationAcknowledgementClient(first_path).check(timeout_seconds=0.2)
    assert time.monotonic() - started < 0.5
    stopped.set()
    stopper = threading.Thread(target=first.stop)
    stopper.start()
    deadline = time.monotonic() + 1
    while first.ready.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not first.ready.is_set()

    second = HostedActivationAckUnixServer(service(publisher), socket_path=second_path)
    with pytest.raises(HostedActivationAckServerUnavailable):
        second.serve(threading.Event())
    assert not second_path.exists()

    release_refresh.set()
    stopper.join(1)
    thread.join(1)
    assert not stopper.is_alive()
    assert not thread.is_alive()
    assert errors == []
