from __future__ import annotations

import copy
import os
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
import test_hosted_transfer_v2 as transfer_v2
import uvicorn
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = Path(os.environ.get("EXOMEM_TEST_ROOT", Path(__file__).resolve().parents[1]))
PLATFORM = ROOT / "infra/helm/platform"
CELL = ROOT / "infra/helm/cell"
HELM = Path(os.environ["HELM_BIN"]) if "HELM_BIN" in os.environ else None
TRAEFIK = Path(os.environ.get("TRAEFIK_BIN", "/tmp/exomem-direct-traefik-3.7.6"))
PLATFORM_NAMESPACE = "exomem-platform"
TRANSFER_HOST = "transfer.example.test"


def _helm_template(
    chart: Path,
    values: Path,
    *,
    namespace: str,
    overlays: tuple[Path, ...] = (),
    extra_args: tuple[str, ...] = (),
    release_name: str = "direct-edge-test",
) -> subprocess.CompletedProcess[str]:
    if HELM is None or not HELM.is_file():
        pytest.skip("set HELM_BIN to run the pinned Helm acceptance tests")
    with tempfile.TemporaryDirectory(prefix="exomem-direct-edge-helm-") as directory:
        staged = Path(directory) / chart.name
        shutil.copytree(chart, staged)
        lock_path = staged / "Chart.lock"
        missing_archives: list[Path] = []
        locked_dependencies: list[dict[str, str]] = []
        if lock_path.is_file():
            lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
            locked_dependencies = lock["dependencies"]
            for dependency in locked_dependencies:
                archive = staged / "charts" / f"{dependency['name']}-{dependency['version']}.tgz"
                if not archive.is_file():
                    missing_archives.append(archive)
        if missing_archives:
            helm_env = os.environ | {
                "HELM_CACHE_HOME": str(Path(directory) / "helm-cache"),
                "HELM_CONFIG_HOME": str(Path(directory) / "helm-config"),
                "HELM_DATA_HOME": str(Path(directory) / "helm-data"),
                "HELM_REGISTRY_CONFIG": str(Path(directory) / "helm-config/registry.json"),
                "HELM_REPOSITORY_CACHE": str(Path(directory) / "helm-cache/repository"),
                "HELM_REPOSITORY_CONFIG": str(Path(directory) / "helm-config/repositories.yaml"),
            }
            for index, repository in enumerate(
                dict.fromkeys(dependency["repository"] for dependency in locked_dependencies)
            ):
                add = subprocess.run(
                    [str(HELM), "repo", "add", f"locked-{index}", repository],
                    cwd=ROOT,
                    env=helm_env,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                assert add.returncode == 0, add.stdout + add.stderr
            build = subprocess.run(
                [str(HELM), "dependency", "build", str(staged)],
                cwd=ROOT,
                env=helm_env,
                check=False,
                capture_output=True,
                text=True,
            )
            assert build.returncode == 0, build.stdout + build.stderr
            assert all(archive.is_file() for archive in missing_archives)
        return subprocess.run(
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


def _render(
    chart: Path,
    values: Path,
    *,
    namespace: str,
    overlays: tuple[Path, ...] = (),
    extra_args: tuple[str, ...] = (),
    release_name: str = "direct-edge-test",
) -> tuple[str, list[dict[str, Any]]]:
    result = _helm_template(
        chart,
        values,
        namespace=namespace,
        overlays=overlays,
        extra_args=extra_args,
        release_name=release_name,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout, [
        document for document in yaml.safe_load_all(result.stdout) if isinstance(document, dict)
    ]


def _platform_render(*extra_args: str) -> tuple[str, list[dict[str, Any]]]:
    return _render(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace=PLATFORM_NAMESPACE,
        overlays=(PLATFORM / "values.direct-edge.yaml",),
        extra_args=extra_args,
        release_name="exomem-platform",
    )


def _cell_render(namespace: str) -> list[dict[str, Any]]:
    _, documents = _render(
        CELL,
        CELL / "values.validation.yaml",
        namespace=namespace,
        release_name=namespace,
        extra_args=(
            "--set",
            f"resourceName={namespace}",
            "--set",
            f"cellId={namespace}",
            "--set",
            f"providerIdentity.cellId={namespace}",
            "--set",
            "routes.enabled=true",
            "--set",
            "routes.transferProfile=private-transfer",
            "--set",
            f"routes.transferServersTransport={namespace}-private-transfer@file",
            "--set",
            "routes.privatePeerHostname=exomem-platform-traefik.exomem-platform.svc.cluster.local",
        ),
    )
    return documents


def _find(documents: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    matches = [
        document
        for document in documents
        if document.get("kind") == kind and document.get("metadata", {}).get("name") == name
    ]
    assert len(matches) == 1, f"expected one {kind}/{name}, found {len(matches)}"
    return matches[0]


def _container(deployment: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [
        container
        for container in deployment["spec"]["template"]["spec"]["containers"]
        if container["name"] == name
    ]
    assert len(matches) == 1, f"expected one container {name}, found {len(matches)}"
    return matches[0]


@dataclass(frozen=True)
class MountedPath:
    container_root: PurePosixPath
    host_root: Path
    volume: dict[str, Any]


class MaterializedPod:
    def __init__(self, mounts: list[MountedPath]) -> None:
        self.mounts = mounts

    def _mount_for(self, configured_path: str) -> MountedPath:
        path = PurePosixPath(configured_path)
        matches = [
            mount
            for mount in self.mounts
            if path == mount.container_root or mount.container_root in path.parents
        ]
        assert matches, (
            f"configured path is not backed by a rendered volume mount: {configured_path}"
        )
        return max(matches, key=lambda item: len(item.container_root.parts))

    def resolve(self, configured_path: str, *, must_exist: bool = True) -> Path:
        path = PurePosixPath(configured_path)
        mount = self._mount_for(configured_path)
        resolved = mount.host_root.joinpath(*path.relative_to(mount.container_root).parts)
        if must_exist:
            assert resolved.exists(), (
                f"rendered volume does not supply configured path {configured_path}"
            )
        return resolved

    def source_for(self, configured_path: str) -> dict[str, Any]:
        return self._mount_for(configured_path).volume


def _materialize_pod(
    deployment: dict[str, Any],
    container_name: str,
    documents: list[dict[str, Any]],
    root: Path,
    secret_payloads: dict[str, dict[str, bytes]],
) -> MaterializedPod:
    pod = deployment["spec"]["template"]["spec"]
    volumes = {volume["name"]: volume for volume in pod.get("volumes", [])}
    mounts: list[MountedPath] = []
    for index, mount in enumerate(_container(deployment, container_name).get("volumeMounts", [])):
        assert mount["name"] in volumes, f"volumeMount {mount['name']} has no rendered volume"
        volume = volumes[mount["name"]]
        host_root = root / f"{index:02d}-{mount['name']}"
        host_root.mkdir(parents=True)
        if "configMap" in volume:
            config_name = volume["configMap"]["name"]
            data = _find(documents, "ConfigMap", config_name).get("data", {})
            assert data, f"ConfigMap/{config_name} has no data"
            for filename, content in data.items():
                target = host_root / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
        elif "secret" in volume:
            secret_name = volume["secret"]["secretName"]
            assert secret_name in secret_payloads, f"no synthetic payload for Secret/{secret_name}"
            for filename, content in secret_payloads[secret_name].items():
                target = host_root / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
        mounts.append(
            MountedPath(
                PurePosixPath(mount["mountPath"]),
                host_root,
                volume,
            )
        )
    return MaterializedPod(mounts)


def _remap_mounted_paths(value: Any, pod: MaterializedPod) -> Any:
    if isinstance(value, dict):
        return {key: _remap_mounted_paths(item, pod) for key, item in value.items()}
    if isinstance(value, list):
        return [_remap_mounted_paths(item, pod) for item in value]
    if isinstance(value, str) and value.startswith("/"):
        return str(pod.resolve(value))
    return value


def _configured_file_paths(value: Any) -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            paths.extend(_configured_file_paths(item))
    elif isinstance(value, list):
        for item in value:
            paths.extend(_configured_file_paths(item))
    elif isinstance(value, str) and value.startswith("/"):
        paths.append(value)
    return paths


def _map_service_socket(dynamic: dict[str, Any], service: str, port: int) -> None:
    servers = dynamic["http"]["services"][service]["loadBalancer"]["servers"]
    assert len(servers) == 1
    original = urlsplit(servers[0]["url"])
    assert original.scheme == "https" and original.hostname and original.port
    servers[0]["url"] = urlunsplit(("https", f"127.0.0.1:{port}", original.path, "", ""))


def _certificate(
    directory: Path,
    filename: str,
    dns_name: str,
    issuer: tuple[object, x509.Certificate] | None = None,
    *,
    usage: ExtendedKeyUsageOID | None = None,
    expired: bool = False,
) -> tuple[object, x509.Certificate]:
    directory.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_name)])
    now = datetime.now(UTC)
    is_ca = issuer is None
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer[1].subject if issuer else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=2) if expired else now - timedelta(minutes=1))
        .not_valid_after(now - timedelta(hours=1) if expired else now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=is_ca, path_length=None), True)
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
                key_encipherment=not is_ca,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=is_ca,
                crl_sign=is_ca,
                encipher_only=False,
                decipher_only=False,
            ),
            True,
        )
    )
    if not is_ca:
        assert usage is not None
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(dns_name)]), False
        ).add_extension(x509.ExtendedKeyUsage([usage]), False)
    certificate = builder.sign(issuer[0] if issuer else key, hashes.SHA256())
    (directory / f"{filename}.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (directory / f"{filename}.crt").write_bytes(
        certificate.public_bytes(serialization.Encoding.PEM)
    )
    return key, certificate


def _pem(path: Path) -> bytes:
    return path.read_bytes()


def _free_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def _wait_for(
    predicate: Callable[[], bool], *, timeout: float = 5.0, interval: float = 0.02
) -> None:
    deadline = time.monotonic() + timeout
    event = threading.Event()
    while time.monotonic() < deadline:
        if predicate():
            return
        event.wait(interval)
    raise AssertionError("condition did not become true before deadline")


def _status(response: bytes) -> int:
    match = re.match(rb"HTTP/1\.[01] (\d{3})", response)
    assert match, response[:200]
    return int(match.group(1))


def _request(
    port: int,
    path: str,
    method: str = "GET",
    *,
    host: str = TRANSFER_HOST,
    sni: str | None = None,
    ca_file: Path | None = None,
    certificate: tuple[Path, Path] | None = None,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    timeout: float = 3,
) -> bytes:
    context = (
        ssl.create_default_context(cafile=str(ca_file))
        if ca_file
        else ssl._create_unverified_context()
    )
    if certificate:
        context.load_cert_chain(certificate[0], certificate[1])
    request_headers = {"Host": host, "Connection": "close", **(headers or {})}
    if body and "Content-Length" not in request_headers:
        request_headers["Content-Length"] = str(len(body))
    wire = (
        f"{method} {path} HTTP/1.1\r\n"
        + "".join(f"{name}: {value}\r\n" for name, value in request_headers.items())
        + "\r\n"
    )
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=sni or host) as stream:
            stream.settimeout(timeout)
            stream.sendall(wire.encode() + body)
            chunks: list[bytes] = []
            while chunk := stream.recv(65536):
                chunks.append(chunk)
            return b"".join(chunks)


class _RecordingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self) -> None:
        self.server.seen.append(  # type: ignore[attr-defined]
            {
                **{name.lower(): value for name, value in self.headers.items()},
                ":path": self.path,
            }
        )
        if self.headers.get("X-Test-Block") == "1":
            self.server.block_entered.set()  # type: ignore[attr-defined]
            self.server.block_release.wait(5)  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        body = self.server.response_body  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_DELETE = _reply
    do_GET = _reply
    do_HEAD = _reply
    do_OPTIONS = _reply
    do_POST = _reply
    do_PUT = _reply

    def log_message(self, _format: str, *_args: object) -> None:
        pass


class _StreamingHandler(_RecordingHandler):
    def do_GET(self) -> None:
        self.server.seen.append(  # type: ignore[attr-defined]
            {
                **{name.lower(): value for name, value in self.headers.items()},
                ":path": self.path,
            }
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.write(b"5\r\nfirst\r\n")
        self.wfile.flush()
        self.server.first_chunk.set()  # type: ignore[attr-defined]
        block = b"x" * 65536
        try:
            for _ in range(4096):
                self.wfile.write(f"{len(block):X}\r\n".encode() + block + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.server.completed.set()  # type: ignore[attr-defined]
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            self.server.cancelled.set()  # type: ignore[attr-defined]


class _TestHTTPServer(ThreadingHTTPServer):
    request_queue_size = 128


@dataclass
class RunningHTTPServer:
    server: ThreadingHTTPServer
    thread: threading.Thread
    seen: list[dict[str, str]]

    def close(self) -> None:
        self.server.block_release.set()  # type: ignore[attr-defined]
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _tls_backend(
    certificate: tuple[Path, Path],
    client_ca: Path,
    *,
    body: bytes,
    streaming: bool = False,
) -> RunningHTTPServer:
    handler = _StreamingHandler if streaming else _RecordingHandler
    server = _TestHTTPServer(("127.0.0.1", 0), handler)
    server.seen = []  # type: ignore[attr-defined]
    server.response_body = body  # type: ignore[attr-defined]
    server.first_chunk = threading.Event()  # type: ignore[attr-defined]
    server.cancelled = threading.Event()  # type: ignore[attr-defined]
    server.completed = threading.Event()  # type: ignore[attr-defined]
    server.block_entered = threading.Event()  # type: ignore[attr-defined]
    server.block_release = threading.Event()  # type: ignore[attr-defined]
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate[0], certificate[1])
    context.load_verify_locations(client_ca)
    context.verify_mode = ssl.CERT_REQUIRED
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return RunningHTTPServer(server, thread, server.seen)  # type: ignore[arg-type]


@dataclass
class RunningUvicorn:
    server: uvicorn.Server
    thread: threading.Thread

    @property
    def port(self) -> int:
        return int(self.server.config.port)

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()


def _real_transfer_cell(
    root: Path,
    server_certificate: tuple[Path, Path],
    client_ca: Path,
) -> tuple[RunningUvicorn, transfer_v2.FakeSecurityAuthority]:
    authority = transfer_v2.FakeSecurityAuthority()
    config = replace(
        transfer_v2._config(root),
        cell_id="cell-alpha",
        transfer_host=TRANSFER_HOST,
    )
    app, _, _ = transfer_v2._app(root, authority, config_value=config)
    port = _free_port()
    uvicorn_config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        ssl_certfile=str(server_certificate[0]),
        ssl_keyfile=str(server_certificate[1]),
        ssl_ca_certs=str(client_ca),
        ssl_cert_reqs=ssl.CERT_REQUIRED,
    )
    server = uvicorn.Server(uvicorn_config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for(lambda: server.started)
    return RunningUvicorn(server, thread), authority


@dataclass
class RunningTraefik:
    process: subprocess.Popen[bytes]
    log_path: Path

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=5)


def _start_traefik(arguments: list[str], log_path: Path) -> RunningTraefik:
    if not TRAEFIK.is_file():
        pytest.skip("set TRAEFIK_BIN to the pinned Traefik 3.7.6 binary")
    version = subprocess.run([str(TRAEFIK), "version"], check=False, capture_output=True, text=True)
    assert version.returncode == 0
    assert re.search(r"^Version:\s+3\.7\.6$", version.stdout, re.MULTILINE), version.stdout
    log = log_path.open("ab")
    process = subprocess.Popen([str(TRAEFIK), *arguments], stdout=log, stderr=log)
    log.close()
    return RunningTraefik(process, log_path)


def _assert_process_alive(instance: RunningTraefik) -> bool:
    if instance.process.poll() is None:
        return True
    raise AssertionError(instance.log_path.read_text(encoding="utf-8"))


def _translate_cell_crds(
    cells: dict[str, tuple[list[dict[str, Any]], int]],
) -> dict[str, Any]:
    # This is intentionally a local projection of rendered CRDs into Traefik's
    # file provider. It exercises the rendered rules/services/middleware without
    # claiming Kubernetes reconciliation or CNI behavior.
    dynamic: dict[str, Any] = {"http": {"routers": {}, "middlewares": {}, "services": {}}}
    for namespace, (documents, port) in cells.items():
        route = _find(documents, "IngressRoute", f"{namespace}-transfer")
        control = _find(documents, "IngressRoute", f"{namespace}-control")
        middleware = _find(documents, "Middleware", f"{namespace}-strip-cell")
        service = _find(documents, "Service", namespace)
        assert route["spec"]["entryPoints"] == ["private-transfer"]
        assert route["spec"]["tls"] == {"options": {"name": "private-transfer@file"}}
        assert control["spec"]["entryPoints"] == ["web"]
        assert service["spec"]["ports"] == [
            {
                "name": "http",
                "port": 8765,
                "targetPort": "http",
                "protocol": "TCP",
            }
        ]
        rendered_route = route["spec"]["routes"]
        assert len(rendered_route) == 1
        rendered_service = rendered_route[0]["services"]
        assert rendered_service == [
            {
                "name": namespace,
                "port": 8765,
                "scheme": "https",
                "serversTransport": f"{namespace}-private-transfer@file",
            }
        ]
        middleware_name = rendered_route[0]["middlewares"][0]["name"]
        dynamic["http"]["middlewares"][middleware_name] = middleware["spec"]
        dynamic["http"]["routers"][namespace] = {
            "entryPoints": route["spec"]["entryPoints"],
            "rule": rendered_route[0]["match"],
            "middlewares": [middleware_name],
            "service": namespace,
            "tls": {"options": route["spec"]["tls"]["options"]["name"].removesuffix("@file")},
        }
        dynamic["http"]["services"][namespace] = {
            "loadBalancer": {
                "passHostHeader": True,
                "serversTransport": rendered_service[0]["serversTransport"].removesuffix("@file"),
                "servers": [{"url": f"https://127.0.0.1:{port}"}],
            }
        }
    return dynamic


def _private_arguments(deployment: dict[str, Any], port: int, provider_dir: Path) -> list[str]:
    args = _container(deployment, "exomem-platform-traefik")["args"]
    address = next(arg for arg in args if arg.startswith("--entryPoints.private-transfer.address="))
    tls = next(arg for arg in args if arg == "--entryPoints.private-transfer.http.tls=true")
    option = next(
        arg for arg in args if arg.startswith("--entryPoints.private-transfer.http.tls.options=")
    )
    provider = next(arg for arg in args if arg.startswith("--providers.file.directory="))
    assert address == "--entryPoints.private-transfer.address=:8444/tcp"
    assert option == "--entryPoints.private-transfer.http.tls.options=private-transfer@file"
    assert provider == "--providers.file.directory=/etc/traefik/dynamic"
    return [
        f"--entryPoints.private-transfer.address=127.0.0.1:{port}/tcp",
        tls,
        option,
        f"--providers.file.directory={provider_dir}",
        "--providers.file.watch=false",
        "--global.checknewversion=false",
        "--global.sendanonymoususage=false",
        "--log.level=ERROR",
    ]


def _public_configs(
    documents: list[dict[str, Any]],
    pod: MaterializedPod,
    *,
    port: int,
    gateway_port: int,
    private_port: int,
) -> tuple[Path, dict[str, Any]]:
    deployment = _find(documents, "Deployment", "exomem-direct-edge")
    argument = _container(deployment, "traefik")["args"]
    assert argument == ["--configFile=/etc/traefik/static/static.yaml"]
    configured_static = argument[0].split("=", 1)[1]
    static_path = pod.resolve(configured_static)
    static = yaml.safe_load(static_path.read_text(encoding="utf-8"))
    provider_filename = static["providers"]["file"]["filename"]
    dynamic_path = pod.resolve(provider_filename)
    dynamic = yaml.safe_load(dynamic_path.read_text(encoding="utf-8"))
    server_names = {
        name: transport["serverName"]
        for name, transport in dynamic["http"]["serversTransports"].items()
    }
    _map_service_socket(dynamic, "gateway", gateway_port)
    _map_service_socket(dynamic, "transfer", private_port)
    dynamic = _remap_mounted_paths(dynamic, pod)
    assert {
        name: transport["serverName"]
        for name, transport in dynamic["http"]["serversTransports"].items()
    } == server_names
    dynamic_path.write_text(yaml.safe_dump(dynamic), encoding="utf-8")
    static["entryPoints"]["direct"]["address"] = f"127.0.0.1:{port}"
    static["providers"]["file"]["filename"] = str(dynamic_path)
    static_path.write_text(yaml.safe_dump(static), encoding="utf-8")
    return static_path, dynamic


def _stream_until(port: int, path: str, ca_file: Path) -> tuple[ssl.SSLSocket, bytes]:
    context = ssl.create_default_context(cafile=str(ca_file))
    raw = socket.create_connection(("127.0.0.1", port), timeout=3)
    stream = context.wrap_socket(raw, server_hostname=TRANSFER_HOST)
    stream.settimeout(3)
    stream.sendall(
        f"GET {path} HTTP/1.1\r\nHost: {TRANSFER_HOST}\r\nConnection: close\r\n\r\n".encode()
    )
    response = b""
    while b"first" not in response:
        response += stream.recv(4096)
    return stream, response


def _await_request(*args: Any, **kwargs: Any) -> bytes:
    responses: list[bytes] = []
    attempts = 0
    last_error: OSError | None = None

    def ready() -> bool:
        nonlocal attempts, last_error
        attempts += 1
        try:
            responses.append(_request(*args, **kwargs))
            return True
        except OSError as error:
            last_error = error
            return False

    try:
        _wait_for(ready)
    except AssertionError as error:
        raise AssertionError(
            f"request did not complete after {attempts} attempts; last error: {last_error!r}"
        ) from error
    return responses[-1]


def _await_alpha_denial(
    port: int,
    ca_file: Path,
    *processes: RunningTraefik,
) -> None:
    attempts = 0
    last_observation = "no attempt"

    def ready() -> bool:
        nonlocal attempts, last_observation
        attempts += 1
        for process in processes:
            _assert_process_alive(process)
        try:
            response = _request(
                port,
                "/cells/cell-alpha/public/exomem/v2/transfers/upload",
                "PUT",
                ca_file=ca_file,
                headers={"Origin": transfer_v2.ORIGIN, "Content-Type": "text/plain"},
                body=b"alpha",
                timeout=0.75,
            )
        except OSError as error:
            last_observation = repr(error)
            return False
        status = _status(response)
        last_observation = f"HTTP {status}: {response[:200]!r}"
        return status == 401 and b"TRANSFER_GRANT_REJECTED" in response

    try:
        _wait_for(ready, interval=0.1)
    except AssertionError as error:
        raise AssertionError(
            f"alpha route did not become ready after {attempts} attempts; "
            f"last observation: {last_observation}"
        ) from error


def _await_tls_accepting(port: int, ca_file: Path, process: RunningTraefik) -> None:
    attempts = 0
    last_error: OSError | None = None

    def ready() -> bool:
        nonlocal attempts, last_error
        attempts += 1
        _assert_process_alive(process)
        try:
            context = ssl.create_default_context(cafile=str(ca_file))
            with socket.create_connection(("127.0.0.1", port), timeout=0.75) as raw:
                with context.wrap_socket(raw, server_hostname=TRANSFER_HOST):
                    return True
        except OSError as error:
            last_error = error
            return False

    try:
        _wait_for(ready, interval=0.1)
    except AssertionError as error:
        raise AssertionError(
            f"TLS listener did not become ready after {attempts} attempts; "
            f"last error: {last_error!r}"
        ) from error


def _start_edge_variant(
    root: Path,
    documents: list[dict[str, Any]],
    deployment: dict[str, Any],
    secret_payloads: dict[str, dict[str, bytes]],
    *,
    gateway_port: int,
    private_port: int,
    mutate: Callable[[dict[str, Any]], None] = lambda _dynamic: None,
) -> tuple[RunningTraefik, int]:
    pod = _materialize_pod(deployment, "traefik", documents, root / "pod", secret_payloads)
    port = _free_port()
    static_path, dynamic = _public_configs(
        documents,
        pod,
        port=port,
        gateway_port=gateway_port,
        private_port=private_port,
    )
    mutate(dynamic)
    static = yaml.safe_load(static_path.read_text(encoding="utf-8"))
    dynamic_path = Path(static["providers"]["file"]["filename"])
    dynamic_path.write_text(yaml.safe_dump(dynamic), encoding="utf-8")
    return (
        _start_traefik([f"--configFile={static_path}"], root / "traefik.log"),
        port,
    )


def _start_private_variant(
    root: Path,
    documents: list[dict[str, Any]],
    deployment: dict[str, Any],
    secret_payloads: dict[str, dict[str, bytes]],
    rendered_private_config: dict[str, Any],
    translated_crds: dict[str, Any],
    *,
    mutate: Callable[[dict[str, Any]], None] = lambda _dynamic: None,
) -> tuple[RunningTraefik, int]:
    pod = _materialize_pod(
        deployment,
        "exomem-platform-traefik",
        documents,
        root / "pod",
        secret_payloads,
    )
    provider_dir = pod.resolve("/etc/traefik/dynamic")
    dynamic = _remap_mounted_paths(copy.deepcopy(rendered_private_config), pod)
    mutate(dynamic)
    (provider_dir / "private-transfer.yaml").write_text(yaml.safe_dump(dynamic), encoding="utf-8")
    (provider_dir / "rendered-cell-routes.yaml").write_text(
        yaml.safe_dump(translated_crds), encoding="utf-8"
    )
    port = _free_port()
    return (
        _start_traefik(
            _private_arguments(deployment, port, provider_dir),
            root / "traefik.log",
        ),
        port,
    )


@dataclass
class SyntheticPKI:
    root: Path
    public_ca: tuple[object, x509.Certificate]
    backend_server_ca: tuple[object, x509.Certificate]
    edge_client_ca: tuple[object, x509.Certificate]
    cell_server_ca: tuple[object, x509.Certificate]
    private_client_ca: tuple[object, x509.Certificate]

    @property
    def public_ca_file(self) -> Path:
        return self.root / "public-ca/ca.crt"


def _synthetic_pki(root: Path, identities: dict[str, str]) -> SyntheticPKI:
    pki = SyntheticPKI(
        root=root,
        public_ca=_certificate(root / "public-ca", "ca", "public-test-ca"),
        backend_server_ca=_certificate(root / "backend-server-ca", "ca", "backend-server-ca"),
        edge_client_ca=_certificate(root / "edge-client-ca", "ca", "edge-client-ca"),
        cell_server_ca=_certificate(root / "cell-server-ca", "ca", "cell-server-ca"),
        private_client_ca=_certificate(root / "private-client-ca", "ca", "private-client-ca"),
    )
    _certificate(
        root / "public",
        "tls",
        TRANSFER_HOST,
        pki.public_ca,
        usage=ExtendedKeyUsageOID.SERVER_AUTH,
    )
    _certificate(
        root / "gateway",
        "tls",
        identities["gateway"],
        pki.backend_server_ca,
        usage=ExtendedKeyUsageOID.SERVER_AUTH,
    )
    _certificate(
        root / "private",
        "tls",
        identities["private"],
        pki.backend_server_ca,
        usage=ExtendedKeyUsageOID.SERVER_AUTH,
    )
    _certificate(
        root / "edge-client",
        "tls",
        "direct-edge-client",
        pki.edge_client_ca,
        usage=ExtendedKeyUsageOID.CLIENT_AUTH,
    )
    _certificate(
        root / "private-client",
        "tls",
        "private-router-client",
        pki.private_client_ca,
        usage=ExtendedKeyUsageOID.CLIENT_AUTH,
    )
    for cell in ("cell-alpha", "cell-beta"):
        _certificate(
            root / cell,
            "tls",
            identities[f"{cell}-private-transfer"],
            pki.cell_server_ca,
            usage=ExtendedKeyUsageOID.SERVER_AUTH,
        )
    return pki


def _secret_payloads(pki: SyntheticPKI) -> dict[str, dict[str, bytes]]:
    root = pki.root
    return {
        "exomem-direct-edge-public-tls": {
            "tls.crt": _pem(root / "public/tls.crt"),
            "tls.key": _pem(root / "public/tls.key"),
        },
        "exomem-direct-edge-backend-ca": {"ca.crt": _pem(root / "backend-server-ca/ca.crt")},
        "exomem-direct-edge-client-tls": {
            "tls.crt": _pem(root / "edge-client/tls.crt"),
            "tls.key": _pem(root / "edge-client/tls.key"),
        },
        "exomem-private-transfer-tls": {
            "tls.crt": _pem(root / "private/tls.crt"),
            "tls.key": _pem(root / "private/tls.key"),
        },
        "exomem-private-transfer-ca": {"ca.crt": _pem(root / "cell-server-ca/ca.crt")},
        "exomem-direct-edge-client-ca": {"ca.crt": _pem(root / "edge-client-ca/ca.crt")},
        "exomem-private-transfer-client-tls": {
            "tls.crt": _pem(root / "private-client/tls.crt"),
            "tls.key": _pem(root / "private-client/tls.key"),
        },
    }


def test_rendered_edge_and_private_router_reach_only_the_selected_tls_cell(
    tmp_path: Path,
) -> None:
    """CRDs are projected to file config; Kubernetes reconciliation/CNI are not exercised."""
    _, documents = _platform_render()
    edge_deployment = _find(documents, "Deployment", "exomem-direct-edge")
    private_deployment = _find(documents, "Deployment", "exomem-platform-traefik")
    edge_config = yaml.safe_load(
        _find(documents, "ConfigMap", "exomem-direct-edge-config")["data"]["dynamic.yaml"]
    )
    private_config = yaml.safe_load(
        _find(documents, "ConfigMap", "exomem-private-transfer-config")["data"][
            "private-transfer.yaml"
        ]
    )
    identities = {
        "gateway": edge_config["http"]["serversTransports"]["gateway"]["serverName"],
        "private": edge_config["http"]["serversTransports"]["transfer"]["serverName"],
        **{
            name: transport["serverName"]
            for name, transport in private_config["http"]["serversTransports"].items()
        },
    }
    assert identities == {
        "gateway": "exomem-gateway.exomem-platform.svc.cluster.local",
        "private": "exomem-platform-traefik.exomem-platform.svc.cluster.local",
        "cell-alpha-private-transfer": "cell-alpha.cell-alpha.svc.cluster.local",
        "cell-beta-private-transfer": "cell-beta.cell-beta.svc.cluster.local",
    }
    pki = _synthetic_pki(tmp_path / "certificates", identities)
    secrets = _secret_payloads(pki)

    gateway = _tls_backend(
        (pki.root / "gateway/tls.crt", pki.root / "gateway/tls.key"),
        pki.root / "edge-client-ca/ca.crt",
        body=b"gateway",
    )
    alpha, alpha_authority = _real_transfer_cell(
        tmp_path / "alpha-app",
        (pki.root / "cell-alpha/tls.crt", pki.root / "cell-alpha/tls.key"),
        pki.root / "private-client-ca/ca.crt",
    )
    beta = _tls_backend(
        (pki.root / "cell-beta/tls.crt", pki.root / "cell-beta/tls.key"),
        pki.root / "private-client-ca/ca.crt",
        body=b"beta",
        streaming=True,
    )
    private_proxy: RunningTraefik | None = None
    edge_proxy: RunningTraefik | None = None
    try:
        private_pod = _materialize_pod(
            private_deployment,
            "exomem-platform-traefik",
            documents,
            tmp_path / "private-pod",
            secrets,
        )
        provider_dir = private_pod.resolve("/etc/traefik/dynamic")
        mounted_private_config = _remap_mounted_paths(
            yaml.safe_load((provider_dir / "private-transfer.yaml").read_text(encoding="utf-8")),
            private_pod,
        )
        cells = {
            "cell-alpha": (_cell_render("cell-alpha"), alpha.port),
            "cell-beta": (
                _cell_render("cell-beta"),
                beta.server.server_port,
            ),
        }
        translated = _translate_cell_crds(cells)
        (provider_dir / "private-transfer.yaml").write_text(
            yaml.safe_dump(mounted_private_config), encoding="utf-8"
        )
        (provider_dir / "rendered-cell-routes.yaml").write_text(
            yaml.safe_dump(translated), encoding="utf-8"
        )
        private_port = _free_port()
        private_proxy = _start_traefik(
            _private_arguments(private_deployment, private_port, provider_dir),
            tmp_path / "private-traefik.log",
        )

        private_responses: list[bytes] = []

        def private_ready() -> bool:
            try:
                private_responses.append(
                    _request(
                        private_port,
                        "/cells/cell-alpha/public/exomem/v2/transfers/upload",
                        "PUT",
                        sni=identities["private"],
                        ca_file=pki.root / "backend-server-ca/ca.crt",
                        certificate=(
                            pki.root / "edge-client/tls.crt",
                            pki.root / "edge-client/tls.key",
                        ),
                        headers={
                            "Origin": transfer_v2.ORIGIN,
                            "Content-Type": "text/plain",
                        },
                        body=b"alpha",
                    )
                )
                return True
            except OSError:
                return False

        _wait_for(private_ready)
        assert _status(private_responses[-1]) == 401, private_responses[-1]
        edge_pod = _materialize_pod(
            edge_deployment,
            "traefik",
            documents,
            tmp_path / "edge-pod",
            secrets,
        )
        edge_port = _free_port()
        edge_static, _ = _public_configs(
            documents,
            edge_pod,
            port=edge_port,
            gateway_port=gateway.server.server_port,
            private_port=private_port,
        )
        edge_proxy = _start_traefik([f"--configFile={edge_static}"], tmp_path / "edge-traefik.log")

        def edge_ready() -> bool:
            try:
                return (
                    _status(
                        _request(
                            edge_port,
                            "/api/exomem/mcp/v1",
                            ca_file=pki.public_ca_file,
                        )
                    )
                    == 200
                )
            except OSError:
                return False

        _wait_for(edge_ready)
        gateway.seen.clear()
        alpha_authority.consume_calls.clear()

        spoofed_headers = {
            "Forwarded": "for=198.51.100.1",
            "X-Forwarded-For": "198.51.100.2",
            "X-Forwarded-Host": "evil.example",
            "X-Forwarded-Port": "9",
            "X-Forwarded-Proto": "http",
            "X-Real-IP": "198.51.100.3",
            "CF-Connecting-IP": "198.51.100.4",
            "CF-IPCountry": "ZZ",
            "CF-Pseudo-IPv4": "198.51.100.5",
            "CF-Ray": "spoof",
            "CF-Visitor": "spoof",
            "CF-Access-Authenticated-User-Email": "attacker@example.test",
            "CF-Access-Jwt-Assertion": "spoof",
            "True-Client-IP": "198.51.100.6",
            "X-Exomem-Gateway-Ingress": "shared-edge",
        }
        gateway_response = _request(
            edge_port,
            "/api/exomem/mcp/v1",
            ca_file=pki.public_ca_file,
            headers=spoofed_headers,
        )
        assert _status(gateway_response) == 200
        assert len(gateway.seen) == 1
        for name, supplied in spoofed_headers.items():
            assert gateway.seen[0].get(name.lower()) != supplied
        assert gateway.seen[0]["x-exomem-gateway-ingress"] == "direct-edge-v1"

        transfer_response = _request(
            edge_port,
            "/cells/cell-beta/public/exomem/v2/transfers/download",
            "OPTIONS",
            ca_file=pki.public_ca_file,
            headers=spoofed_headers,
        )
        assert _status(transfer_response) == 200
        assert len(beta.seen) == 1
        for name, supplied in spoofed_headers.items():
            assert beta.seen[0].get(name.lower()) != supplied
        assert "x-exomem-gateway-ingress" not in beta.seen[0]
        beta.seen.clear()

        assert (
            _status(
                _request(
                    edge_port,
                    "/.well-known/oauth-protected-resource/api/exomem/mcp/v1",
                    "HEAD",
                    ca_file=pki.public_ca_file,
                )
            )
            == 200
        )
        for path, method in (
            ("/cells/cell-alpha/private/exomem/v1", "POST"),
            ("/cells/cell-alpha/public/exomem/v2/transfers/upload/extra", "PUT"),
            ("/api%2fexomem/mcp/v1", "GET"),
            ("/api/exomem/mcp/v1", "PUT"),
            ("/healthz", "GET"),
        ):
            assert _status(_request(edge_port, path, method, ca_file=pki.public_ca_file)) != 200
        assert (
            _status(
                _request(
                    edge_port,
                    "/api/exomem/mcp/v1",
                    host="other.example.test",
                    sni=TRANSFER_HOST,
                    ca_file=pki.public_ca_file,
                )
            )
            != 200
        )
        with pytest.raises(ssl.SSLError):
            _request(
                edge_port,
                "/api/exomem/mcp/v1",
                sni="other.example.test",
                ca_file=pki.public_ca_file,
            )

        for host in (TRANSFER_HOST, identities["private"]):
            private_control = _request(
                private_port,
                "/cells/cell-alpha/private/exomem/v1",
                "POST",
                host=host,
                sni=identities["private"],
                ca_file=pki.root / "backend-server-ca/ca.crt",
                certificate=(
                    pki.root / "edge-client/tls.crt",
                    pki.root / "edge-client/tls.key",
                ),
            )
            assert _status(private_control) == 404

        denied = _request(
            edge_port,
            "/cells/cell-alpha/public/exomem/v2/transfers/upload",
            "PUT",
            ca_file=pki.public_ca_file,
            headers={
                "Origin": transfer_v2.ORIGIN,
                "Content-Type": "text/plain",
            },
            body=b"alpha",
        )
        assert _status(denied) == 401
        assert b"TRANSFER_GRANT_REJECTED" in denied
        assert b"alpha" not in denied.split(b"\r\n\r\n", 1)[-1]

        grant = transfer_v2._grant(jti=str(uuid.uuid4()), origin=transfer_v2.ORIGIN)
        accepted = _request(
            edge_port,
            "/cells/cell-alpha/public/exomem/v2/transfers/upload",
            "PUT",
            ca_file=pki.public_ca_file,
            headers={
                "Origin": transfer_v2.ORIGIN,
                "Content-Type": "text/plain",
                "X-Exomem-Transfer-Grant": grant,
            },
            body=b"alpha",
        )
        assert _status(accepted) == 201, accepted
        assert len(alpha_authority.consume_calls) == 1

        mismatched_grant = transfer_v2._grant(jti=str(uuid.uuid4()), origin=transfer_v2.ORIGIN)
        mismatched = _request(
            edge_port,
            "/cells/cell-alpha/public/exomem/v2/transfers/upload",
            "PUT",
            ca_file=pki.public_ca_file,
            headers={
                "Origin": transfer_v2.ORIGIN,
                "Content-Type": "text/plain",
                "X-Exomem-Transfer-Grant": mismatched_grant,
            },
            body=b"alpha-extra",
        )
        assert _status(mismatched) == 400
        assert b"TRANSFER_REQUEST_INVALID" in mismatched

        assert not beta.seen
        stream, prefix = _stream_until(
            edge_port,
            "/cells/cell-beta/public/exomem/v2/transfers/download",
            pki.public_ca_file,
        )
        assert b"first" in prefix
        assert beta.server.first_chunk.is_set()  # type: ignore[attr-defined]
        assert not beta.server.completed.is_set()  # type: ignore[attr-defined]
        stream.close()
        assert beta.server.cancelled.wait(3)  # type: ignore[attr-defined]
        assert beta.seen[-1][":path"] == "/public/exomem/v2/transfers/download"

        for namespace, (cell_documents, _) in cells.items():
            route = _find(cell_documents, "IngressRoute", f"{namespace}-transfer")
            assert route["spec"]["entryPoints"] == ["private-transfer"]
            assert (
                route["spec"]["routes"][0]["services"][0]["serversTransport"]
                == f"{namespace}-private-transfer@file"
            )
            assert _find(cell_documents, "IngressRoute", f"{namespace}-control")["spec"][
                "entryPoints"
            ] == ["web"]

        _assert_private_client_auth_failures(
            tmp_path,
            documents,
            edge_deployment,
            secrets,
            pki,
            gateway.server.server_port,
            private_port,
            beta,
        )
        _assert_wrong_backend_name(
            tmp_path,
            documents,
            edge_deployment,
            private_deployment,
            secrets,
            pki,
            private_config,
            translated,
            gateway.server.server_port,
            beta,
        )
        _assert_backend_server_certificate_failures(
            tmp_path,
            documents,
            edge_deployment,
            private_deployment,
            secrets,
            pki,
            private_config,
            translated,
            gateway.server.server_port,
            identities["cell-beta-private-transfer"],
        )
        _assert_rendered_limits_use_socket_source(
            tmp_path,
            documents,
            edge_deployment,
            secrets,
            pki,
            gateway,
            private_port,
        )
    finally:
        if edge_proxy:
            edge_proxy.close()
        if private_proxy:
            private_proxy.close()
        gateway.close()
        alpha.close()
        beta.close()


def _assert_private_client_auth_failures(
    tmp_path: Path,
    documents: list[dict[str, Any]],
    edge_deployment: dict[str, Any],
    valid_secrets: dict[str, dict[str, bytes]],
    pki: SyntheticPKI,
    gateway_port: int,
    private_port: int,
    beta: RunningHTTPServer,
) -> None:
    _certificate(
        pki.root / "untrusted-edge-client",
        "tls",
        "untrusted-edge-client",
        pki.cell_server_ca,
        usage=ExtendedKeyUsageOID.CLIENT_AUTH,
    )
    _certificate(
        pki.root / "expired-edge-client",
        "tls",
        "expired-edge-client",
        pki.edge_client_ca,
        usage=ExtendedKeyUsageOID.CLIENT_AUTH,
        expired=True,
    )
    cases: list[tuple[str, dict[str, dict[str, bytes]], Callable[[dict[str, Any]], None]]] = []
    cases.append(
        (
            "missing",
            valid_secrets,
            lambda dynamic: dynamic["http"]["serversTransports"]["transfer"].pop("certificates"),
        )
    )
    for label in ("untrusted", "expired"):
        directory = pki.root / f"{label}-edge-client"
        secrets = copy.deepcopy(valid_secrets)
        secrets["exomem-direct-edge-client-tls"] = {
            "tls.crt": _pem(directory / "tls.crt"),
            "tls.key": _pem(directory / "tls.key"),
        }
        cases.append((label, secrets, lambda _dynamic: None))

    before = len(beta.seen)
    for label, secrets, mutate in cases:
        proxy, port = _start_edge_variant(
            tmp_path / f"edge-client-{label}",
            documents,
            edge_deployment,
            secrets,
            gateway_port=gateway_port,
            private_port=private_port,
            mutate=mutate,
        )
        try:
            response = _await_request(
                port,
                "/cells/cell-beta/public/exomem/v2/transfers/download",
                ca_file=pki.public_ca_file,
                timeout=0.75,
            )
            assert _status(response) >= 500, (label, response[:200])
            assert _assert_process_alive(proxy)
            assert len(beta.seen) == before
        finally:
            proxy.close()


def _assert_wrong_backend_name(
    tmp_path: Path,
    documents: list[dict[str, Any]],
    edge_deployment: dict[str, Any],
    private_deployment: dict[str, Any],
    secrets: dict[str, dict[str, bytes]],
    pki: SyntheticPKI,
    rendered_private_config: dict[str, Any],
    translated_crds: dict[str, Any],
    gateway_port: int,
    beta: RunningHTTPServer,
) -> None:
    def wrong_name(dynamic: dict[str, Any]) -> None:
        dynamic["http"]["serversTransports"]["cell-beta-private-transfer"]["serverName"] = (
            "wrong.internal.test"
        )

    private_proxy, private_port = _start_private_variant(
        tmp_path / "wrong-backend-private",
        documents,
        private_deployment,
        secrets,
        rendered_private_config,
        translated_crds,
        mutate=wrong_name,
    )
    edge_proxy, edge_port = _start_edge_variant(
        tmp_path / "wrong-backend-edge",
        documents,
        edge_deployment,
        secrets,
        gateway_port=gateway_port,
        private_port=private_port,
    )
    before = len(beta.seen)
    try:
        _await_alpha_denial(
            edge_port,
            pki.public_ca_file,
            edge_proxy,
            private_proxy,
        )
        response = _await_request(
            edge_port,
            "/cells/cell-beta/public/exomem/v2/transfers/download",
            ca_file=pki.public_ca_file,
        )
        assert _status(response) >= 500
        assert len(beta.seen) == before
    finally:
        edge_proxy.close()
        private_proxy.close()


def _assert_backend_server_certificate_failures(
    tmp_path: Path,
    documents: list[dict[str, Any]],
    edge_deployment: dict[str, Any],
    private_deployment: dict[str, Any],
    secrets: dict[str, dict[str, bytes]],
    pki: SyntheticPKI,
    rendered_private_config: dict[str, Any],
    translated_crds: dict[str, Any],
    gateway_port: int,
    backend_identity: str,
) -> None:
    untrusted_ca = _certificate(
        pki.root / "untrusted-cell-server-ca", "ca", "untrusted-cell-server-ca"
    )
    _certificate(
        pki.root / "untrusted-cell-server",
        "tls",
        backend_identity,
        untrusted_ca,
        usage=ExtendedKeyUsageOID.SERVER_AUTH,
    )
    _certificate(
        pki.root / "expired-cell-server",
        "tls",
        backend_identity,
        pki.cell_server_ca,
        usage=ExtendedKeyUsageOID.SERVER_AUTH,
        expired=True,
    )

    for label in ("untrusted", "expired"):
        backend = _tls_backend(
            (
                pki.root / f"{label}-cell-server/tls.crt",
                pki.root / f"{label}-cell-server/tls.key",
            ),
            pki.root / "private-client-ca/ca.crt",
            body=b"must-not-arrive",
        )
        local_crds = copy.deepcopy(translated_crds)
        local_crds["http"]["services"]["cell-beta"]["loadBalancer"]["servers"] = [
            {"url": f"https://127.0.0.1:{backend.server.server_port}"}
        ]
        private_proxy, private_port = _start_private_variant(
            tmp_path / f"{label}-backend-private",
            documents,
            private_deployment,
            secrets,
            rendered_private_config,
            local_crds,
        )
        edge_proxy, edge_port = _start_edge_variant(
            tmp_path / f"{label}-backend-edge",
            documents,
            edge_deployment,
            secrets,
            gateway_port=gateway_port,
            private_port=private_port,
        )
        try:
            _await_alpha_denial(
                edge_port,
                pki.public_ca_file,
                edge_proxy,
                private_proxy,
            )
            response = _await_request(
                edge_port,
                "/cells/cell-beta/public/exomem/v2/transfers/download",
                ca_file=pki.public_ca_file,
            )
            assert _status(response) >= 500, (label, response[:200])
            assert not backend.seen
        finally:
            edge_proxy.close()
            private_proxy.close()
            backend.close()


def _assert_rendered_limits_use_socket_source(
    tmp_path: Path,
    documents: list[dict[str, Any]],
    edge_deployment: dict[str, Any],
    secrets: dict[str, dict[str, bytes]],
    pki: SyntheticPKI,
    gateway: RunningHTTPServer,
    private_port: int,
) -> None:
    rendered = yaml.safe_load(
        _find(documents, "ConfigMap", "exomem-direct-edge-config")["data"]["dynamic.yaml"]
    )
    assert rendered["http"]["middlewares"]["direct-rate"] == {
        "rateLimit": {"average": 30, "burst": 30}
    }
    assert rendered["http"]["middlewares"]["direct-inflight"] == {"inFlightReq": {"amount": 16}}
    assert not any("buffer" in name.lower() for name in rendered["http"]["middlewares"])

    rate_proxy, rate_port = _start_edge_variant(
        tmp_path / "rate-edge",
        documents,
        edge_deployment,
        secrets,
        gateway_port=gateway.server.server_port,
        private_port=private_port,
        mutate=lambda dynamic: dynamic["http"]["middlewares"]["direct-rate"]["rateLimit"].update(
            average=1, burst=1
        ),
    )
    try:
        _await_tls_accepting(rate_port, pki.public_ca_file, rate_proxy)
        context = ssl.create_default_context(cafile=str(pki.public_ca_file))
        with socket.create_connection(("127.0.0.1", rate_port), timeout=3) as raw:
            with context.wrap_socket(raw, server_hostname=TRANSFER_HOST) as stream:
                stream.settimeout(5)
                requests = []
                # The exact production values are asserted above. Reducing only
                # average/burst makes the source-key behavior deterministic; the
                # sourceCriterion and middleware order remain rendered values.
                for index in range(2):
                    connection = "close" if index == 1 else "keep-alive"
                    requests.append(
                        "GET /api/exomem/mcp/v1 HTTP/1.1\r\n"
                        f"Host: {TRANSFER_HOST}\r\n"
                        f"X-Forwarded-For: 198.51.100.{index + 1}\r\n"
                        f"Connection: {connection}\r\n\r\n"
                    )
                stream.sendall("".join(requests).encode())
                response = b""
                while chunk := stream.recv(65536):
                    response += chunk
        accepted = len(re.findall(rb"HTTP/1\.1 200", response))
        limited = len(re.findall(rb"HTTP/1\.1 429", response))
        assert (accepted, limited) == (1, 1)
    finally:
        rate_proxy.close()

    inflight_proxy, inflight_port = _start_edge_variant(
        tmp_path / "inflight-edge",
        documents,
        edge_deployment,
        secrets,
        gateway_port=gateway.server.server_port,
        private_port=private_port,
    )
    blocked_statuses: list[int] = []
    blocked_threads: list[threading.Thread] = []
    gateway.seen.clear()

    def blocked(index: int) -> None:
        response = _request(
            inflight_port,
            "/api/exomem/mcp/v1",
            ca_file=pki.public_ca_file,
            headers={
                "X-Test-Block": "1",
                "X-Forwarded-For": f"203.0.113.{index + 1}",
            },
        )
        blocked_statuses.append(_status(response))

    try:
        _await_tls_accepting(inflight_port, pki.public_ca_file, inflight_proxy)
        blocked_threads = [
            threading.Thread(target=blocked, args=(index,), daemon=True) for index in range(16)
        ]
        for thread in blocked_threads:
            thread.start()
        _wait_for(
            lambda: (
                len([headers for headers in gateway.seen if headers.get("x-test-block") == "1"])
                == 16
            ),
            timeout=10,
        )
        seventeenth = _request(
            inflight_port,
            "/api/exomem/mcp/v1",
            ca_file=pki.public_ca_file,
            headers={"X-Forwarded-For": "192.0.2.17"},
        )
        assert _status(seventeenth) == 429
    finally:
        gateway.server.block_release.set()  # type: ignore[attr-defined]
        for thread in blocked_threads:
            thread.join(timeout=5)
        inflight_proxy.close()
    assert blocked_statuses == [200] * 16


def test_chart_mounts_network_policy_and_disabled_default_match_rendered_objects(
    tmp_path: Path,
) -> None:
    lock = yaml.safe_load((PLATFORM / "Chart.lock").read_text(encoding="utf-8"))
    assert [
        dependency["version"]
        for dependency in lock["dependencies"]
        if dependency["name"] == "traefik"
    ] == ["41.0.2"]

    default, default_documents = _render(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace=PLATFORM_NAMESPACE,
        release_name="exomem-platform",
    )
    explicit_off, _ = _render(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace=PLATFORM_NAMESPACE,
        release_name="exomem-platform",
        extra_args=("--set", "directEdge.enabled=false"),
    )
    assert explicit_off == default
    assert not any(
        document.get("metadata", {}).get("name") == "exomem-direct-edge"
        for document in default_documents
    )

    gateway_args = (
        "--set",
        "gateway.enabled=true",
        "--set",
        "gateway.image=ghcr.io/substrate-systems/substrate-gateway@sha256:" + "a" * 64,
        "--set",
        "gateway.originHostname=origin.example.test",
        "--set-json",
        'gateway.databaseEgressCidrs=["192.0.2.1/32"]',
    )
    _, documents = _platform_render(*gateway_args)
    edge = _find(documents, "Deployment", "exomem-direct-edge")
    private = _find(documents, "Deployment", "exomem-platform-traefik")
    gateway = _find(documents, "Deployment", "exomem-gateway")
    edge_service = _find(documents, "Service", "exomem-direct-edge")
    private_service = _find(documents, "Service", "exomem-platform-traefik")
    policy = _find(documents, "NetworkPolicy", "exomem-direct-edge")
    direct_data = _find(documents, "ConfigMap", "exomem-direct-edge-config")["data"]
    private_data = _find(documents, "ConfigMap", "exomem-private-transfer-config")["data"]

    assert edge_service["spec"] == {
        "type": "ClusterIP",
        "selector": {"app.kubernetes.io/name": "exomem-direct-edge"},
        "ports": [{"name": "https", "port": 8443, "targetPort": "https"}],
    }
    assert edge["spec"]["strategy"] == {"type": "Recreate"}
    assert edge["spec"]["replicas"] == 1
    edge_container = _container(edge, "traefik")
    assert edge_container["image"] == (
        "docker.io/traefik@sha256:21a3d83696379bac6434bb32e1dde0aff0e84ef2abd053ed3db87d3f45e749b2"
    )
    assert edge_container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert "hostNetwork" not in edge["spec"]["template"]["spec"]
    assert "hostPort" not in yaml.safe_dump(edge)
    assert private_service["spec"]["type"] == "ClusterIP"
    private_8444 = [port for port in private_service["spec"]["ports"] if port["port"] == 8444]
    assert private_8444 == [
        {
            "name": "private-transfe",
            "port": 8444,
            "targetPort": "private-transfe",
            "protocol": "TCP",
        }
    ]

    private_args = _container(private, "exomem-platform-traefik")["args"]
    assert "--providers.kubernetescrd.crossProviderNamespaces=cell-alpha,cell-beta" in private_args
    assert (
        "--providers.kubernetescrd.namespaces=exomem-platform,cell-alpha,cell-beta" in private_args
    )
    assert "--providers.kubernetescrd.allowCrossNamespace=true" not in private_args
    assert "--entryPoints.private-transfer.http.tls.options=private-transfer@file" in private_args
    assert "--providers.file.directory=/etc/traefik/dynamic" in private_args

    placeholder_secrets = {
        volume["secret"]["secretName"]: {
            "ca.crt": b"test",
            "tls.crt": b"test",
            "tls.key": b"test",
        }
        for deployment in (edge, private)
        for volume in deployment["spec"]["template"]["spec"]["volumes"]
        if "secret" in volume
    }
    direct_static = yaml.safe_load(direct_data["static.yaml"])
    direct_dynamic = yaml.safe_load(direct_data["dynamic.yaml"])
    private_dynamic = yaml.safe_load(private_data["private-transfer.yaml"])
    assert direct_static["providers"] == {
        "file": {
            "filename": "/etc/traefik/static/dynamic.yaml",
            "watch": True,
        }
    }
    assert "kubernetesCRD" not in yaml.safe_dump(direct_static)
    assert "kubernetesIngress" not in yaml.safe_dump(direct_static)
    assert "api@internal" not in yaml.safe_dump(direct_dynamic)
    assert "accessLog" not in yaml.safe_dump(direct_static)
    assert direct_static["entryPoints"]["direct"]["http"]["sanitizePath"] is False
    assert direct_dynamic["http"]["routers"]["mcp"]["rule"] == (
        "Host(`transfer.example.test`) && Path(`/api/exomem/mcp/v1`) && "
        "(Method(`GET`) || Method(`POST`) || Method(`DELETE`))"
    )
    assert direct_dynamic["http"]["routers"]["transfer-upload"]["rule"] == (
        "Host(`transfer.example.test`) && "
        "PathRegexp(`^/cells/[A-Za-z0-9][A-Za-z0-9_-]{0,127}/public/"
        "exomem/v2/transfers/upload$`) && (Method(`PUT`) || Method(`OPTIONS`))"
    )
    assert direct_dynamic["http"]["services"]["gateway"]["loadBalancer"]["servers"] == [
        {"url": "https://exomem-gateway.exomem-platform.svc.cluster.local:8443"}
    ]
    assert direct_dynamic["http"]["services"]["transfer"]["loadBalancer"]["servers"] == [
        {"url": "https://exomem-platform-traefik.exomem-platform.svc.cluster.local:8444"}
    ]
    edge_pod = _materialize_pod(edge, "traefik", documents, tmp_path / "edge", placeholder_secrets)
    private_pod = _materialize_pod(
        private,
        "exomem-platform-traefik",
        documents,
        tmp_path / "private",
        placeholder_secrets,
    )
    configured_static = edge_container["args"][0].split("=", 1)[1]
    assert edge_pod.resolve(configured_static).is_file()
    assert edge_pod.resolve(direct_static["providers"]["file"]["filename"]).is_file()
    for configured_path in _configured_file_paths(direct_dynamic):
        assert "secret" in edge_pod.source_for(configured_path)
        assert edge_pod.resolve(configured_path).is_file()
    for configured_path in _configured_file_paths(private_dynamic):
        assert "secret" in private_pod.source_for(configured_path)
        assert private_pod.resolve(configured_path).is_file()

    secret_by_mount = {
        mount["mountPath"]: next(
            volume["secret"]["secretName"]
            for volume in private["spec"]["template"]["spec"]["volumes"]
            if volume["name"] == mount["name"]
        )
        for mount in _container(private, "exomem-platform-traefik")["volumeMounts"]
        if "secret"
        in next(
            volume
            for volume in private["spec"]["template"]["spec"]["volumes"]
            if volume["name"] == mount["name"]
        )
    }
    direct_values = yaml.safe_load((PLATFORM / "values.yaml").read_text(encoding="utf-8"))[
        "directEdge"
    ]
    assert secret_by_mount == {
        "/var/run/exomem-private-transfer/tls": direct_values["privateTransferTlsSecretName"],
        "/var/run/exomem-private-transfer/ca": direct_values["privateTransferCaSecretName"],
        "/var/run/exomem-private-transfer/edge-client-ca": direct_values[
            "privateTransferEdgeClientCaSecretName"
        ],
        "/var/run/exomem-private-transfer/client": direct_values["privateTransferClientSecretName"],
    }
    assert private_dynamic["tls"]["options"]["private-transfer"]["clientAuth"] == {
        "caFiles": ["/var/run/exomem-private-transfer/edge-client-ca/ca.crt"],
        "clientAuthType": "RequireAndVerifyClientCert",
    }

    edge_labels = edge["spec"]["template"]["metadata"]["labels"]
    gateway_labels = gateway["spec"]["template"]["metadata"]["labels"]
    private_labels = private["spec"]["template"]["metadata"]["labels"]
    assert policy["spec"]["podSelector"]["matchLabels"].items() <= edge_labels.items()
    expected_egress = [
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": PLATFORM_NAMESPACE}
                    },
                    "podSelector": {"matchLabels": {"app.kubernetes.io/name": "exomem-gateway"}},
                }
            ],
            "ports": [{"port": 8443, "protocol": "TCP"}],
        },
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": PLATFORM_NAMESPACE}
                    },
                    "podSelector": {"matchLabels": {"exomem.io/ingress": "traefik"}},
                }
            ],
            "ports": [{"port": 8444, "protocol": "TCP"}],
        },
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                    },
                    "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                }
            ],
            "ports": [
                {"port": 53, "protocol": "UDP"},
                {"port": 53, "protocol": "TCP"},
            ],
        },
    ]
    assert policy["spec"]["egress"] == expected_egress
    assert (
        expected_egress[0]["to"][0]["podSelector"]["matchLabels"].items() <= gateway_labels.items()
    )
    assert (
        expected_egress[1]["to"][0]["podSelector"]["matchLabels"].items() <= private_labels.items()
    )

    for namespace in ("cell-alpha", "cell-beta"):
        cell_documents = _cell_render(namespace)
        workload_labels = _find(cell_documents, "StatefulSet", namespace)["spec"]["template"][
            "metadata"
        ]["labels"]
        ingress = _find(cell_documents, "NetworkPolicy", f"{namespace}-traefik-ingress")["spec"]
        assert ingress["podSelector"]["matchLabels"].items() <= workload_labels.items()
        traefik_rule = ingress["ingress"][0]
        assert traefik_rule == {
            "from": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": PLATFORM_NAMESPACE}
                    },
                    "podSelector": {
                        "matchLabels": {
                            "app.kubernetes.io/name": "traefik",
                            "exomem.io/ingress": "traefik",
                        }
                    },
                }
            ],
            "ports": [{"protocol": "TCP", "port": 8765}],
        }
        assert (
            traefik_rule["from"][0]["podSelector"]["matchLabels"].items() <= private_labels.items()
        )

    _, legacy_cell = _render(
        CELL,
        CELL / "values.validation.yaml",
        namespace="cell-alpha",
        release_name="cell-alpha",
        extra_args=("--set", "routes.enabled=true"),
    )
    legacy_transfer = _find(legacy_cell, "IngressRoute", "cell-alpha-transfer")
    legacy_control = _find(legacy_cell, "IngressRoute", "cell-alpha-control")
    assert legacy_transfer["spec"]["entryPoints"] == ["web"]
    assert "tls" not in legacy_transfer["spec"]
    assert legacy_transfer["spec"]["routes"][0]["services"] == [
        {"name": "cell-alpha", "port": 8765}
    ]
    assert legacy_control["spec"]["entryPoints"] == ["web"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            (
                "--set-json",
                'directEdge.privateTransferBackends=[{"transportName":"same","serverName":"cell-alpha.cell-alpha.svc.cluster.local"},{"transportName":"same","serverName":"cell-beta.cell-beta.svc.cluster.local"}]',
            ),
            "map each admitted namespace exactly once with unique transport names",
        ),
        (
            (
                "--set-json",
                'directEdge.privateTransferBackends=[{"transportName":"alpha","serverName":"cell-alpha.cell-alpha.svc.cluster.local"},{"transportName":"beta","serverName":"other.cell-alpha.svc.cluster.local"}]',
            ),
            "map each admitted namespace exactly once with unique transport names",
        ),
        (
            ("--set", "traefik.ports.private-transfer.port=9444"),
            "requires private-transfer TLS on the internal ClusterIP port 8444",
        ),
        (
            ("--set", "traefik.service.spec.type=LoadBalancer"),
            "requires private-transfer TLS on the internal ClusterIP port 8444",
        ),
        (
            (
                "--set",
                "traefik.ports.private-transfer.http.tls.options=default@file",
            ),
            "requires private-transfer TLS on the internal ClusterIP port 8444",
        ),
        (
            (
                "--set-json",
                'traefik.providers.kubernetesCRD.namespaces=["cell-alpha","cell-beta"]',
            ),
            "requires kubernetesCRD.namespaces to contain exactly the platform namespace and admitted cell namespaces",
        ),
        (
            (
                "--set-json",
                'traefik.providers.kubernetesCRD.namespaces=["exomem-platform","cell-alpha","cell-beta","cell-gamma"]',
            ),
            "requires kubernetesCRD.namespaces to contain exactly the platform namespace and admitted cell namespaces",
        ),
        (
            (
                "--set-json",
                'traefik.providers.kubernetesCRD.namespaces=["exomem-platform","cell-alpha","cell-beta","cell-beta"]',
            ),
            "requires kubernetesCRD.namespaces to contain exactly the platform namespace and admitted cell namespaces",
        ),
        (
            (
                "--set",
                "directEdge.privateTransferEdgeClientCaSecretName=exomem-private-transfer-ca",
            ),
            "requires separate edge-client and cell-server CA Secrets",
        ),
    ],
    ids=[
        "duplicate-transport",
        "duplicate-namespace",
        "wrong-port",
        "public-service",
        "wrong-tls-option",
        "missing-platform-watch-namespace",
        "extra-watch-namespace",
        "duplicate-watch-namespace",
        "shared-client-server-ca",
    ],
)
def test_enabled_render_rejects_incoherent_private_listener_or_backend_mapping(
    arguments: tuple[str, ...], message: str
) -> None:
    result = _helm_template(
        PLATFORM,
        PLATFORM / "values.validation.yaml",
        namespace=PLATFORM_NAMESPACE,
        overlays=(PLATFORM / "values.direct-edge.yaml",),
        extra_args=arguments,
        release_name="exomem-platform",
    )
    assert result.returncode != 0
    assert message in result.stderr
