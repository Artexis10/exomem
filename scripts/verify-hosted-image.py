#!/usr/bin/env python3
"""Exercise the hosted image's real operator, transfer, and restart contract."""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import pathlib
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from exomem import __version__, hosted_gateway, hosted_transfer
from exomem.hosted_runtime import HostedCellConfig, HostedResourceLimits

ORIGIN = "https://transfer-client.example"
TRANSFER_HOST = "transfer.example"
CELL_ID = "cell-image-drill"
VAULT_ID = "vault-image-drill"
CREDENTIAL_VERSION = "credential-v1"
CREDENTIAL = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()
PRINCIPAL = (
    base64.urlsafe_b64encode(hashlib.sha256(b"image-drill-principal").digest())
    .rstrip(b"=")
    .decode()
)


def _run(arguments: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, check=True, **kwargs)


def _free_port() -> int:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = int(listener.getsockname()[1])
    listener.close()
    return port


def _grant(data: bytes, filename: str) -> str:
    now = int(time.time())
    metadata = {
        "category": "documents",
        "content_type": "application/octet-stream",
        "description": None,
        "filename": filename,
        "scope": "image-drill",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }
    target = {
        "kind": "upload-v1",
        "metadata": metadata,
        "metadata_sha256": hashlib.sha256(hosted_transfer.canonical_json(metadata)).hexdigest(),
    }
    return hosted_transfer.mint_transfer_grant_v2(
        signing_credential=CREDENTIAL,
        kid=CREDENTIAL_VERSION,
        origin=ORIGIN,
        operation="upload",
        cell_id=CELL_ID,
        principal_scope=PRINCIPAL,
        jti=str(uuid.uuid4()),
        max_bytes=len(data),
        target=target,
        issued_at=now,
        not_before=now,
        expires_at=now + 300,
    )


class _Body:
    def __init__(self, data: bytes, *, slow: bool) -> None:
        self.data = data
        self.offset = 0
        self.slow = slow

    def read(self, requested: int = -1) -> bytes:
        if self.offset >= len(self.data):
            return b""
        size = min(
            256 * 1024,
            len(self.data) - self.offset,
            requested if requested >= 0 else 256 * 1024,
        )
        value = self.data[self.offset : self.offset + size]
        self.offset += size
        if self.slow:
            time.sleep(0.003)
        return value


def _upload(
    port: int,
    data: bytes,
    grant: str,
    *,
    chunked: bool = False,
    slow: bool = False,
) -> tuple[int, dict[str, Any]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=180)
    headers = {
        "Host": TRANSFER_HOST,
        "Origin": ORIGIN,
        hosted_transfer.TRANSFER_GRANT_HEADER: grant,
        "Content-Type": "application/octet-stream",
    }
    if not chunked:
        headers["Content-Length"] = str(len(data))
    connection.request(
        "PUT",
        hosted_transfer.TRANSFER_UPLOAD_PATH,
        body=_Body(data, slow=slow),
        headers=headers,
        encode_chunked=chunked,
    )
    response = connection.getresponse()
    payload = json.loads(response.read())
    status = response.status
    connection.close()
    return status, payload


def _create_fixture(image: str, root: pathlib.Path) -> None:
    request = {
        "request_id": "77777777-7777-4777-8777-777777777777",
        "operation_id": "image-drill-init-v1",
        "cell_id": CELL_ID,
        "vault_id": VAULT_ID,
        "vault_root": "/mnt/vault-pvc/vault",
        "state_root": "/mnt/state-pvc/state",
        "log_root": "/mnt/log-pvc/logs",
        "expected_release": __version__,
        "expected_protocol": "1",
        "runtime_uid": 10001,
        "runtime_gid": 10001,
        "active_credential_version": CREDENTIAL_VERSION,
    }
    bundle = (
        json.dumps(
            {"schema_version": 1, "credentials": {CREDENTIAL_VERSION: CREDENTIAL}},
            separators=(",", ":"),
        )
        + "\n"
    )
    request_document = json.dumps(request, separators=(",", ":")) + "\n"
    setup = f"""\
import os, pathlib
root = pathlib.Path('/fixture')
for parent, child in [('vault-pvc', 'vault'), ('state-pvc', 'state'), ('log-pvc', 'logs')]:
    directory = root / parent
    directory.mkdir()
    os.chown(directory, 10001, 10001)
    os.chmod(directory, 0o700)
    child_directory = directory / child
    child_directory.mkdir()
    os.chown(child_directory, 10001, 10001)
    os.chmod(child_directory, 0o700)
secret = root / 'credentials'
secret.mkdir()
generation = secret / '..2026_07_14_00_00_00.000000001'
generation.mkdir()
credential_file = generation / 'credentials.json'
credential_file.write_text({bundle!r})
os.chown(credential_file, 0, 0)
os.chmod(credential_file, 0o444)
(secret / '..data').symlink_to(generation.name)
(secret / 'credentials.json').symlink_to('..data/credentials.json')
request_file = root / 'init.json'
request_file.write_text({request_document!r})
os.chown(request_file, 0, 0)
os.chmod(request_file, 0o444)
"""
    _run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "0:0",
            "-v",
            f"{root}:/fixture",
            "--entrypoint",
            "python",
            image,
            "-c",
            setup,
        ]
    )


def _mounts(root: pathlib.Path) -> list[str]:
    return [
        "-v",
        f"{root}/vault-pvc:/mnt/vault-pvc",
        "-v",
        f"{root}/state-pvc:/mnt/state-pvc",
        "-v",
        f"{root}/log-pvc:/mnt/log-pvc",
        "-v",
        f"{root}/credentials:/run/exomem/credentials:ro",
    ]


def _initialize(image: str, root: pathlib.Path) -> None:
    result = _run(
        [
            "docker",
            "run",
            "--rm",
            "--read-only",
            *_mounts(root),
            "-v",
            f"{root}/init.json:/run/exomem/operator-requests/init.json:ro",
            image,
            "hosted",
            "init",
            "--contract-version",
            "1",
            "--request-file",
            "/run/exomem/operator-requests/init.json",
        ],
        text=True,
        capture_output=True,
    )
    envelope = json.loads(result.stdout)
    if not envelope.get("ok") or envelope.get("code") != "HOSTED_CELL_INITIALIZED":
        raise RuntimeError(result.stdout)


def _start(image: str, root: pathlib.Path, name: str, port: int) -> None:
    environment = {
        "EXOMEM_HOSTED_CELL": "true",
        "EXOMEM_HOSTED_CELL_ID": CELL_ID,
        "EXOMEM_HOSTED_VAULT_ID": VAULT_ID,
        "EXOMEM_VAULT_PATH": "/mnt/vault-pvc/vault",
        "EXOMEM_HOSTED_STATE_ROOT": "/mnt/state-pvc/state",
        "EXOMEM_LOG_DIR": "/mnt/log-pvc/logs",
        "EXOMEM_HOSTED_RUNTIME_UID": "10001",
        "EXOMEM_HOSTED_RUNTIME_GID": "10001",
        "EXOMEM_HOSTED_PROTOCOL_VERSION": "1",
        "EXOMEM_HOSTED_WORKER_POLICY_DIGEST": hashlib.sha256(b"image-drill-policy").hexdigest(),
        "EXOMEM_HOSTED_FEATURE_GRANTS": "media,diarization",
        "EXOMEM_HOSTED_WORKER_LIMIT": "0",
        "EXOMEM_HOSTED_TRANSFER_BROWSER_ORIGIN": ORIGIN,
        "EXOMEM_HOSTED_TRANSFER_HOST": TRANSFER_HOST,
    }
    arguments = [
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "--read-only",
        "-p",
        f"127.0.0.1:{port}:8765",
        *_mounts(root),
    ]
    for key, value in environment.items():
        arguments.extend(("-e", f"{key}={value}"))
    arguments.extend((image, "--transport", "http", "--host", "0.0.0.0", "--port", "8765"))
    _run(arguments, stdout=subprocess.DEVNULL)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            connection.request(
                "OPTIONS",
                hosted_transfer.TRANSFER_UPLOAD_PATH,
                headers={
                    "Host": TRANSFER_HOST,
                    "Origin": ORIGIN,
                    "Access-Control-Request-Method": "PUT",
                    "Access-Control-Request-Headers": ("Content-Type, X-Exomem-Transfer-Grant"),
                    "Content-Length": "0",
                },
            )
            status = connection.getresponse().status
            connection.close()
            if status == 204:
                return
        except OSError:
            pass
        time.sleep(0.15)
    logs = subprocess.run(["docker", "logs", name], text=True, capture_output=True, check=False)
    raise RuntimeError(logs.stdout + logs.stderr)


def _container_value(name: str, expression: str) -> str:
    return _run(
        ["docker", "exec", name, "python", "-c", expression],
        text=True,
        capture_output=True,
    ).stdout.strip()


def _cleanup(image: str, root: pathlib.Path, name: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "0:0",
            "-v",
            f"{root}:/fixture",
            "--entrypoint",
            "python",
            image,
            "-c",
            'import shutil; shutil.rmtree("/fixture", ignore_errors=True)',
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    shutil.rmtree(root, ignore_errors=True)


def _legacy_environment(deadline: str) -> dict[str, str]:
    return {
        "EXOMEM_HOSTED_CELL": "true",
        "EXOMEM_HOSTED_CELL_ID": "cell-v1-image-drill",
        "EXOMEM_VAULT_PATH": "/mnt/vault-pvc/vault",
        "EXOMEM_HOSTED_STATE_ROOT": "/mnt/state-pvc/state",
        "EXOMEM_LOG_DIR": "/mnt/log-pvc/logs",
        "EXOMEM_HOSTED_SERVICE_CREDENTIAL": CREDENTIAL,
        "EXOMEM_HOSTED_RUNTIME_UID": "10001",
        "EXOMEM_HOSTED_RUNTIME_GID": "10001",
        "EXOMEM_HOSTED_PROTOCOL_VERSION": "1",
        "EXOMEM_HOSTED_FEATURE_GRANTS": "media,diarization",
        "EXOMEM_HOSTED_WORKER_LIMIT": "0",
        "EXOMEM_HOSTED_TRANSFER_BROWSER_ORIGIN": ORIGIN,
        "EXOMEM_HOSTED_TRANSFER_HOST": TRANSFER_HOST,
        "EXOMEM_HOSTED_TRANSFER_V1_COMPAT_UNTIL": deadline,
    }


def _legacy_fixture(image: str, root: pathlib.Path) -> None:
    setup = """\
import os, pathlib
root = pathlib.Path('/fixture')
for parent, child in [('vault-pvc', 'vault'), ('state-pvc', 'state'), ('log-pvc', 'logs')]:
    directory = root / parent
    directory.mkdir()
    os.chown(directory, 10001, 10001)
    os.chmod(directory, 0o700)
    child_directory = directory / child
    child_directory.mkdir()
    os.chown(child_directory, 10001, 10001)
    os.chmod(child_directory, 0o700)
"""
    _run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "0:0",
            "-v",
            f"{root}:/fixture",
            "--entrypoint",
            "python",
            image,
            "-c",
            setup,
        ]
    )


def _image_v1_deadline(image: str) -> str:
    raw = _run(
        ["docker", "inspect", image, "--format", "{{json .Config.Env}}"],
        text=True,
        capture_output=True,
    ).stdout
    environment = dict(value.split("=", 1) for value in json.loads(raw) if "=" in value)
    build = datetime.fromisoformat(environment["EXOMEM_RELEASE_BUILD_TIME"].replace("Z", "+00:00"))
    now = datetime.now(UTC)
    deadline = min(build + timedelta(days=7), now + timedelta(days=1))
    if deadline <= now:
        raise RuntimeError("hosted image is outside its immutable v1 compatibility window")
    return deadline.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _legacy_mounts(root: pathlib.Path) -> list[str]:
    return [
        "-v",
        f"{root}/vault-pvc:/mnt/vault-pvc",
        "-v",
        f"{root}/state-pvc:/mnt/state-pvc",
        "-v",
        f"{root}/log-pvc:/mnt/log-pvc",
    ]


def _legacy_start(
    image: str,
    root: pathlib.Path,
    name: str,
    port: int,
    deadline: str,
) -> None:
    arguments = [
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "--read-only",
        "-p",
        f"127.0.0.1:{port}:8765",
        *_legacy_mounts(root),
    ]
    for key, value in _legacy_environment(deadline).items():
        arguments.extend(("-e", f"{key}={value}"))
    arguments.extend((image, "--transport", "http", "--host", "0.0.0.0", "--port", "8765"))
    _run(arguments, stdout=subprocess.DEVNULL)
    expiration = time.time() + 20
    while time.time() < expiration:
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            connection.request(
                "GET",
                "/private/exomem/v1/live",
                headers={
                    "Authorization": f"Bearer {CREDENTIAL}",
                    hosted_gateway.CELL_HEADER: "cell-v1-image-drill",
                    hosted_gateway.PROTOCOL_HEADER: "1",
                    hosted_gateway.REQUEST_HEADER: str(uuid.uuid4()),
                    hosted_gateway.PRINCIPAL_HEADER: PRINCIPAL,
                },
            )
            response = connection.getresponse()
            response.read()
            connection.close()
            if response.status == 200:
                return
        except OSError:
            pass
        time.sleep(0.15)
    logs = subprocess.run(["docker", "logs", name], text=True, capture_output=True, check=False)
    raise RuntimeError(logs.stdout + logs.stderr)


def _legacy_grant(maximum_bytes: int, identity: str) -> str:
    config = HostedCellConfig(
        cell_id="cell-v1-image-drill",
        vault_root=pathlib.Path("/mnt/vault-pvc/vault"),
        state_root=pathlib.Path("/mnt/state-pvc/state"),
        log_root=pathlib.Path("/mnt/log-pvc/logs"),
        service_credential=CREDENTIAL,
        enforce_transfer_v1_compatibility=False,
        transfer_browser_origin=ORIGIN,
        transfer_host=TRANSFER_HOST,
        resource_limits=HostedResourceLimits(
            storage_bytes=5 * 1024 * 1024 * 1024,
            upload_bytes=90 * 1024 * 1024,
            worker_count=0,
        ),
    )
    return hosted_gateway.mint_transfer_grant(
        config,
        tenant_scope="tenant-image-drill",
        principal_scope=PRINCIPAL,
        operation="upload",
        jti=identity,
        max_bytes=maximum_bytes,
    )


def _multipart(data: bytes, boundary: str) -> bytes:
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="scope"\r\n\r\nimage-drill\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="category"\r\n\r\ndocuments\r\n'.encode(),
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            'filename="v1-crash.bin"\r\nContent-Type: application/octet-stream\r\n\r\n'
        ).encode(),
        data,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts)


def _legacy_headers(grant: str, identity: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {CREDENTIAL}",
        hosted_gateway.CELL_HEADER: "cell-v1-image-drill",
        hosted_gateway.PROTOCOL_HEADER: "1",
        hosted_gateway.REQUEST_HEADER: str(uuid.uuid4()),
        hosted_gateway.PRINCIPAL_HEADER: PRINCIPAL,
        hosted_gateway.TRANSFER_GRANT_HEADER: grant,
        "Idempotency-Key": identity,
    }


def verify_v1_restart(image: str) -> dict[str, Any]:
    root = pathlib.Path(tempfile.mkdtemp(prefix="exomem-hosted-v1-image-"))
    name = "exomem-hosted-v1-drill-" + uuid.uuid4().hex[:8]
    try:
        deadline = _image_v1_deadline(image)
        _legacy_fixture(image, root)
        provision = [
            "docker",
            "run",
            "--rm",
            "--read-only",
            *_legacy_mounts(root),
        ]
        for key, value in _legacy_environment(deadline).items():
            provision.extend(("-e", f"{key}={value}"))
        provision.extend(
            (
                "--entrypoint",
                "python",
                image,
                "-c",
                "from exomem.hosted_runtime import HostedCellConfig, provision_hosted_cell; "
                "print(provision_hosted_cell(HostedCellConfig.from_env()).status)",
            )
        )
        result = _run(provision, text=True, capture_output=True)
        if result.stdout.strip() != "provisioned":
            raise RuntimeError(result.stdout)
        port = _free_port()
        _legacy_start(image, root, name, port, deadline)

        data = b"V" * (3 * 1024 * 1024)
        boundary = "exomem-image-drill-boundary"
        body = _multipart(data, boundary)
        identity = "v1-crash-upload"
        grant = _legacy_grant(len(data), identity)
        headers = _legacy_headers(grant, identity)
        connection = socket.create_connection(("127.0.0.1", port))
        request_headers = [
            "POST /private/exomem/v1/upload HTTP/1.1",
            f"Host: 127.0.0.1:{port}",
            *(f"{key}: {value}" for key, value in headers.items()),
            f"Content-Type: multipart/form-data; boundary={boundary}",
            f"Content-Length: {len(body)}",
            "",
            "",
        ]
        connection.sendall("\r\n".join(request_headers).encode() + body[:-16])
        time.sleep(0.5)
        _run(
            [
                "docker",
                "exec",
                name,
                "python",
                "-c",
                "from pathlib import Path; "
                "Path('/mnt/state-pvc/state/tmp/runtime/diarizer-stale.tmp').write_bytes(b'stale')",
            ]
        )
        _run(["docker", "kill", name], stdout=subprocess.DEVNULL)
        connection.close()
        _run(["docker", "rm", name], stdout=subprocess.DEVNULL)
        _legacy_start(image, root, name, port, deadline)
        remaining = _container_value(
            name,
            "import pathlib; p=pathlib.Path('/mnt/state-pvc/state/tmp/runtime'); "
            "print(len(list(p.iterdir())))",
        )
        if remaining != "0":
            raise RuntimeError(f"v1 runtime cleanup failed: {remaining}")

        successful_data = b"v1-image-drill"
        successful_boundary = "exomem-image-drill-success"
        successful_body = _multipart(successful_data, successful_boundary)
        successful_identity = "v1-success-upload"
        successful_grant = _legacy_grant(len(successful_data), successful_identity)
        successful_headers = _legacy_headers(successful_grant, successful_identity)
        successful_headers.update(
            {
                "Content-Type": f"multipart/form-data; boundary={successful_boundary}",
                "Content-Length": str(len(successful_body)),
            }
        )
        http_connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        http_connection.request(
            "POST",
            "/private/exomem/v1/upload",
            body=successful_body,
            headers=successful_headers,
        )
        response = http_connection.getresponse()
        response_payload = json.loads(response.read())
        status = response.status
        http_connection.close()
        if status != 201:
            raise RuntimeError(f"v1 upload failed: {status} {response_payload}")
        return {
            "v1_deadline": deadline,
            "v1_partial_multipart_bytes": len(body) - 16,
            "v1_restart_runtime_temp_entries": int(remaining),
            "v1_upload_status": status,
        }
    finally:
        _cleanup(image, root, name)


def verify(image: str) -> dict[str, Any]:
    root = pathlib.Path(tempfile.mkdtemp(prefix="exomem-hosted-image-"))
    name = "exomem-hosted-image-drill-" + uuid.uuid4().hex[:8]
    try:
        _create_fixture(image, root)
        _initialize(image, root)
        port = _free_port()
        _start(image, root, name, port)

        large = b"L" * (90 * 1024 * 1024)
        large_result: list[tuple[int, dict[str, Any]]] = []
        large_thread = threading.Thread(
            target=lambda: large_result.append(
                _upload(port, large, _grant(large, "large-90m.bin"), slow=True)
            )
        )
        large_thread.start()
        peak_temp = 0
        while large_thread.is_alive():
            measured = _container_value(
                name,
                "import pathlib; p=pathlib.Path('/mnt/state-pvc/state/tmp/transfers-v2'); "
                "print(max([x.stat().st_size for x in p.iterdir()]+[0]))",
            )
            peak_temp = max(peak_temp, int(measured))
            time.sleep(0.05)
        large_thread.join()
        if not large_result or large_result[0][0] != 201:
            raise RuntimeError(f"large upload failed: {large_result!r}")
        if peak_temp > hosted_transfer.TRANSFER_TEMP_QUOTA_BYTES:
            raise RuntimeError("v2 transfer temp exceeded its quota")

        aborted = b"A" * (5 * 1024 * 1024)
        aborted_grant = _grant(aborted, "aborted.bin")
        connection = socket.create_connection(("127.0.0.1", port))
        headers = (
            f"PUT {hosted_transfer.TRANSFER_UPLOAD_PATH} HTTP/1.1\r\n"
            f"Host: {TRANSFER_HOST}\r\n"
            f"Origin: {ORIGIN}\r\n"
            f"{hosted_transfer.TRANSFER_GRANT_HEADER}: {aborted_grant}\r\n"
            "Content-Type: application/octet-stream\r\n"
            f"Content-Length: {len(aborted)}\r\n\r\n"
        ).encode()
        connection.sendall(headers + aborted[: 1024 * 1024])
        connection.close()
        time.sleep(0.5)
        replay = _upload(port, aborted, aborted_grant)
        if replay[0] == 201 or replay[1]["error"]["requires_new_grant"] is not True:
            raise RuntimeError(f"aborted grant was not burned: {replay!r}")

        crashing = b"C" * (8 * 1024 * 1024)
        crash_errors: list[OSError] = []

        def crashing_upload() -> None:
            try:
                _upload(port, crashing, _grant(crashing, "crash.bin"), slow=True)
            except OSError as exc:
                crash_errors.append(exc)

        crash_thread = threading.Thread(target=crashing_upload)
        crash_thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            if (
                _container_value(
                    name,
                    "import pathlib; p=pathlib.Path('/mnt/state-pvc/state/tmp/transfers-v2'); "
                    "print(any(p.iterdir()))",
                )
                == "True"
            ):
                break
            time.sleep(0.05)
        _run(
            [
                "docker",
                "exec",
                name,
                "python",
                "-c",
                "from pathlib import Path; "
                "Path('/mnt/state-pvc/state/tmp/runtime/diarizer-stale.tmp').write_bytes(b'stale')",
            ]
        )
        _run(["docker", "kill", name], stdout=subprocess.DEVNULL)
        crash_thread.join(timeout=10)
        if crash_thread.is_alive() or not crash_errors:
            raise RuntimeError("crash upload did not observe container termination")
        _run(["docker", "rm", name], stdout=subprocess.DEVNULL)
        _start(image, root, name, port)
        temp_counts = _container_value(
            name,
            "import pathlib; a=pathlib.Path('/mnt/state-pvc/state/tmp/runtime'); "
            "b=pathlib.Path('/mnt/state-pvc/state/tmp/transfers-v2'); "
            "print(len(list(a.iterdir())), len(list(b.iterdir())))",
        )
        if temp_counts != "0 0":
            raise RuntimeError(f"restart cleanup failed: {temp_counts}")

        chunked = b"K" * (5 * 1024 * 1024)
        chunked_result = _upload(port, chunked, _grant(chunked, "chunked-5m.bin"), chunked=True)
        if chunked_result[0] != 201:
            raise RuntimeError(f"chunked upload failed: {chunked_result!r}")
        committed = _container_value(
            name,
            "import hashlib,pathlib; root=pathlib.Path('/mnt/vault-pvc/vault'); "
            "print(sorted((p.name,p.stat().st_size,hashlib.sha256(p.read_bytes()).hexdigest()) "
            "for p in root.rglob('*.bin')))",
        )
        return {
            "aborted_replay_code": replay[1]["error"]["code"],
            "chunked_status": chunked_result[0],
            "committed_files": committed,
            "init": "HOSTED_CELL_INITIALIZED",
            "large_peak_temp_bytes": peak_temp,
            "large_status": large_result[0][0],
            "read_only_root": True,
            "restart_temp_counts": temp_counts,
        }
    finally:
        _cleanup(image, root, name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="exomem:hosted-runtime-contract")
    args = parser.parse_args()
    result = verify(args.image)
    result.update(verify_v1_restart(args.image))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
