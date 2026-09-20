from __future__ import annotations

import json
import socket
import struct
import threading
import time

import pytest

from exomem.hosted_activation_ack_client import (
    ActivationAcknowledgementClient,
    ActivationAcknowledgementUnavailable,
    is_hosted_activation_ack_enabled,
)
from exomem.hosted_activation_ack_protocol import PROTOCOL, decode_message, encode_message


def test_capability_requires_the_exact_complete_configuration():
    assert not is_hosted_activation_ack_enabled({})
    valid = {
        "EXOMEM_HOSTED_ACTIVATION_ACK_PROTOCOL": PROTOCOL,
        "EXOMEM_HOSTED_ACTIVATION_ACK_SOCKET": "/run/exomem/activation-ack/ack.sock",
    }
    assert is_hosted_activation_ack_enabled(valid)
    for invalid in (
        {"EXOMEM_HOSTED_ACTIVATION_ACK_PROTOCOL": PROTOCOL},
        {"EXOMEM_HOSTED_ACTIVATION_ACK_SOCKET": valid["EXOMEM_HOSTED_ACTIVATION_ACK_SOCKET"]},
        {**valid, "EXOMEM_HOSTED_ACTIVATION_ACK_PROTOCOL": "unknown"},
        {**valid, "EXOMEM_HOSTED_ACTIVATION_ACK_SOCKET": "/tmp/elsewhere.sock"},
        {**valid, "EXOMEM_HOSTED_ACTIVATION_ACK_PROTOCOL": ""},
    ):
        with pytest.raises(ActivationAcknowledgementUnavailable):
            is_hosted_activation_ack_enabled(invalid)


@pytest.mark.parametrize("cell", ["cell-1", ""])
def test_hosted_cell_cannot_silently_use_local_custody_when_capability_is_missing(cell):
    with pytest.raises(ActivationAcknowledgementUnavailable):
        is_hosted_activation_ack_enabled({"EXOMEM_HOSTED_CELL_ID": cell})


@pytest.fixture
def helper(tmp_path):
    if not hasattr(socket, "SO_PEERCRED"):
        pytest.skip("Hosted uses Linux Unix sockets")
    servers = []

    def start(responder):
        path = tmp_path / f"ack-{len(servers)}.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        path.chmod(0o600)
        server.listen(1)
        errors = []

        def serve():
            try:
                with server.accept()[0] as connection:
                    connection.settimeout(2)
                    size = struct.unpack("!I", connection.recv(4, socket.MSG_WAITALL))[0]
                    request = json.loads(connection.recv(size, socket.MSG_WAITALL))
                    response = responder(request)
                    connection.sendall(struct.pack("!I", len(response)) + response)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as error:  # noqa: BLE001 - report every helper-thread failure
                errors.append(error)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        servers.append((server, thread, errors))
        return ActivationAcknowledgementClient(path)

    yield start
    for server, thread, errors in servers:
        thread.join(3)
        server.close()
        assert not thread.is_alive()
        assert errors == []


def ready(request):
    return encode_message(
        {
            "protocol": PROTOCOL,
            "request_id": request["request_id"],
            "status": "ready",
            "bundle_revision": "a" * 64,
            "activation": {
                "activation_store_id": "store-1",
                "activation_epoch": 1,
                "activation_state_digest": "b" * 64,
            },
            "code": "ACK_READY",
            "retry_after_ms": 0,
        },
        "udsResponse",
    )


@pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"), reason="Hosted uses Linux Unix sockets")
def test_real_socket_check_correlates_response_and_verifies_peer(helper):
    client = helper(ready)
    response = client.check()
    assert response["status"] == "ready"
    assert response["activation"]["activation_epoch"] == 1


@pytest.mark.parametrize("change", ["correlation", "status-code", "missing-activation"])
def test_malformed_success_is_not_readiness(helper, change):
    def malformed(request):
        result = decode_message(ready(request), "udsResponse")
        if change == "correlation":
            result["request_id"] = "0" * 64
        elif change == "status-code":
            result["code"] = "ACKNOWLEDGED"
        else:
            result["activation"] = None
        return encode_message(result, "udsResponse")

    with pytest.raises(ActivationAcknowledgementUnavailable):
        helper(malformed).check()


def test_missing_socket_and_nonpositive_deadline_refuse(tmp_path):
    with pytest.raises(ActivationAcknowledgementUnavailable):
        ActivationAcknowledgementClient(tmp_path / "absent").check()
    with pytest.raises(ActivationAcknowledgementUnavailable):
        ActivationAcknowledgementClient(tmp_path / "absent").check(timeout_seconds=0)


def test_world_writable_socket_is_not_trusted(tmp_path):
    if not hasattr(socket, "SO_PEERCRED"):
        pytest.skip("Hosted uses Linux Unix sockets")
    path = tmp_path / "unsafe.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        path.chmod(0o666)
        with pytest.raises(ActivationAcknowledgementUnavailable):
            ActivationAcknowledgementClient(path).check()


def test_oversized_response_is_refused_before_body_decode(helper):
    with pytest.raises(ActivationAcknowledgementUnavailable):
        helper(lambda _: b"x" * 8193).check()


def test_deadline_bounds_a_silent_helper(helper):
    def slow(request):
        time.sleep(0.2)
        return ready(request)

    started = time.monotonic()
    with pytest.raises(ActivationAcknowledgementUnavailable):
        helper(slow).check(timeout_seconds=0.05)
    assert time.monotonic() - started < 1


@pytest.mark.parametrize("wrong_target", [False, True])
def test_acknowledgement_requires_the_exact_requested_successor(helper, wrong_target):
    predecessor = {
        "activation_store_id": "store-1",
        "activation_epoch": 1,
        "activation_state_digest": "b" * 64,
    }
    successor = {**predecessor, "activation_epoch": 2, "activation_state_digest": "c" * 64}
    publication = {
        "publication_event_id": "event-1",
        "predecessor": predecessor,
        "successor": successor,
    }

    def acknowledge(request):
        assert request["publication"] == publication
        response = decode_message(ready(request), "udsResponse")
        response.update(
            status="acknowledged",
            code="ACKNOWLEDGED",
            activation=predecessor if wrong_target else successor,
        )
        return encode_message(response, "udsResponse")

    client = helper(acknowledge)
    if wrong_target:
        with pytest.raises(ActivationAcknowledgementUnavailable):
            client.acknowledge(publication)
    else:
        assert client.acknowledge(publication)["activation"] == successor
