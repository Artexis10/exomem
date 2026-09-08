from __future__ import annotations

import os
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = Path(__file__).resolve().parents[1]
PLATFORM = ROOT / "infra/helm/platform"
CELL = ROOT / "infra/helm/cell"
HELM = Path(os.environ["HELM_BIN"]) if "HELM_BIN" in os.environ else None
TRAEFIK = Path(os.environ["TRAEFIK_BIN"]) if "TRAEFIK_BIN" in os.environ else None


def _render(
    chart: Path,
    values: Path,
    *,
    namespace: str,
    overlays: tuple[Path, ...] = (),
    extra_args: tuple[str, ...] = (),
    release_name: str = "direct-edge-test",
) -> list[dict]:
    if HELM is None:
        pytest.skip("set HELM_BIN to run pinned Helm rendering")
    with tempfile.TemporaryDirectory(prefix="exomem-direct-edge-helm-") as directory:
        staged = Path(directory) / chart.name
        shutil.copytree(chart, staged)
        build = subprocess.run(
            [str(HELM), "dependency", "build", str(staged)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert build.returncode == 0, build.stdout + build.stderr
        result = subprocess.run(
            [
                str(HELM),
                "template",
                release_name,
                str(staged),
                "--namespace",
                namespace,
                "--values",
                str(staged / values.relative_to(chart)),
                *(
                    argument
                    for overlay in overlays
                    for argument in ("--values", str(staged / overlay.relative_to(chart)))
                ),
                "--include-crds",
                *extra_args,
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    return [
        document for document in yaml.safe_load_all(result.stdout) if isinstance(document, dict)
    ]


def _find(documents: list[dict], kind: str, name: str) -> dict:
    for document in documents:
        if document.get("kind") == kind and document.get("metadata", {}).get("name") == name:
            return document
    raise AssertionError(f"missing {kind}/{name}")


def test_direct_edge_is_disabled_by_default_and_enabled_overlay_is_private() -> None:
    default = _render(PLATFORM, PLATFORM / "values.validation.yaml", namespace="exomem-platform")
    assert not any(
        document.get("metadata", {}).get("name") == "exomem-direct-edge" for document in default
    )

    enabled = _render(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace="exomem-platform",
        overlays=(PLATFORM / "values.direct-edge.yaml",),
        release_name="exomem-platform",
    )
    deployment = _find(enabled, "Deployment", "exomem-direct-edge")
    service = _find(enabled, "Service", "exomem-direct-edge")
    config = _find(enabled, "ConfigMap", "exomem-direct-edge-config")
    private_config = _find(enabled, "ConfigMap", "exomem-private-transfer-config")
    policy = _find(enabled, "NetworkPolicy", "exomem-direct-edge")

    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    assert deployment["spec"]["replicas"] == 1
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert (
        container["image"]
        == "docker.io/traefik@sha256:21a3d83696379bac6434bb32e1dde0aff0e84ef2abd053ed3db87d3f45e749b2"
    )
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"] == [{"name": "https", "port": 8443, "targetPort": "https"}]
    assert "hostNetwork" not in deployment["spec"]["template"]["spec"]
    assert "hostPort" not in yaml.safe_dump(deployment)
    rendered = yaml.safe_dump(config)
    assert "kubernetesCRD" not in rendered
    assert "kubernetesIngress" not in rendered
    assert "api@internal" not in rendered
    assert "accessLog" not in rendered
    assert "exomem-gateway.exomem-platform.svc.cluster.local:8443" in rendered
    assert "exomem-platform-traefik.exomem-platform.svc.cluster.local:8444" in rendered
    assert "insecureSkipVerify" not in rendered
    assert "X-Exomem-Gateway-Ingress: direct-edge-v1" in rendered
    assert "Path(`/api/exomem/mcp/v1`)" in rendered
    assert ".well-known/oauth-protected-resource/api/exomem/mcp/v1" in rendered
    assert (
        "PathRegexp(`^/cells/[A-Za-z0-9][A-Za-z0-9_-]{0,127}/public/exomem/v2/transfers/upload$`)"
        in rendered
    )
    assert "Method(`PUT`) || Method(`OPTIONS`)" in rendered
    assert "Method(`GET`) || Method(`OPTIONS`)" in rendered
    assert policy["spec"]["policyTypes"] == ["Ingress", "Egress"]
    private_traefik = _find(enabled, "Deployment", "exomem-platform-traefik")
    private_args = private_traefik["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--providers.kubernetescrd.crossProviderNamespaces=cell-alpha,cell-beta" in private_args
    private_service = _find(enabled, "Service", "exomem-platform-traefik")
    assert {
        "name": "private-transfe",
        "port": 8444,
        "protocol": "TCP",
        "targetPort": "private-transfe",
    } in private_service["spec"]["ports"]
    private_rendered = yaml.safe_dump(private_config)
    assert "cell-alpha-private-transfer:" in private_rendered
    assert "cell-beta-private-transfer:" in private_rendered


def test_private_transfer_profile_keeps_control_on_web_and_uses_file_transport() -> None:
    values = CELL / "values.validation.yaml"
    documents = _render(
        CELL,
        values,
        namespace="cell-alpha",
        extra_args=("--set", "routes.enabled=true"),
    )
    transfer = _find(documents, "IngressRoute", "cell-alpha-transfer")
    control = _find(documents, "IngressRoute", "cell-alpha-control")
    assert transfer["spec"]["entryPoints"] == ["web"]
    assert control["spec"]["entryPoints"] == ["web"]

    private = _render(
        CELL,
        values,
        namespace="cell-alpha",
        extra_args=(
            "--set",
            "routes.enabled=true",
            "--set",
            "routes.transferProfile=private-transfer",
            "--set",
            "routes.transferServersTransport=cell-alpha-private-transfer@file",
        ),
    )
    transfer = _find(private, "IngressRoute", "cell-alpha-transfer")
    control = _find(private, "IngressRoute", "cell-alpha-control")
    assert transfer["spec"]["entryPoints"] == ["private-transfer"]
    assert transfer["spec"]["routes"][0]["services"] == [
        {
            "name": "cell-alpha",
            "port": 8765,
            "scheme": "https",
            "serversTransport": "cell-alpha-private-transfer@file",
        }
    ]
    assert control["spec"]["entryPoints"] == ["web"]


def _certificate(
    directory: Path,
    name: str,
    issuer: tuple[object, x509.Certificate] | None = None,
    *,
    client: bool = False,
    expired: bool = False,
) -> tuple[object, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer[1].subject if issuer else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=2) if expired else now - timedelta(minutes=1))
        .not_valid_after(now - timedelta(hours=1) if expired else now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=issuer is None, path_length=None), True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(
                (issuer[0] if issuer else key).public_key()
            ),
            False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=issuer is None,
                crl_sign=issuer is None,
                encipher_only=False,
                decipher_only=False,
            ),
            True,
        )
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), False)
    )
    if issuer:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.CLIENT_AUTH if client else ExtendedKeyUsageOID.SERVER_AUTH]
            ),
            False,
        )
    certificate = builder.sign(issuer[0] if issuer else key, hashes.SHA256())
    (directory / f"{name}.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (directory / f"{name}.crt").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return key, certificate


def _backend(
    directory: Path,
    ca_directory: Path,
    name: str,
    issuer: tuple[object, x509.Certificate],
    *,
    expired: bool = False,
) -> tuple[ThreadingHTTPServer, threading.Thread, list[dict[str, str]]]:
    seen: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def _reply(self) -> None:
            seen.append(dict(self.headers))
            self.send_response(200)
            self.send_header("Content-Length", str(len(name)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(name.encode())

        do_DELETE = _reply
        do_GET = _reply
        do_HEAD = _reply
        do_OPTIONS = _reply
        do_POST = _reply
        do_PUT = _reply

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    _certificate(directory, name, issuer, expired=expired)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(directory / f"{name}.crt", directory / f"{name}.key")
    context.load_verify_locations(ca_directory / "ca.crt")
    context.verify_mode = ssl.CERT_REQUIRED
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, seen


def _request(
    port: int,
    path: str,
    method: str = "GET",
    *,
    host: str = "transfer.example.test",
    sni: str | None = None,
) -> bytes:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", port), timeout=2) as raw:
        with context.wrap_socket(raw, server_hostname=sni or host) as stream:
            stream.sendall(
                (
                    f"{method} {path} HTTP/1.1\r\n"
                    f"Host: {host}\r\n"
                    "X-Forwarded-For: 198.51.100.8\r\n"
                    "CF-Connecting-IP: 198.51.100.9\r\n"
                    "Connection: close\r\n\r\n"
                ).encode()
            )
            return b"".join(iter(lambda: stream.recv(4096), b""))


def test_rendered_direct_edge_config_runs_on_pinned_traefik_with_verified_backends(
    tmp_path: Path,
) -> None:
    if TRAEFIK is None or not TRAEFIK.is_file():
        pytest.skip("set TRAEFIK_BIN to the pinned Traefik 3.7.6 binary")
    version = subprocess.run([str(TRAEFIK), "version"], check=False, capture_output=True, text=True)
    assert version.returncode == 0
    assert re.search(r"^Version:\s+3\.7\.6$", version.stdout, re.MULTILINE)
    documents = _render(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace="exomem-platform",
        overlays=(PLATFORM / "values.direct-edge.yaml",),
        release_name="exomem-platform",
    )
    config = _find(documents, "ConfigMap", "exomem-direct-edge-config")["data"]
    public, ca, client = (tmp_path / part for part in ("public", "ca", "client"))
    for directory in (public, ca, client):
        directory.mkdir()
    issuer = _certificate(ca, "test-ca")
    shutil.copy(ca / "test-ca.crt", ca / "ca.crt")
    _certificate(public, "transfer.example.test", issuer)
    (public / "tls.crt").write_bytes((public / "transfer.example.test.crt").read_bytes())
    (public / "tls.key").write_bytes((public / "transfer.example.test.key").read_bytes())
    _certificate(client, "edge-client", issuer, client=True)
    (client / "tls.crt").write_bytes((client / "edge-client.crt").read_bytes())
    (client / "tls.key").write_bytes((client / "edge-client.key").read_bytes())
    gateway, gateway_thread, gateway_seen = _backend(tmp_path, ca, "gateway.internal.test", issuer)
    transfer, transfer_thread, transfer_seen = _backend(
        tmp_path, ca, "transfer.internal.test", issuer
    )
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        edge_port = available.getsockname()[1]
    dynamic = config["dynamic.yaml"]
    dynamic = (
        dynamic.replace(
            "https://exomem-gateway.exomem-platform.svc.cluster.local:8443",
            f"https://127.0.0.1:{gateway.server_port}",
        )
        .replace(
            "https://exomem-platform-traefik.exomem-platform.svc.cluster.local:8444",
            f"https://127.0.0.1:{transfer.server_port}",
        )
        .replace("exomem-gateway.exomem-platform.svc.cluster.local", "gateway.internal.test")
        .replace(
            "exomem-platform-traefik.exomem-platform.svc.cluster.local", "transfer.internal.test"
        )
    )
    dynamic = dynamic.replace("/var/run/exomem-direct-edge/public", str(public))
    dynamic = dynamic.replace("/var/run/exomem-direct-edge/ca", str(ca))
    dynamic = dynamic.replace("/var/run/exomem-direct-edge/client", str(client))
    static = config["static.yaml"].replace(":8443", f"127.0.0.1:{edge_port}")
    static = static.replace("/etc/traefik/dynamic/dynamic.yaml", str(tmp_path / "dynamic.yaml"))
    (tmp_path / "static.yaml").write_text(static, encoding="utf-8")

    def start(config_text: str) -> subprocess.Popen[bytes]:
        (tmp_path / "dynamic.yaml").write_text(config_text, encoding="utf-8")
        log = (tmp_path / "traefik.log").open("a", encoding="utf-8")
        return subprocess.Popen(
            [str(TRAEFIK), f"--configFile={tmp_path / 'static.yaml'}"],
            stdout=log,
            stderr=log,
        )

    def denied(config_text: str) -> None:
        process = start(config_text)
        try:
            for _ in range(30):
                try:
                    response = _request(edge_port, "/api/exomem/mcp/v1")
                    if response:
                        assert b"200" not in response[:32]
                        return
                except OSError:
                    pass
                time.sleep(0.1)
            raise AssertionError((tmp_path / "traefik.log").read_text(encoding="utf-8"))
        finally:
            process.terminate()
            process.wait(timeout=5)

    process = start(dynamic)
    try:
        for _ in range(40):
            try:
                if b"200" in _request(edge_port, "/api/exomem/mcp/v1")[:32]:
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError((tmp_path / "traefik.log").read_text(encoding="utf-8"))
        gateway_seen.clear()
        transfer_seen.clear()
        assert b"200" in _request(edge_port, "/api/exomem/mcp/v1")[:32]
        assert (
            b"200"
            in _request(
                edge_port, "/.well-known/oauth-protected-resource/api/exomem/mcp/v1", "HEAD"
            )[:32]
        )
        assert (
            b"200"
            in _request(edge_port, "/cells/cell_A/public/exomem/v2/transfers/upload", "PUT")[:32]
        )
        assert (
            b"200"
            in _request(edge_port, "/cells/cell_A/public/exomem/v2/transfers/download", "OPTIONS")[
                :32
            ]
        )
        for path in (
            "/api%2fexomem/mcp/v1",
            "/cells/cell_A/private/exomem/v1",
            "/cells/cell_A/public/exomem/v2/transfers/upload/extra",
        ):
            assert b"200" not in _request(edge_port, path)[:32]
        assert (
            b"200"
            not in _request(
                edge_port,
                "/api/exomem/mcp/v1",
                host="other.example.test",
                sni="transfer.example.test",
            )[:32]
        )
        with pytest.raises(ssl.SSLError):
            _request(edge_port, "/api/exomem/mcp/v1", sni="other.example.test")
        assert gateway_seen and transfer_seen
        for headers in gateway_seen + transfer_seen:
            assert "X-Forwarded-For" not in headers
            assert "CF-Connecting-IP" not in headers
        assert gateway_seen[-1]["X-Exomem-Gateway-Ingress"] == "direct-edge-v1"
        assert "X-Exomem-Gateway-Ingress" not in transfer_seen[-1]
        assert transfer_seen[-1]["Host"] == "transfer.example.test"
        process.terminate()
        process.wait(timeout=5)

        untrusted = tmp_path / "untrusted"
        untrusted.mkdir()
        untrusted_issuer = _certificate(untrusted, "untrusted-ca")
        bad_gateway, bad_thread, bad_seen = _backend(
            tmp_path, ca, "gateway-untrusted.test", untrusted_issuer
        )
        try:
            denied(
                dynamic.replace(
                    f"https://127.0.0.1:{gateway.server_port}",
                    f"https://127.0.0.1:{bad_gateway.server_port}",
                ).replace("gateway.internal.test", "gateway-untrusted.test")
            )
            assert not bad_seen
        finally:
            bad_gateway.shutdown()
            bad_gateway.server_close()
            bad_thread.join(timeout=5)

        expired_gateway, expired_thread, expired_seen = _backend(
            tmp_path, ca, "gateway-expired.test", issuer, expired=True
        )
        try:
            denied(
                dynamic.replace(
                    f"https://127.0.0.1:{gateway.server_port}",
                    f"https://127.0.0.1:{expired_gateway.server_port}",
                ).replace("gateway.internal.test", "gateway-expired.test")
            )
            assert not expired_seen
        finally:
            expired_gateway.shutdown()
            expired_gateway.server_close()
            expired_thread.join(timeout=5)

        untrusted_client = tmp_path / "untrusted-client"
        untrusted_client.mkdir()
        _certificate(untrusted_client, "edge-client", untrusted_issuer, client=True)
        (untrusted_client / "tls.crt").write_bytes(
            (untrusted_client / "edge-client.crt").read_bytes()
        )
        (untrusted_client / "tls.key").write_bytes(
            (untrusted_client / "edge-client.key").read_bytes()
        )
        gateway_seen.clear()
        denied(dynamic.replace(str(client), str(untrusted_client)))
        assert not gateway_seen
    finally:
        process.terminate()
        process.wait(timeout=5)
        for server, thread in ((gateway, gateway_thread), (transfer, transfer_thread)):
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def test_private_transfer_listener_routes_two_cells_and_rejects_control(tmp_path: Path) -> None:
    if TRAEFIK is None or not TRAEFIK.is_file():
        pytest.skip("set TRAEFIK_BIN to the pinned Traefik 3.7.6 binary")
    ca, listener, client = (tmp_path / part for part in ("ca", "listener", "client"))
    for directory in (ca, listener, client):
        directory.mkdir()
    issuer = _certificate(ca, "private-test-ca")
    shutil.copy(ca / "private-test-ca.crt", ca / "ca.crt")
    _certificate(listener, "private.internal.test", issuer)
    (listener / "tls.crt").write_bytes((listener / "private.internal.test.crt").read_bytes())
    (listener / "tls.key").write_bytes((listener / "private.internal.test.key").read_bytes())
    _certificate(client, "private-edge-client", issuer, client=True)
    (client / "tls.crt").write_bytes((client / "private-edge-client.crt").read_bytes())
    (client / "tls.key").write_bytes((client / "private-edge-client.key").read_bytes())
    alpha, alpha_thread, alpha_seen = _backend(tmp_path, ca, "cell-alpha.internal.test", issuer)
    beta, beta_thread, beta_seen = _backend(tmp_path, ca, "cell-beta.internal.test", issuer)
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    dynamic = f"""
tls:
  certificates:
    - certFile: {listener}/tls.crt
      keyFile: {listener}/tls.key
  options:
    default:
      minVersion: VersionTLS12
      sniStrict: true
      clientAuth:
        caFiles: [{ca}/ca.crt]
        clientAuthType: RequireAndVerifyClientCert
http:
  routers:
    alpha:
      entryPoints: [private-transfer]
      rule: Host(`transfer.example.test`) && Path(`/cells/cell-alpha/public/exomem/v2/transfers/upload`) && Method(`PUT`)
      service: alpha
      tls: {{}}
    beta:
      entryPoints: [private-transfer]
      rule: Host(`transfer.example.test`) && Path(`/cells/cell-beta/public/exomem/v2/transfers/download`) && Method(`GET`)
      service: beta
      tls: {{}}
  services:
    alpha:
      loadBalancer:
        serversTransport: cell-alpha-private-transfer
        servers: [{{url: https://127.0.0.1:{alpha.server_port}}}]
    beta:
      loadBalancer:
        serversTransport: cell-beta-private-transfer
        servers: [{{url: https://127.0.0.1:{beta.server_port}}}]
  serversTransports:
    cell-alpha-private-transfer:
      serverName: cell-alpha.internal.test
      rootCAs: [{ca}/ca.crt]
      certificates: [{{certFile: {client}/tls.crt, keyFile: {client}/tls.key}}]
    cell-beta-private-transfer:
      serverName: cell-beta.internal.test
      rootCAs: [{ca}/ca.crt]
      certificates: [{{certFile: {client}/tls.crt, keyFile: {client}/tls.key}}]
"""
    static = f"entryPoints:\n  private-transfer:\n    address: 127.0.0.1:{port}\nproviders:\n  file:\n    filename: {tmp_path}/dynamic.yaml\n"
    (tmp_path / "dynamic.yaml").write_text(dynamic, encoding="utf-8")
    (tmp_path / "static.yaml").write_text(static, encoding="utf-8")
    process = subprocess.Popen([str(TRAEFIK), f"--configFile={tmp_path / 'static.yaml'}"])
    context = ssl.create_default_context(cafile=str(ca / "ca.crt"))
    context.check_hostname = False
    context.load_cert_chain(client / "tls.crt", client / "tls.key")

    def request(path: str, method: str) -> bytes:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as raw:
            with context.wrap_socket(raw, server_hostname="private.internal.test") as stream:
                stream.sendall(
                    f"{method} {path} HTTP/1.1\r\nHost: transfer.example.test\r\nConnection: close\r\n\r\n".encode()
                )
                return b"".join(iter(lambda: stream.recv(4096), b""))

    try:
        for _ in range(30):
            try:
                if (
                    b"200"
                    in request("/cells/cell-beta/public/exomem/v2/transfers/download", "GET")[:32]
                ):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("private listener did not start")
        alpha_seen.clear()
        beta_seen.clear()
        assert b"200" in request("/cells/cell-alpha/public/exomem/v2/transfers/upload", "PUT")[:32]
        assert b"200" in request("/cells/cell-beta/public/exomem/v2/transfers/download", "GET")[:32]
        assert b"200" not in request("/cells/cell-alpha/private/exomem/v1", "POST")[:32]
        assert alpha_seen and beta_seen
    finally:
        process.terminate()
        process.wait(timeout=5)
        for server, thread in ((alpha, alpha_thread), (beta, beta_thread)):
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
