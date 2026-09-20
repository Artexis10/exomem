"""The name the cell dials and the name the certificate carries must be one name.

Every other test in this repair pins one side. The client suite asserted the
hostname the client builds; the issuer suite asserted the SAN the certificate
gets. Both passed while the two disagreed, and a disagreement here means no
capability-bound cell can complete a governed write at all.

These tests only pass if the two sides agree, and the last one proves it the
way the runtime finds out: a real TLS handshake.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import socket
import ssl
import sys
import threading
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from exomem.hosted_activation_ack_http import HostedActivationAckHttpClient

from exomem_provisioner.activation_ack_configuration import (
    activation_ack_server_dns_name,
    validate_activation_ack_server_certificate,
)

NAMESPACE = "exomem-platform"
ROOT = Path(__file__).resolve().parents[3]


def _issuer():
    path = ROOT / "infra/scripts/activation_ack_certificate_handoff.py"
    spec = importlib.util.spec_from_file_location("activation_ack_certificate_handoff", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _client_hostname(namespace: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Ask the real client factory, rather than restating what it ought to do.

    The factory loads the pinned trust file to build its context, so the file
    has to be a real bundle even when only the hostname is under test.
    """

    trust = tmp_path / "client-ca.pem"
    if not trust.exists():
        _, authority = _issuer().build_authority(
            now=dt.datetime.now(dt.UTC), lifetime_days=3650, common_name="Exomem test CA"
        )
        trust.write_bytes(authority.public_bytes(serialization.Encoding.PEM))
    monkeypatch.setattr("exomem.hosted_activation_ack_http.TRUST_CA_PATH", trust)
    client = HostedActivationAckHttpClient.for_platform_namespace(namespace, cell_id="cell-1")
    return client._server_hostname


def test_the_client_dials_exactly_the_name_the_certificate_is_issued_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _client_hostname(NAMESPACE, tmp_path, monkeypatch) == activation_ack_server_dns_name(
        NAMESPACE
    )


def test_the_validator_accepts_a_certificate_named_for_what_the_client_dials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issuer = _issuer()
    now = dt.datetime.now(dt.UTC)
    hostname = _client_hostname(NAMESPACE, tmp_path, monkeypatch)
    authority_key, authority = issuer.build_authority(
        now=now, lifetime_days=3650, common_name="Exomem test CA"
    )
    _, leaf = issuer.build_listener_certificate(
        authority_key=authority_key,
        authority=authority,
        dns_name=hostname,
        now=now,
        lifetime_days=90,
    )

    report = validate_activation_ack_server_certificate(
        leaf.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        platform_namespace=NAMESPACE,
        trust_pem=authority.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        now=now,
    )

    assert report["dns_name"] == hostname


def test_a_real_handshake_against_the_issued_certificate_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check the runtime actually performs, which no unit assertion replaces."""

    issuer = _issuer()
    now = dt.datetime.now(dt.UTC)
    hostname = _client_hostname(NAMESPACE, tmp_path, monkeypatch)
    authority_key, authority = issuer.build_authority(
        now=now, lifetime_days=3650, common_name="Exomem test CA"
    )
    leaf_key, leaf = issuer.build_listener_certificate(
        authority_key=authority_key,
        authority=authority,
        dns_name=activation_ack_server_dns_name(NAMESPACE),
        now=now,
        lifetime_days=90,
    )
    trust = tmp_path / "trust.pem"
    certificate = tmp_path / "leaf.pem"
    key = tmp_path / "leaf.key"
    trust.write_bytes(authority.public_bytes(serialization.Encoding.PEM))
    certificate.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(str(certificate), str(key))
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]

    def serve() -> None:
        try:
            accepted, _ = listener.accept()
        except OSError:
            return
        try:
            server_context.wrap_socket(accepted, server_side=True).close()
        except OSError:
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        # Exactly how the cell builds its context: the pinned CA file, default
        # verification, and the hostname the client factory produced.
        context = ssl.create_default_context(cafile=str(trust))
        secured = context.wrap_socket(
            socket.create_connection(("127.0.0.1", port), timeout=10),
            server_hostname=hostname,
        )
        assert secured.getpeercert() is not None
        secured.close()
    finally:
        listener.close()
        thread.join(timeout=5)
