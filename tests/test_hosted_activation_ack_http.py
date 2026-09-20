from __future__ import annotations

import base64
import datetime as dt
import json
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from exomem.hosted_activation_ack_http import (
    ActivationAckHttpError,
    HostedActivationAckHttpClient,
)
from exomem.hosted_activation_ack_protocol import PROTOCOL, encode_message
from exomem.hosted_security import CredentialBundle

SECRET = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
REQUEST_ID = "a" * 64


def _publication() -> dict[str, object]:
    predecessor = {
        "activation_store_id": "activation-store-1",
        "activation_epoch": 1,
        "activation_state_digest": "b" * 64,
    }
    return {
        "publication_event_id": "publication-1",
        "predecessor": predecessor,
        "successor": {
            **predecessor,
            "activation_epoch": 2,
            "activation_state_digest": "c" * 64,
        },
    }


def _bundle(request_id: str = REQUEST_ID) -> bytes:
    return encode_message(
        {
            "protocol": PROTOCOL,
            "request_id": request_id,
            "outcome": "current",
            "cell_id": "cell-1",
            "logical_vault_id": "vault-1",
            "registry_attachment_id": "hosted-attachment-v1-" + "d" * 64,
            "attachment_epoch": 1,
            "bundle_revision": "e" * 64,
            "keyring_sha256": "f" * 64,
            "control_b64": "e30",
            "serving_membership_b64": "e30",
        },
        "httpBundleResponse",
    )


def _response(
    status: int,
    body: bytes,
    *,
    headers: tuple[tuple[str, str], ...] = (),
) -> bytes:
    reason = {200: "OK", 302: "Found", 503: "Service Unavailable"}.get(status, "Error")
    lines = [
        f"HTTP/1.1 {status} {reason}",
        f"Content-Length: {len(body)}",
        "Content-Type: application/json",
    ]
    lines.extend(f"{name}: {value}" for name, value in headers)
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body


def _read_request(connection: ssl.SSLSocket) -> tuple[str, str, dict[str, str], bytes]:
    raw = bytearray()
    while b"\r\n\r\n" not in raw:
        chunk = connection.recv(4096)
        if not chunk:
            raise RuntimeError("client closed before request headers")
        raw.extend(chunk)
    head, body = bytes(raw).split(b"\r\n\r\n", 1)
    lines = head.decode("ascii").split("\r\n")
    method, path, _ = lines[0].split(" ")
    headers = {
        name.lower(): value.strip() for name, value in (line.split(":", 1) for line in lines[1:])
    }
    length = int(headers["content-length"])
    while len(body) < length:
        body += connection.recv(length - len(body))
    return method, path, headers, body


@pytest.fixture(scope="module")
def tls_material(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    root = tmp_path_factory.mktemp("activation-ack-tls")
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Activation Ack Test CA")])
    now = dt.datetime.now(dt.UTC)
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    server = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = root / "ca.pem"
    cert_path = root / "server.pem"
    key_path = root / "server-key.pem"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(server.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca_path, cert_path, key_path


@pytest.fixture
def tls_server(
    tls_material: tuple[Path, Path, Path],
) -> Iterator[
    Callable[
        [Callable[[tuple[str, str, dict[str, str], bytes]], bytes | list[tuple[bytes, float]]]],
        tuple[HostedActivationAckHttpClient, list[tuple[str, str, dict[str, str], bytes]]],
    ]
]:
    ca_path, cert_path, key_path = tls_material
    running: list[tuple[socket.socket, threading.Thread, list[BaseException]]] = []

    def start(
        responder: Callable[
            [tuple[str, str, dict[str, str], bytes]], bytes | list[tuple[bytes, float]]
        ],
        *,
        server_hostname: str = "localhost",
        trust_ca: bool = True,
        handshake_delay: float = 0.0,
    ) -> tuple[HostedActivationAckHttpClient, list[tuple[str, str, dict[str, str], bytes]]]:
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        requests: list[tuple[str, str, dict[str, str], bytes]] = []
        errors: list[BaseException] = []
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert_path, key_path)

        def serve() -> None:
            try:
                plain, _ = listener.accept()
                with plain:
                    time.sleep(handshake_delay)
                    with server_context.wrap_socket(plain, server_side=True) as connection:
                        request = _read_request(connection)
                        requests.append(request)
                        reply = responder(request)
                        if isinstance(reply, bytes):
                            connection.sendall(reply)
                        else:
                            for chunk, delay in reply:
                                connection.sendall(chunk)
                                time.sleep(delay)
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError, TimeoutError):
                pass
            except BaseException as error:  # noqa: BLE001 - preserve helper-thread failures
                errors.append(error)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        running.append((listener, thread, errors))
        context = (
            ssl.create_default_context(cafile=str(ca_path))
            if trust_ca
            else ssl.create_default_context()
        )
        client = HostedActivationAckHttpClient(
            endpoint=listener.getsockname(),
            server_hostname=server_hostname,
            ssl_context=context,
            cell_id="cell-1",
            credential_loader=lambda: CredentialBundle({"credential-v1": SECRET}),
        )
        return client, requests

    yield start
    for listener, thread, errors in running:
        thread.join(2)
        listener.close()
        assert not thread.is_alive()
        assert errors == []


def test_real_tls_current_and_ack_send_fixed_paths_and_credentials(tls_server) -> None:
    client, current_requests = tls_server(lambda _: _response(200, _bundle()))
    current = client.current(REQUEST_ID, deadline=time.monotonic() + 1)

    ack_client, ack_requests = tls_server(lambda _: _response(200, _bundle()))
    acknowledged = ack_client.acknowledge(REQUEST_ID, _publication(), deadline=time.monotonic() + 1)

    assert current["outcome"] == acknowledged["outcome"] == "current"
    method, path, headers, body = current_requests[0]
    assert (method, path, body) == ("GET", "/cell-runtime/v1/activation/current", b"")
    assert headers["authorization"] == f"Bearer {SECRET}"
    assert headers["x-exomem-cell-id"] == "cell-1"
    assert headers["x-exomem-credential-version"] == "credential-v1"
    assert headers["x-exomem-activation-protocol"] == PROTOCOL
    assert headers["x-exomem-request-id"] == REQUEST_ID
    method, path, _, body = ack_requests[0]
    assert (method, path) == ("POST", "/cell-runtime/v1/activation/ack")
    assert json.loads(body)["publication"] == _publication()


@pytest.mark.parametrize(
    ("server_hostname", "trust_ca"), [("wrong.example", True), ("localhost", False)]
)
def test_tls_requires_expected_hostname_and_trusted_ca(
    tls_server, server_hostname, trust_ca
) -> None:
    client, _ = tls_server(
        lambda _: _response(200, _bundle()),
        server_hostname=server_hostname,
        trust_ca=trust_ca,
    )

    with pytest.raises(ActivationAckHttpError):
        client.current(REQUEST_ID, deadline=time.monotonic() + 1)


def test_redirect_is_refused_without_a_second_request(tls_server) -> None:
    client, requests = tls_server(
        lambda _: _response(302, b"{}", headers=(("Location", "https://attacker.invalid/"),))
    )

    with pytest.raises(ActivationAckHttpError):
        client.current(REQUEST_ID, deadline=time.monotonic() + 1)
    assert len(requests) == 1


@pytest.mark.parametrize(
    "reply",
    [
        b"HTTP/1.1 200 OK\r\nX-Oversized: " + b"x" * 8192,
        b"HTTP/1.1 200 OK\r\nContent-Length: 196609\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n{}",
    ],
)
def test_response_framing_and_sizes_are_bounded_before_body_read(tls_server, reply) -> None:
    client, _ = tls_server(lambda _: reply)

    with pytest.raises(ActivationAckHttpError):
        client.current(REQUEST_ID, deadline=time.monotonic() + 1)


def test_deadline_stops_a_trickled_response(tls_server) -> None:
    reply = _response(200, _bundle())
    client, _ = tls_server(lambda _: [(bytes([byte]), 0.02) for byte in reply])
    started = time.monotonic()

    with pytest.raises(ActivationAckHttpError):
        client.current(REQUEST_ID, deadline=started + 0.08)
    assert time.monotonic() - started < 1


def test_connect_and_tls_handshake_are_capped_at_half_a_second(tls_server) -> None:
    client, _ = tls_server(lambda _: _response(200, _bundle()), handshake_delay=0.8)
    started = time.monotonic()

    with pytest.raises(ActivationAckHttpError):
        client.current(REQUEST_ID, deadline=started + 2)
    assert time.monotonic() - started < 1


def test_repeated_dns_timeouts_share_one_in_flight_lookup_until_it_finishes(monkeypatch) -> None:
    release = threading.Event()
    lock = threading.Lock()
    calls = 0
    active = 0

    def stalled_resolution(*args, **kwargs):
        nonlocal active, calls
        with lock:
            calls += 1
            active += 1
        release.wait(2)
        with lock:
            active -= 1
        return []

    monkeypatch.setattr(socket, "getaddrinfo", stalled_resolution)
    client = HostedActivationAckHttpClient(
        endpoint=("slow.invalid", 8443),
        server_hostname="slow.invalid",
        ssl_context=ssl.create_default_context(),
        cell_id="cell-1",
        credential_loader=lambda: CredentialBundle({"credential-v1": SECRET}),
    )
    try:
        for _ in range(3):
            started = time.monotonic()
            with pytest.raises(ActivationAckHttpError):
                client.current(REQUEST_ID, deadline=started + 0.05)
            assert time.monotonic() - started < 0.5
        with lock:
            assert calls == 1
            assert active == 1
    finally:
        release.set()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with lock:
            if active == 0:
                break
        time.sleep(0.01)
    with lock:
        assert active == 0


def test_protocol_errors_are_mapped_without_exposing_credentials(tls_server) -> None:
    body = encode_message(
        {
            "protocol": PROTOCOL,
            "request_id": REQUEST_ID,
            "code": "ACK_DEADLINE_EXCEEDED",
            "retry_after_ms": 250,
        },
        "errorResponse",
    )
    client, _ = tls_server(lambda _: _response(503, body))

    with pytest.raises(ActivationAckHttpError) as caught:
        client.current(REQUEST_ID, deadline=time.monotonic() + 1)

    assert caught.value.code == "ACK_DEADLINE_EXCEEDED"
    assert caught.value.retry_after_ms == 250
    assert SECRET not in str(caught.value)
    assert SECRET not in repr(caught.value)


def test_success_requires_exact_request_correlation(tls_server) -> None:
    client, _ = tls_server(lambda _: _response(200, _bundle("0" * 64)))

    with pytest.raises(ActivationAckHttpError):
        client.current(REQUEST_ID, deadline=time.monotonic() + 1)


@pytest.mark.parametrize("offset", [-0.01, 5.01])
def test_deadline_must_be_future_and_at_most_five_seconds(offset) -> None:
    client = HostedActivationAckHttpClient(
        endpoint=("127.0.0.1", 9),
        server_hostname="localhost",
        ssl_context=ssl.create_default_context(),
        cell_id="cell-1",
        credential_loader=lambda: CredentialBundle({"credential-v1": SECRET}),
    )

    with pytest.raises(ActivationAckHttpError):
        client.current(REQUEST_ID, deadline=time.monotonic() + offset)


def test_production_factory_fixes_dns_port_ca_and_rejects_invalid_namespaces(monkeypatch) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    calls: list[str] = []
    monkeypatch.setattr(
        ssl,
        "create_default_context",
        lambda *, cafile: calls.append(cafile) or context,
    )

    client = HostedActivationAckHttpClient.for_platform_namespace("platform-1", cell_id="cell-1")

    assert client._endpoint == ("exomem-activation-ack.platform-1.svc", 8443)
    assert client._server_hostname == "exomem-activation-ack.platform-1.svc"
    assert calls == ["/run/exomem/activation-ack-trust/ca.pem"]
    for invalid in ("", "UPPER", "-prefix", "suffix-", "a" * 64, "two.labels"):
        with pytest.raises(ActivationAckHttpError):
            HostedActivationAckHttpClient.for_platform_namespace(invalid, cell_id="cell-1")
