#!/usr/bin/env python3
"""Validate and optionally probe the immutable hosted cross-repository release unit."""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_IMAGE = re.compile(r"^ghcr\.io/artexis10/exomem@sha256:[0-9a-f]{64}$")
_RELEASE_KEYS = {
    "artifact",
    "schemaVersion",
    "sourceRepository",
    "sourceCommit",
    "release",
    "hostedProtocol",
    "releaseBuildTime",
    "runtimeImage",
    "publishedTag",
    "operatorContractSha256",
    "gatewayContractSha256",
    "commandRegistry",
}
_GATE_BINDINGS = (
    "sourceRepository",
    "sourceCommit",
    "release",
    "hostedProtocol",
    "releaseBuildTime",
    "operatorContractSha256",
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _registry_from_fixture(fixture: dict[str, Any]) -> list[dict[str, object]]:
    commands = fixture.get("commands")
    if not isinstance(commands, list) or not commands:
        raise ValueError("gateway fixture has no command registry")
    registry: list[dict[str, object]] = []
    seen: set[str] = set()
    for command in commands:
        if not isinstance(command, dict):
            raise ValueError("gateway fixture command is not an object")
        values = [
            command.get("name"),
            command.get("read_only"),
            command.get("mode"),
            command.get("tier"),
            command.get("capability"),
        ]
        name, read_only, mode, tier, capability = values
        if (
            not isinstance(name, str)
            or not name
            or name in seen
            or type(read_only) is not bool
            or mode not in {"read", "write"}
            or type(tier) is not int
            or tier < 1
            or not isinstance(capability, str)
            or not capability
            or read_only != (mode == "read")
        ):
            raise ValueError("gateway fixture command registry is invalid")
        seen.add(name)
        registry.append(
            {
                "name": name,
                "readOnly": read_only,
                "mode": mode,
                "tier": tier,
                "capability": capability,
            }
        )
    return registry


def _validate_registry(registry: object) -> None:
    if not isinstance(registry, list) or not registry:
        raise ValueError("release command registry is empty")
    fixture: dict[str, list[dict[str, object]]] = {"commands": []}
    for row in registry:
        if not isinstance(row, dict) or set(row) != {
            "name",
            "readOnly",
            "mode",
            "tier",
            "capability",
        }:
            raise ValueError("release command registry row is invalid")
        fixture["commands"].append(
            {
                "name": row["name"],
                "read_only": row["readOnly"],
                "mode": row["mode"],
                "tier": row["tier"],
                "capability": row["capability"],
            }
        )
    if _registry_from_fixture(fixture) != registry:
        raise ValueError("release command registry is not canonical")


def validate_release_manifest(release: dict[str, Any], gate: dict[str, Any]) -> None:
    """Reject mutable images, partial overrides, and incoherent release bindings."""

    if set(release) != _RELEASE_KEYS:
        raise ValueError("release manifest fields are incomplete or unknown")
    if release.get("artifact") != "exomem-hosted-release" or release.get("schemaVersion") != 1:
        raise ValueError("unsupported hosted release manifest")
    for key in _GATE_BINDINGS:
        if release.get(key) != gate.get(key):
            raise ValueError(f"release {key} does not match the reviewed runtime gate")
    source_commit = release.get("sourceCommit")
    runtime_image = release.get("runtimeImage")
    published_tag = release.get("publishedTag")
    if not isinstance(source_commit, str) or not _COMMIT.fullmatch(source_commit):
        raise ValueError("release source commit is not exact")
    if not isinstance(runtime_image, str) or not _IMAGE.fullmatch(runtime_image):
        raise ValueError("release runtime image is not an immutable approved digest")
    if published_tag != f"ghcr.io/artexis10/exomem:{source_commit}-hosted":
        raise ValueError("release publication tag is not bound to the source commit")
    for key in ("operatorContractSha256", "gatewayContractSha256"):
        value = release.get(key)
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise ValueError(f"release {key} is not a SHA-256 digest")
    _validate_registry(release.get("commandRegistry"))


def validate_gateway_fixture(release: dict[str, Any], fixture: dict[str, Any]) -> None:
    """Validate the complete generated Substrate fixture against the release unit."""

    if fixture.get("schema_version") != 1:
        raise ValueError("gateway fixture schema is unsupported")
    if fixture.get("exomem_release") != release.get("release"):
        raise ValueError("gateway fixture release drift")
    if fixture.get("protocol_version") != release.get("hostedProtocol"):
        raise ValueError("gateway fixture protocol drift")
    digest = fixture.get("digest")
    expected_digest = release.get("gatewayContractSha256")
    if digest != {"algorithm": "sha256", "value": expected_digest}:
        raise ValueError("gateway fixture declared digest drift")
    semantic = copy.deepcopy(fixture)
    semantic.pop("digest", None)
    if hashlib.sha256(_canonical(semantic)).hexdigest() != expected_digest:
        raise ValueError("gateway fixture semantic digest drift")
    if _registry_from_fixture(fixture) != release.get("commandRegistry"):
        raise ValueError("gateway fixture command registry drift")


def validate_image_provenance(release: dict[str, Any], provenance: dict[str, Any]) -> None:
    """Bind the selected registry artifact to its reviewed VCS input and build target."""

    try:
        args = provenance["SLSA"]["buildDefinition"]["externalParameters"]["request"]["root"][
            "request"
        ]["args"]
    except (KeyError, TypeError) as error:
        raise ValueError("published runtime provenance is incomplete") from error
    expected = {
        "build-arg:EXOMEM_RELEASE_BUILD_TIME": release.get("releaseBuildTime"),
        "target": "hosted",
        "vcs:revision": release.get("sourceCommit"),
        "vcs:source": release.get("sourceRepository"),
    }
    if not isinstance(args, dict) or any(args.get(key) != value for key, value in expected.items()):
        raise ValueError("published runtime provenance differs from the release unit")


def _run(command: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({command[0]} {command[1] if len(command) > 1 else ''}): "
            + result.stdout
            + result.stderr
        )
    return result


def _docker_mount(source: Path, target: str, *, read_only: bool = False) -> str:
    suffix = ",readonly" if read_only else ""
    return f"type=bind,source={source},target={target}{suffix}"


def _prepare_runtime_tree(image: str, root: Path, release: dict[str, Any], credential: str) -> None:
    for name in ("vault", "state", "logs", "requests", "credentials"):
        (root / name).mkdir(mode=0o700)
    request = {
        "request_id": str(uuid.uuid4()),
        "operation_id": "release-verification-1",
        "cell_id": "release-verification-cell",
        "vault_id": "release-verification-vault",
        "vault_root": "/var/lib/exomem/vault",
        "state_root": "/var/lib/exomem/state",
        "log_root": "/var/lib/exomem/logs",
        "expected_release": release["release"],
        "expected_protocol": release["hostedProtocol"],
        "runtime_uid": 10001,
        "runtime_gid": 10001,
        "active_credential_version": "active-v1",
    }
    (root / "requests/init.json").write_text(
        json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    credential_generation = root / "credentials/..release-verification"
    credential_generation.mkdir(mode=0o700)
    (credential_generation / "credentials.json").write_text(
        json.dumps(
            {"schema_version": 1, "credentials": {"active-v1": credential}},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "credentials/..data").symlink_to(credential_generation.name)
    (root / "credentials/credentials.json").symlink_to("..data/credentials.json")
    _run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "0:0",
            "--entrypoint",
            "/bin/sh",
            "--mount",
            _docker_mount(root, "/work"),
            image,
            "-euc",
            "chown -R 10001:10001 /work/vault /work/state /work/logs; "
            "chmod 0700 /work/vault /work/state /work/logs; "
            "chown -R 0:0 /work/requests /work/credentials; "
            "chmod 0555 /work/requests /work/credentials "
            "/work/credentials/..release-verification; "
            "chmod 0444 /work/requests/init.json "
            "/work/credentials/..release-verification/credentials.json",
        ]
    )


def _runtime_mounts(root: Path) -> list[str]:
    arguments: list[str] = []
    for source, target, read_only in (
        (root / "vault", "/var/lib/exomem/vault", False),
        (root / "state", "/var/lib/exomem/state", False),
        (root / "logs", "/var/lib/exomem/logs", False),
        (root / "credentials", "/run/exomem/credentials", True),
    ):
        arguments += ["--mount", _docker_mount(source, target, read_only=read_only)]
    return arguments


def _reclaim_runtime_tree(image: str, root: Path) -> None:
    _run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "0:0",
            "--entrypoint",
            "/bin/sh",
            "--mount",
            _docker_mount(root, "/work"),
            image,
            "-euc",
            f"chown -R {os.getuid()}:{os.getgid()} /work; chmod -R u+rwX /work",
        ]
    )


def _runtime_environment(release: dict[str, Any]) -> list[str]:
    values = {
        "EXOMEM_HOSTED_CELL": "1",
        "EXOMEM_HOSTED_CELL_ID": "release-verification-cell",
        "EXOMEM_HOSTED_VAULT_ID": "release-verification-vault",
        "EXOMEM_HOSTED_RUNTIME_UID": "10001",
        "EXOMEM_HOSTED_RUNTIME_GID": "10001",
        "EXOMEM_VAULT_PATH": "/var/lib/exomem/vault",
        "EXOMEM_HOSTED_STATE_ROOT": "/var/lib/exomem/state",
        "EXOMEM_LOG_DIR": "/var/lib/exomem/logs",
        "TMPDIR": "/var/lib/exomem/state/tmp/runtime",
        "HOME": "/var/lib/exomem/state/home",
        "EXOMEM_HOSTED_PROTOCOL_VERSION": str(release["hostedProtocol"]),
        "EXOMEM_HOSTED_EXPECTED_RELEASE": str(release["release"]),
        "EXOMEM_HOSTED_WORKER_POLICY_DIGEST": "b" * 64,
        "EXOMEM_HOSTED_STORAGE_LIMIT_BYTES": str(5 * 1024 * 1024 * 1024),
        "EXOMEM_HOSTED_UPLOAD_LIMIT_BYTES": str(90 * 1024 * 1024),
        "EXOMEM_HOSTED_WORKER_LIMIT": "0",
        "EXOMEM_HOSTED_FEATURE_GRANTS": "",
        "EXOMEM_HOSTED_TRANSFER_BROWSER_ORIGIN": "https://substratesystems.io",
        "EXOMEM_HOSTED_TRANSFER_HOST": "transfer.release.invalid",
    }
    arguments: list[str] = []
    for name, value in values.items():
        arguments += ["--env", f"{name}={value}"]
    return arguments


def _probe_contract(
    port: int,
    credential: str,
    *,
    protocol: str,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    principal_bytes = base64.urlsafe_b64encode(
        hashlib.sha256(b"release-verifier").digest()
    )
    principal = principal_bytes.rstrip(b"=").decode("ascii")
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/private/exomem/v1/contract",
            headers={
                "Authorization": f"Bearer {credential}",
                "X-Exomem-Cell-Id": "release-verification-cell",
                "X-Exomem-Protocol-Version": protocol,
                "X-Exomem-Request-Id": str(uuid.uuid4()),
                "X-Exomem-Principal-Scope": principal,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:  # noqa: S310
                if (
                    response.status == 200
                    and response.headers.get_content_type() == "application/json"
                ):
                    value = json.load(response)
                    if isinstance(value, dict):
                        return value
        except (OSError, urllib.error.HTTPError, urllib.error.URLError) as error:
            last_error = error
        time.sleep(0.5)
    raise RuntimeError(f"hosted release contract route did not become ready: {last_error}")


def probe_published_runtime(release: dict[str, Any], fixture: dict[str, Any]) -> None:
    """Pull the published digest and compare its real route byte-semantically."""

    image = str(release["runtimeImage"])
    published_tag = str(release["publishedTag"])
    _run(["docker", "pull", published_tag], timeout=900)
    tag_inspection = json.loads(_run(["docker", "image", "inspect", published_tag]).stdout)[0]
    if image not in tag_inspection.get("RepoDigests", []):
        raise ValueError("published source tag does not resolve to the release image digest")
    _run(["docker", "pull", image], timeout=900)
    inspection = json.loads(_run(["docker", "image", "inspect", image]).stdout)[0]
    if image not in inspection.get("RepoDigests", []):
        raise ValueError("pulled runtime image did not retain the selected registry digest")
    provenance = json.loads(
        _run(
            [
                "docker",
                "buildx",
                "imagetools",
                "inspect",
                image,
                "--format",
                "{{json .Provenance}}",
            ],
            timeout=900,
        ).stdout
    )
    validate_image_provenance(release, provenance)
    probe_runtime_contract(image, release, fixture)


def probe_runtime_contract(image: str, release: dict[str, Any], fixture: dict[str, Any]) -> None:
    """Initialize one selected image and compare its authenticated contract route."""

    inspection = json.loads(_run(["docker", "image", "inspect", image]).stdout)[0]
    config = inspection.get("Config", {})
    if config.get("User") != "10001:10001" or config.get("Entrypoint") != ["exomem"]:
        raise ValueError("published runtime image identity/entrypoint drift")
    environment = {
        item.split("=", 1)[0]: item.split("=", 1)[1]
        for item in config.get("Env", [])
        if isinstance(item, str) and "=" in item
    }
    if environment.get("EXOMEM_CONTAINER_VARIANT") != "hosted" or environment.get(
        "EXOMEM_RELEASE_BUILD_TIME"
    ) != release.get("releaseBuildTime"):
        raise ValueError("published runtime image variant/build-time drift")

    credential = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    container = f"exomem-release-verify-{uuid.uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory(prefix="exomem-release-") as temporary:
        root = Path(temporary)
        try:
            _prepare_runtime_tree(image, root, release, credential)
            init_command = [
                "docker",
                "run",
                "--rm",
                "--read-only",
                "--user",
                "0:0",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "CHOWN",
                "--cap-add",
                "DAC_OVERRIDE",
                "--cap-add",
                "FOWNER",
                "--security-opt",
                "no-new-privileges",
                "--mount",
                _docker_mount(root, "/var/lib/exomem"),
                "--mount",
                _docker_mount(root / "credentials", "/run/exomem/credentials", read_only=True),
                "--mount",
                _docker_mount(root / "requests", "/run/exomem/operator-requests", read_only=True),
                image,
                "hosted",
                "init",
                "--contract-version",
                "1",
                "--request-file",
                "/run/exomem/operator-requests/init.json",
            ]
            init = json.loads(_run(init_command).stdout)
            if init.get("ok") is not True or init.get("code") != "HOSTED_CELL_INITIALIZED":
                raise ValueError("published runtime initializer returned an invalid proof")

            try:
                _run(
                    [
                        "docker",
                        "run",
                        "--detach",
                        "--rm",
                        "--read-only",
                        "--cap-drop",
                        "ALL",
                        "--security-opt",
                        "no-new-privileges",
                        "--user",
                        "10001:10001",
                        "--name",
                        container,
                        "--publish",
                        "127.0.0.1::8765",
                        *_runtime_mounts(root),
                        *_runtime_environment(release),
                        image,
                        "--transport",
                        "http",
                        "--port",
                        "8765",
                    ]
                )
                port_output = _run(["docker", "port", container, "8765/tcp"]).stdout.strip()
                port = int(port_output.rsplit(":", 1)[1])
                observed = _probe_contract(
                    port,
                    credential,
                    protocol=str(release["hostedProtocol"]),
                )
                if observed != fixture:
                    raise ValueError(
                        "published runtime /contract response differs from Substrate fixture"
                    )
                validate_gateway_fixture(release, observed)
            finally:
                subprocess.run(
                    ["docker", "rm", "--force", container],
                    text=True,
                    capture_output=True,
                    check=False,
                )
        finally:
            _reclaim_runtime_tree(image, root)


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--runtime-gate", type=Path, required=True)
    parser.add_argument("--substrate-fixture", type=Path, required=True)
    parser.add_argument("--probe-image", action="store_true")
    args = parser.parse_args(argv)

    release = _load(args.manifest)
    gate = _load(args.runtime_gate)
    fixture = _load(args.substrate_fixture)
    validate_release_manifest(release, gate)
    validate_gateway_fixture(release, fixture)
    if args.probe_image:
        if not shutil_which("docker"):
            raise SystemExit("docker is required for --probe-image")
        probe_published_runtime(release, fixture)
    print("hosted release verified")
    return 0


def shutil_which(command: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / command
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


if __name__ == "__main__":
    raise SystemExit(main())
