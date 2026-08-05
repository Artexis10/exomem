#!/usr/bin/env python3
"""Validate and optionally probe the immutable hosted cross-repository release unit."""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import importlib.util
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_IMAGE = re.compile(r"^ghcr\.io/artexis10/exomem@sha256:[0-9a-f]{64}$")
_SUBSTRATE_REPOSITORY = "https://github.com/substrate-systems/substrate"
_SUBSTRATE_COMMIT = "d451153469718fc113abef085044a09001942b8d"
_SUBSTRATE_FIXTURE_PATH = "src/lib/exomem-hosted/__tests__/gateway-contract-0-22-0.json"
_SUBSTRATE_SELECTION_KEYS = {
    "artifact",
    "schemaVersion",
    "sourceRepository",
    "sourceCommit",
    "fixturePath",
    "fixtureSha256",
    "gatewayContractSha256",
}
_MAX_FIXTURE_BYTES = 1_048_576
_MAX_LOCK_EVIDENCE_FILES = 32
_MAX_RUNTIME_CANDIDATE_ASSETS = 32
_MAX_PROVISIONER_REFERRERS = 32
_MAX_PULL_FILES = 3
_OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
_OCI_EMPTY_CONFIG_MEDIA_TYPE = "application/vnd.oci.empty.v1+json"
_OCI_SUBJECT_MEDIA_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
_CANDIDATE_MEDIA_TYPE = "application/vnd.exomem.hosted-image-candidate.v1+json"
_CANDIDATE_BUNDLE_MEDIA_TYPE = "application/vnd.dev.sigstore.bundle.v0.3+json"
_MAX_OCI_CONFIG_BYTES = 1_024
_MAX_OCI_ATTACHMENT_BYTES = 128 * 1024 + 16 * 1024 * 1024 + _MAX_OCI_CONFIG_BYTES
_MAX_DISCOVERED_OCI_BYTES = 64 * 1024 * 1024
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_CANONICAL_DEPLOYMENT_LOCK_PAIR = _REPOSITORY_ROOT / "infra/contracts/exomem-hosted-deployment-lock-pair-v2.json"
_CANONICAL_DEPLOYMENT_LOCK_EVIDENCE = _REPOSITORY_ROOT / "infra/contracts/exomem-hosted-deployment-lock-evidence-v2"
_FROZEN_V1_CORPUS = _REPOSITORY_ROOT / "infra/provisioner/tests/fixtures/provisioner-wire-v1.json"
_FROZEN_V1_CORPUS_SHA256 = "ced714a5aa204a837e22cab831262cc0ae4766e44720b2896e61b8c157ddd3b5"
_SUBSTRATE_FIXTURE_SHA256 = "ba3c211377616ba87877947ba7392ffa66e9769a9f631027a141ce5cccc40054"
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


def validate_substrate_selection(release: dict[str, Any], selection: dict[str, Any]) -> None:
    """Require one reviewed Substrate commit and byte-exact generated fixture."""

    if set(selection) != _SUBSTRATE_SELECTION_KEYS:
        raise ValueError("Substrate gateway selection fields are incomplete or unknown")
    if (
        selection.get("artifact") != "exomem-hosted-substrate-gateway-selection"
        or type(selection.get("schemaVersion")) is not int
        or selection.get("schemaVersion") != 1
    ):
        raise ValueError("Substrate gateway selection identity is invalid")
    if selection.get("sourceRepository") != _SUBSTRATE_REPOSITORY:
        raise ValueError("Substrate gateway selection repository is not canonical")
    source_commit = selection.get("sourceCommit")
    if (
        not isinstance(source_commit, str)
        or not _COMMIT.fullmatch(source_commit)
        or source_commit != _SUBSTRATE_COMMIT
    ):
        raise ValueError("Substrate gateway selection commit is not the reviewed commit")
    if selection.get("fixturePath") != _SUBSTRATE_FIXTURE_PATH:
        raise ValueError("Substrate gateway selection path is not canonical")
    fixture_digest = selection.get("fixtureSha256")
    if (
        not isinstance(fixture_digest, str)
        or not _SHA256.fullmatch(fixture_digest)
        or fixture_digest != _SUBSTRATE_FIXTURE_SHA256
    ):
        raise ValueError("Substrate gateway fixture byte digest is not reviewed")
    if selection.get("gatewayContractSha256") != release.get("gatewayContractSha256"):
        raise ValueError("Substrate gateway selection contract digest drift")


def validate_selected_gateway_fixture(
    release: dict[str, Any],
    selection: dict[str, Any],
    fixture: dict[str, Any],
    raw_fixture: bytes,
) -> None:
    """Bind the semantic gateway contract to the exact reviewed fixture bytes."""

    validate_substrate_selection(release, selection)
    if hashlib.sha256(raw_fixture).hexdigest() != selection["fixtureSha256"]:
        raise ValueError("Substrate gateway fixture byte digest drift")
    validate_gateway_fixture(release, fixture)


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
    uid = getattr(os, "getuid", lambda: 0)()
    gid = getattr(os, "getgid", lambda: 0)()
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
            f"chown -R {uid}:{gid} /work; chmod -R u+rwX /work",
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
        "EXOMEM_HOSTED_WORKER_LIMIT": "2",
        "EXOMEM_HOSTED_FEATURE_GRANTS": "embeddings,file-watcher",
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
    principal_bytes = base64.urlsafe_b64encode(hashlib.sha256(b"release-verifier").digest())
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


def _verify_offline_embedding_capability(image: str, environment: dict[str, str]) -> None:
    """Prove the published image can embed with no network and no writable root.

    That is exactly the condition a cell runs under: a default-deny NetworkPolicy
    with no egress rules and `readOnlyRootFilesystem: true`. A hosted image built
    without the embedding runtime or without baked weights still starts, still
    reports ready, and still answers queries — it just never matches on meaning.
    Nothing else in this verification would notice, so run the real thing behind
    `--network none` and require a vector back.
    """
    if environment.get("HF_HUB_OFFLINE") != "1":
        raise ValueError("published hosted image does not pin the model hub offline")

    script = (
        "from exomem.embeddings import MODEL_NAME;"
        "from sentence_transformers import SentenceTransformer;"
        "m = SentenceTransformer(MODEL_NAME, device='cpu');"
        "v = m.encode(['release verification'], show_progress_bar=False);"
        "assert v.shape[0] == 1 and v.shape[1] > 0, v.shape;"
        "print('ok', MODEL_NAME, v.shape[1])"
    )
    result = _run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--user",
            "10001:10001",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/tmp",
            "--env",
            "HOME=/tmp",
            "--entrypoint",
            "python",
            image,
            "-c",
            script,
        ],
        timeout=900,
    )
    if "ok " not in result.stdout:
        raise ValueError("published hosted image cannot embed offline")


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
    _verify_offline_embedding_capability(image, environment)

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


def _decode_json(raw: bytes, *, label: str) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"{label} contains a duplicate JSON field")
            value[key] = item
        return value

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not strict UTF-8 JSON") from error
    return value


def _decode_object(raw: bytes, *, label: str) -> dict[str, Any]:
    value = _decode_json(raw, label=label)
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not a JSON object")
    return value


def _load_bytes(path: Path, *, label: str, max_bytes: int = _MAX_FIXTURE_BYTES) -> bytes:
    raw = path.read_bytes()
    if not 1 <= len(raw) <= max_bytes:
        raise ValueError(f"{label} exceeds its size contract")
    return raw


def _load(path: Path) -> dict[str, Any]:
    return _decode_object(_load_bytes(path, label=str(path)), label=str(path))


def _load_script(name: str) -> Any:
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"{name} is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _evidence_bytes(
    directory: Path,
    expected_sha256: str,
    *,
    label: str,
    composer: Any,
    require_canonical_json: bool = True,
) -> tuple[Path, bytes]:
    """Select one bounded, canonical evidence file by its reviewed digest."""

    if not _SHA256.fullmatch(expected_sha256):
        raise ValueError(f"{label} digest is invalid")
    try:
        information = directory.lstat()
    except OSError as error:
        raise ValueError("canonical deployment-lock evidence directory is unavailable") from error
    if stat.S_ISLNK(information.st_mode) or not stat.S_ISDIR(information.st_mode):
        raise ValueError("canonical deployment-lock evidence directory is unsafe")
    try:
        entries = list(directory.iterdir())
    except OSError as error:
        raise ValueError("canonical deployment-lock evidence directory cannot be read") from error
    if not 1 <= len(entries) <= _MAX_LOCK_EVIDENCE_FILES:
        raise ValueError("canonical deployment-lock evidence file count is invalid")
    matches: list[tuple[Path, bytes]] = []
    for path in entries:
        try:
            entry = path.lstat()
        except OSError as error:
            raise ValueError("cannot inspect deployment-lock evidence") from error
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
            raise ValueError("deployment-lock evidence must be regular non-symlink files")
        raw = composer._read_regular(
            path, label=f"{label} evidence", maximum=composer.MAX_EVIDENCE_BYTES
        )
        if hashlib.sha256(raw).hexdigest() == expected_sha256:
            matches.append((path, raw))
    if len(matches) != 1:
        raise ValueError(f"{label} evidence did not yield exactly one reviewed file")
    path, raw = matches[0]
    value = _decode_json(raw, label=f"{label} evidence")
    if require_canonical_json and raw != composer._canonical(value):
        raise ValueError(f"{label} evidence is not canonical JSON")
    return path, raw


def _evidence_file(
    directory: Path, expected_sha256: str, *, label: str, composer: Any
) -> tuple[Path, dict[str, Any]]:
    path, raw = _evidence_bytes(directory, expected_sha256, label=label, composer=composer)
    value = _decode_object(raw, label=f"{label} evidence")
    return path, value


def _validate_legacy_manifest(manifest: dict[str, Any], lock: dict[str, Any]) -> None:
    """Bind the reviewed v1 rollback manifest to exactly one embedded legacy unit."""

    validate_release_manifest(manifest, manifest)
    matches = []
    for unit in lock["composition"]["legacyCatalog"]:
        contract = unit["contract"]
        if (
            manifest["release"] == contract["releaseVersion"]
            and manifest["hostedProtocol"] == contract["protocolVersion"]
            and manifest["runtimeImage"] == contract["runtimeImage"]
            and manifest["sourceCommit"] == contract["sourceCommit"]
        ):
            matches.append(unit)
    if len(matches) != 1:
        raise ValueError("rollback manifest does not equal one reviewed legacy runtime identity")


def _substitute_v1_corpus_tokens(value: object) -> object:
    if value == "$ACTIVE_SERVICE_CREDENTIAL":
        return "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
    if value == "$NEXT_SERVICE_CREDENTIAL":
        return "ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8"
    if value == "$NOW_PLUS_86400_SECONDS":
        return (datetime.now(UTC) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    if value == "$NOW_PLUS_600_SECONDS":
        return (datetime.now(UTC) + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {name: _substitute_v1_corpus_tokens(item) for name, item in value.items()}
    if isinstance(value, list):
        return [_substitute_v1_corpus_tokens(item) for item in value]
    return value


def _validate_frozen_v1_corpus(raw: bytes) -> None:
    """Require the exact frozen corpus and validate every legacy wire shape."""

    if hashlib.sha256(raw).hexdigest() != _FROZEN_V1_CORPUS_SHA256:
        raise ValueError("rollback v1 corpus does not have the frozen v1 corpus digest")
    if _FROZEN_V1_CORPUS.is_symlink() or not _FROZEN_V1_CORPUS.is_file():
        raise ValueError("repository frozen v1 corpus is unavailable")
    frozen = _FROZEN_V1_CORPUS.read_bytes()
    if hashlib.sha256(frozen).hexdigest() != _FROZEN_V1_CORPUS_SHA256 or raw != frozen:
        raise ValueError("rollback v1 corpus differs from the repository frozen corpus")
    provisioner_source = os.fspath(_REPOSITORY_ROOT / "infra/provisioner/src")
    inserted = provisioner_source not in sys.path
    if inserted:
        sys.path.insert(0, provisioner_source)
    try:
        from exomem_provisioner.schemas import FailureResponse, PendingResponse, request_plaintext
        from exomem_provisioner.wire_protocol import (
            FINAL_MODELS_BY_PROTOCOL,
            REQUEST_MODELS_BY_PROTOCOL,
            WIRE_PROTOCOL_V1,
        )
    except ImportError as error:
        raise ValueError("strict provisioner wire models are unavailable") from error
    finally:
        if inserted:
            sys.path.remove(provisioner_source)
    payload = _decode_json(raw, label="rollback v1 corpus")
    if not isinstance(payload, dict) or set(payload) != {
        "schemaVersion",
        "protocol",
        "actions",
        "errors",
    }:
        raise ValueError("rollback v1 corpus envelope is invalid")
    if (
        payload["schemaVersion"] != 1
        or payload["protocol"] != WIRE_PROTOCOL_V1
        or not isinstance(payload["actions"], dict)
    ):
        raise ValueError("rollback v1 corpus protocol is invalid")
    request_models = REQUEST_MODELS_BY_PROTOCOL[WIRE_PROTOCOL_V1]
    final_models = FINAL_MODELS_BY_PROTOCOL[WIRE_PROTOCOL_V1]
    if set(payload["actions"]) != set(request_models):
        raise ValueError("rollback v1 corpus action coverage is invalid")
    try:
        for action, sample in payload["actions"].items():
            if not isinstance(sample, dict):
                raise ValueError("rollback v1 corpus action is invalid")
            request = _substitute_v1_corpus_tokens(sample["request"])
            model = request_models[action].model_validate(request)
            if request_plaintext(model) != request:
                raise ValueError("rollback v1 corpus request shape is invalid")
            pending = sample["pending"]
            if not isinstance(pending, dict) or pending.get("status") != 202 or pending.get("headers") != {
                "retry-after": "2"
            }:
                raise ValueError("rollback v1 corpus pending shape is invalid")
            PendingResponse.model_validate(pending["body"])
            final = sample["final"]
            response_model = final_models[action]
            if response_model is None:
                if final != {"status": 204, "body": None}:
                    raise ValueError("rollback v1 corpus final shape is invalid")
            else:
                if not isinstance(final, dict) or final.get("status") != 200:
                    raise ValueError("rollback v1 corpus final shape is invalid")
                response_model.model_validate(_substitute_v1_corpus_tokens(final["body"]))
        failures = payload["errors"]
        if not isinstance(failures, list) or not failures:
            raise ValueError("rollback v1 corpus failures are invalid")
        for failure in failures:
            if not isinstance(failure, dict) or failure.get("status") not in {400, 409, 422, 500, 503}:
                raise ValueError("rollback v1 corpus failure status is invalid")
            body = failure.get("body")
            FailureResponse.model_validate(body)
            if "sentinel" in json.dumps(body):
                raise ValueError("rollback v1 corpus failure body is invalid")
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("rollback v1 corpus does not satisfy strict v1 wire models") from error


def _verify_lock_evidence(lock: dict[str, Any], directory: Path, composer: Any) -> None:
    """Revalidate every fixed artifact that was used to compose the lock pair."""

    composer.validate_deployment_lock(lock)
    composition = lock["composition"]
    components = lock["components"]
    forward_path, forward = _evidence_file(
        directory, composition["forwardContractSha256"], label="forward runtime contract", composer=composer
    )
    target, image, source = composer._contract(forward, label="forward runtime contract")
    if target != lock["runtimeTarget"] or image != components["runtime"]["image"] or source != components["runtime"]["sourceCommit"]:
        raise ValueError("forward runtime contract differs from the selected deployment lock")

    authority_path, authority = _evidence_file(
        directory,
        composition["authoritativeLegacyReleaseSetSha256"],
        label="authoritative legacy release set",
        composer=composer,
    )
    contracts: list[Any] = []
    catalog_units: list[dict[str, Any]] = []
    for unit in composition["legacyCatalog"]:
        contract_path, contract = _evidence_file(
            directory, unit["contractSha256"], label="legacy contract", composer=composer
        )
        if contract != unit["contract"]:
            raise ValueError("legacy contract differs from the selected deployment lock")
        contracts.append(composer.HashedInput(contract_path, unit["contractSha256"]))
        catalog_units.append({key: unit[key] for key in unit if key != "contract"})
    reconstructed, release_set_sha = composer._legacy_catalog(
        {"schemaVersion": 1, "units": catalog_units},
        authority,
        tuple(contracts),
    )
    if reconstructed != composition["legacyCatalog"] or release_set_sha != composition["legacyReleaseSetSha256"]:
        raise ValueError("reviewed legacy catalog differs from the selected deployment lock")

    legacy_manifest_path, legacy_manifest = _evidence_file(
        directory,
        lock["rollback"]["legacyManifestSha256"],
        label="rollback legacy manifest",
        composer=composer,
    )
    _validate_legacy_manifest(legacy_manifest, lock)
    rollback = composer._rollback(lock["rollback"])
    if rollback["v1CorpusSha256"] != _FROZEN_V1_CORPUS_SHA256:
        raise ValueError("rollback v1 corpus does not use the frozen v1 corpus digest")
    _, corpus_raw = _evidence_bytes(
        directory,
        rollback["v1CorpusSha256"],
        label="rollback v1 corpus",
        composer=composer,
        require_canonical_json=False,
    )
    _validate_frozen_v1_corpus(corpus_raw)
    _ = forward_path, authority_path, legacy_manifest_path


def _candidate_matches_lock(candidate: dict[str, Any], component: dict[str, Any], *, kind: str, release: str | None) -> None:
    if candidate.get("kind") != kind:
        raise ValueError("discovered candidate kind differs from the deployment lock")
    image = candidate.get("image")
    source = candidate.get("source")
    if not isinstance(image, dict) or not isinstance(source, dict):
        raise ValueError("discovered candidate identity is invalid")
    if image.get("reference") != component.get("image") or source.get("commit") != component.get("sourceCommit"):
        raise ValueError("discovered candidate identity differs from the deployment lock")
    if release is not None:
        candidate_release = candidate.get("release")
        if not isinstance(candidate_release, dict) or candidate_release.get("tag") != f"v{release}":
            raise ValueError("runtime candidate release differs from the deployment lock")


def _one_candidate(paths: list[Path], expected_sha256: str, loader: Any) -> Path:
    if not 1 <= len(paths) <= _MAX_RUNTIME_CANDIDATE_ASSETS:
        raise ValueError("candidate discovery count exceeds its size contract")
    matches = [
        path
        for path in paths
        if hashlib.sha256(
            loader._read_regular(
                path,
                label="candidate",
                maximum=loader.MAX_CANDIDATE_BYTES,
            )
        ).hexdigest()
        == expected_sha256
    ]
    if len(matches) != 1:
        raise ValueError("candidate discovery did not yield exactly one locked candidate")
    loader.load_candidate(matches[0])
    return matches[0]


def _release_candidate_assets(gh_binary: str, tag: str) -> list[str]:
    listing = json.loads(
        _run(
            [
                gh_binary,
                "release",
                "view",
                tag,
                "--repo",
                "Artexis10/exomem",
                "--json",
                "assets",
            ]
        ).stdout
    )
    assets = listing.get("assets") if isinstance(listing, dict) else None
    if not isinstance(assets, list):
        raise ValueError("runtime candidate asset listing is invalid")
    candidate_assets = [
        asset
        for asset in assets
        if isinstance(asset, dict)
        and isinstance(asset.get("name"), str)
        and asset["name"].endswith(".candidate-v1.json")
    ]
    if not 1 <= len(candidate_assets) <= _MAX_RUNTIME_CANDIDATE_ASSETS:
        raise ValueError("runtime candidate asset count exceeds its size contract")
    selected: list[str] = []
    for asset in candidate_assets:
        name = asset["name"]
        size = asset.get("size")
        if not isinstance(size, int) or not 1 <= size <= 128 * 1024:
            raise ValueError("runtime candidate asset exceeds its size contract")
        stem = name.removesuffix(".candidate-v1.json")
        selected.append(name)
        for suffix in (".sigstore.json", ".candidate.sigstore.json"):
            bundle = next(
                (
                    item
                    for item in assets
                    if isinstance(item, dict) and item.get("name") == f"{stem}{suffix}"
                ),
                None,
            )
            if not isinstance(bundle, dict) or not isinstance(bundle.get("size"), int):
                raise ValueError("runtime candidate bundle is unavailable")
            if not 1 <= bundle["size"] <= 16 * 1024 * 1024:
                raise ValueError("runtime candidate bundle exceeds its size contract")
            selected.append(bundle["name"])
    return selected


def _pulled_candidate_paths(directory: Path, loader: Any) -> list[Path]:
    entries = list(directory.iterdir())
    if not 1 <= len(entries) <= _MAX_PULL_FILES:
        raise ValueError("candidate attachment file count exceeds its size contract")
    candidates: list[Path] = []
    for path in entries:
        try:
            information = path.lstat()
        except OSError as error:
            raise ValueError("cannot inspect candidate attachment") from error
        if stat.S_ISLNK(information.st_mode) or not stat.S_ISREG(information.st_mode):
            raise ValueError("candidate attachment must be a regular non-symlink file")
        maximum = (
            loader.MAX_CANDIDATE_BYTES
            if path.name.endswith(".candidate-v1.json")
            else loader.MAX_BUNDLE_BYTES
        )
        loader._read_regular(path, label="candidate attachment", maximum=maximum)
        if path.name.endswith(".candidate-v1.json"):
            candidates.append(path)
    return candidates


def _oci_descriptor(
    value: object,
    *,
    label: str,
    media_type: str,
    maximum: int,
    allow_artifact_type: bool = False,
) -> tuple[str, int]:
    fields = {"mediaType", "digest", "size", "annotations", "data"}
    if allow_artifact_type:
        fields.add("artifactType")
    if not isinstance(value, dict) or set(value) - fields:
        raise ValueError(f"{label} descriptor is invalid")
    digest = value.get("digest")
    size = value.get("size")
    annotations = value.get("annotations")
    inline = value.get("data")
    if value.get("mediaType") != media_type or not isinstance(digest, str):
        raise ValueError(f"{label} descriptor media type is invalid")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest) or not isinstance(size, int):
        raise ValueError(f"{label} descriptor identity is invalid")
    if not 1 <= size <= maximum:
        raise ValueError(f"{label} descriptor exceeds its size contract")
    if annotations is not None and (
        not isinstance(annotations, dict)
        or any(not isinstance(key, str) or not isinstance(item, str) for key, item in annotations.items())
    ):
        raise ValueError(f"{label} descriptor annotations are invalid")
    if inline is not None:
        # An inline descriptor MUST carry exactly the blob it names (OCI image-spec).
        if not isinstance(inline, str):
            raise ValueError(f"{label} descriptor inline data is invalid")
        try:
            decoded = base64.b64decode(inline, validate=True)
        except ValueError as error:
            raise ValueError(f"{label} descriptor inline data is invalid") from error
        if len(decoded) != size or f"sha256:{hashlib.sha256(decoded).hexdigest()}" != digest:
            raise ValueError(f"{label} descriptor inline data does not match its digest")
    return digest, size


def _validate_oci_candidate_manifest(manifest: object, *, subject_image: str | None = None) -> int:
    """Reject OCI attachments before their layers are transferred locally."""

    if not isinstance(manifest, dict) or set(manifest) - {
        "schemaVersion",
        "mediaType",
        "artifactType",
        "config",
        "layers",
        "subject",
        "annotations",
    }:
        raise ValueError("provisioner candidate OCI manifest is invalid")
    if (
        manifest.get("schemaVersion") != 2
        or manifest.get("mediaType") != _OCI_MANIFEST_MEDIA_TYPE
        or manifest.get("artifactType") != _CANDIDATE_MEDIA_TYPE
    ):
        raise ValueError("provisioner candidate OCI manifest type is invalid")
    if subject_image is not None:
        subject = manifest.get("subject")
        if not isinstance(subject, dict) or set(subject) - {
            "mediaType",
            "digest",
            "size",
            "annotations",
        }:
            raise ValueError("provisioner candidate OCI subject is invalid")
        subject_digest = subject.get("digest")
        subject_size = subject.get("size")
        if (
            subject.get("mediaType") not in _OCI_SUBJECT_MEDIA_TYPES
            or subject_digest != subject_image.rsplit("@", 1)[1]
            or not isinstance(subject_size, int)
            or not 1 <= subject_size <= _MAX_FIXTURE_BYTES
        ):
            raise ValueError("provisioner candidate OCI subject differs from the locked image")
    config_digest, config_size = _oci_descriptor(
        manifest.get("config"),
        label="provisioner candidate OCI config",
        media_type=_OCI_EMPTY_CONFIG_MEDIA_TYPE,
        maximum=_MAX_OCI_CONFIG_BYTES,
    )
    if not config_digest or config_size < 1:
        raise ValueError("provisioner candidate OCI config is invalid")
    layers = manifest.get("layers")
    if not isinstance(layers, list) or len(layers) != 2:
        raise ValueError("provisioner candidate OCI layer count is invalid")
    seen: set[str] = set()
    observed: dict[str, int] = {}
    for expected_type, maximum in (
        (_CANDIDATE_MEDIA_TYPE, 128 * 1024),
        (_CANDIDATE_BUNDLE_MEDIA_TYPE, 16 * 1024 * 1024),
    ):
        descriptors = [layer for layer in layers if isinstance(layer, dict) and layer.get("mediaType") == expected_type]
        if len(descriptors) != 1:
            raise ValueError("provisioner candidate OCI layer media types are invalid")
        digest, size = _oci_descriptor(
            descriptors[0],
            label="provisioner candidate OCI layer",
            media_type=expected_type,
            maximum=maximum,
        )
        if digest in seen:
            raise ValueError("provisioner candidate OCI layers are duplicated")
        seen.add(digest)
        observed[expected_type] = size
    if config_size + sum(observed.values()) > _MAX_OCI_ATTACHMENT_BYTES:
        raise ValueError("provisioner candidate OCI attachment exceeds its size contract")
    return config_size + sum(observed.values())


def _oci_referrer_descriptors(discovered: object) -> list[dict[str, Any]]:
    manifests: object = None
    if isinstance(discovered, dict):
        # oras emits "referrers" from 1.3 onward; earlier releases emitted "manifests".
        manifests = discovered.get("referrers")
        if manifests is None:
            manifests = discovered.get("manifests")
    if not isinstance(manifests, list) or not 1 <= len(manifests) <= _MAX_PROVISIONER_REFERRERS:
        raise ValueError("provisioner candidate attachment discovery count exceeds its size contract")
    descriptors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for manifest in manifests:
        # "reference" and the nested "referrers" tree are oras reporting fields, not
        # part of the OCI descriptor; drop them before the strict descriptor check.
        if not isinstance(manifest, dict) or set(manifest) - {
            "mediaType",
            "digest",
            "size",
            "artifactType",
            "annotations",
            "reference",
            "referrers",
        }:
            raise ValueError("provisioner candidate attachment descriptor is invalid")
        descriptor = {
            key: value
            for key, value in manifest.items()
            if key in {"mediaType", "digest", "size", "artifactType", "annotations"}
        }
        if descriptor.get("artifactType") != _CANDIDATE_MEDIA_TYPE:
            raise ValueError("provisioner candidate attachment type is invalid")
        digest, _ = _oci_descriptor(
            descriptor,
            label="provisioner candidate attachment",
            media_type=_OCI_MANIFEST_MEDIA_TYPE,
            maximum=_MAX_OCI_ATTACHMENT_BYTES,
            allow_artifact_type=True,
        )
        if digest in seen:
            raise ValueError("provisioner candidate attachment descriptors are duplicated")
        seen.add(digest)
        descriptors.append(descriptor)
    return descriptors


def _verified_provisioner_candidate(
    *,
    image: str,
    source_commit: str,
    expected_sha256: str | None,
    directory: Path,
    candidate_tool: Any,
    oras_binary: str,
    gh_binary: str,
) -> None:
    repository_name = image.split("@", 1)[0]
    discovered = json.loads(
        _run(
            [
                oras_binary,
                "discover",
                "--artifact-type",
                _CANDIDATE_MEDIA_TYPE,
                image,
                "--format",
                "json",
            ]
        ).stdout
    )
    attachments: list[tuple[str, dict[str, Any]]] = []
    total_size = 0
    for descriptor in _oci_referrer_descriptors(discovered):
        digest = descriptor["digest"]
        manifest = _decode_object(
            _run([oras_binary, "manifest", "fetch", f"{repository_name}@{digest}"]).stdout.encode(),
            label="provisioner candidate OCI manifest",
        )
        total_size += _validate_oci_candidate_manifest(manifest, subject_image=image)
        if total_size > _MAX_DISCOVERED_OCI_BYTES:
            raise ValueError("provisioner candidate OCI discovery exceeds its size contract")
        attachments.append((digest, manifest))
    candidate_paths: list[Path] = []
    for index, (digest, _) in enumerate(attachments):
        destination = directory / str(index)
        destination.mkdir(mode=0o700)
        _run(
            [
                oras_binary,
                "pull",
                f"{repository_name}@{digest}",
                "--output",
                os.fspath(destination),
            ]
        )
        candidate_paths.extend(_pulled_candidate_paths(destination, candidate_tool))
    component = {"image": image, "sourceCommit": source_commit}
    matches: list[Path] = []
    for path in candidate_paths:
        raw = candidate_tool._read_regular(
            path, label="candidate", maximum=candidate_tool.MAX_CANDIDATE_BYTES
        )
        if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
            continue
        candidate = candidate_tool.load_candidate(path)
        _candidate_matches_lock(candidate, component, kind="provisioner", release=None)
        matches.append(path)
    if len(matches) != 1:
        raise ValueError("provisioner candidate discovery did not yield exactly one locked candidate")
    candidate = matches[0]
    candidate_tool.verify_candidate(
        candidate,
        bundle_from_oci=True,
        candidate_bundle=candidate.with_name(
            candidate.name.removesuffix(".candidate-v1.json") + ".candidate.sigstore.json"
        ),
        gh_binary=gh_binary,
    )


def _verify_substrate_v1_consumer(commit: str, gh_binary: str) -> None:
    observed = _run(
        [
            gh_binary,
            "api",
            "--method",
            "GET",
            f"repos/substrate-systems/substrate/git/commits/{commit}",
            "--jq",
            ".sha",
        ]
    ).stdout.strip()
    if observed != commit:
        raise ValueError("rollback Substrate v1 consumer commit is unavailable")


def verify_selected_deployment_lock(
    *, phase: str, repository: Path, oras_binary: str = "oras", gh_binary: str = "gh"
) -> dict[str, Any]:
    """Reverify a selected member plus its candidate attestations and source closures."""

    prepare = _load_script("prepare_hosted_release.py")
    composer = _load_script("hosted_composition_lock.py")
    candidate_tool = _load_script("hosted_image_candidate.py")
    selected, _ = prepare._select_member(
        prepare._load_pair(_CANONICAL_DEPLOYMENT_LOCK_PAIR), phase=phase, member_sha256=None
    )
    _verify_lock_evidence(selected, _CANONICAL_DEPLOYMENT_LOCK_EVIDENCE, composer)
    components = selected["components"]
    target = selected["runtimeTarget"]
    closure = selected["composition"]["sourceClosure"]
    composer.verify_source_closure(repository, components["runtime"]["sourceCommit"], selected["composition"]["commit"], tuple(closure["runtime"]["paths"]))
    composer.verify_source_closure(repository, components["provisioner"]["sourceCommit"], selected["composition"]["commit"], tuple(closure["provisioner"]["paths"]))
    with tempfile.TemporaryDirectory(prefix="exomem-lock-proof-") as directory:
        root = Path(directory)
        runtime_dir = root / "runtime"
        runtime_dir.mkdir(mode=0o700)
        tag = f"v{target['releaseVersion']}"
        assets = _release_candidate_assets(gh_binary, tag)
        command = [gh_binary, "release", "download", tag, "--repo", "Artexis10/exomem", "--dir", os.fspath(runtime_dir)]
        for asset in assets:
            command.extend(["--pattern", asset])
        _run(command)
        runtime_candidate = _one_candidate(list(runtime_dir.glob("*.candidate-v1.json")), components["runtime"]["candidateSha256"], candidate_tool)
        _candidate_matches_lock(candidate_tool.load_candidate(runtime_candidate), components["runtime"], kind="runtime", release=target["releaseVersion"])
        runtime_stem = runtime_candidate.name.removesuffix(".candidate-v1.json")
        candidate_tool.verify_candidate(runtime_candidate, bundle=runtime_dir / f"{runtime_stem}.sigstore.json", candidate_bundle=runtime_dir / f"{runtime_stem}.candidate.sigstore.json", gh_binary=gh_binary)
        provisioner_dir = root / "provisioner"
        provisioner_dir.mkdir(mode=0o700)
        _verified_provisioner_candidate(
            image=components["provisioner"]["image"],
            source_commit=components["provisioner"]["sourceCommit"],
            expected_sha256=components["provisioner"]["candidateSha256"],
            directory=provisioner_dir,
            candidate_tool=candidate_tool,
            oras_binary=oras_binary,
            gh_binary=gh_binary,
        )
        rollback_dir = root / "rollback-provisioner"
        rollback_dir.mkdir(mode=0o700)
        rollback = selected["rollback"]
        _verified_provisioner_candidate(
            image=rollback["provisionerImage"],
            source_commit=rollback["provisionerSourceCommit"],
            expected_sha256=None,
            directory=rollback_dir,
            candidate_tool=candidate_tool,
            oras_binary=oras_binary,
            gh_binary=gh_binary,
        )
        _verify_substrate_v1_consumer(rollback["substrateV1ConsumerCommit"], gh_binary)
    return selected


def fetch_selected_gateway_fixture(
    release: dict[str, Any],
    selection: dict[str, Any],
) -> tuple[dict[str, Any], bytes]:
    """Fetch only the reviewed commit/path and accept it only at the pinned digest."""

    validate_substrate_selection(release, selection)
    url = (
        "https://raw.githubusercontent.com/substrate-systems/substrate/"
        f"{selection['sourceCommit']}/{selection['fixturePath']}"
    )
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "exomem-release-verifier/1"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(_MAX_FIXTURE_BYTES + 1)
    except (OSError, urllib.error.HTTPError, urllib.error.URLError) as error:
        raise RuntimeError("reviewed Substrate fixture could not be fetched") from error
    if not 1 <= len(raw) <= _MAX_FIXTURE_BYTES:
        raise ValueError("reviewed Substrate fixture exceeds its size contract")
    fixture = _decode_object(raw, label="reviewed Substrate fixture")
    validate_selected_gateway_fixture(release, selection, fixture, raw)
    return fixture, raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("expand", "contract"))
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--oras-binary", default="oras")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--runtime-gate", type=Path)
    parser.add_argument("--substrate-selection", type=Path)
    fixture_source = parser.add_mutually_exclusive_group(required=False)
    fixture_source.add_argument("--substrate-fixture", type=Path)
    fixture_source.add_argument("--fetch-substrate-fixture", action="store_true")
    parser.add_argument("--probe-image", action="store_true")
    args = parser.parse_args(argv)

    if args.phase is not None:
        if args.repository is None:
            parser.error("--phase requires --repository")
        verify_selected_deployment_lock(
            phase=args.phase,
            repository=args.repository,
            oras_binary=args.oras_binary,
        )
        print("hosted deployment lock verified")
        return 0
    if any(value is None for value in (args.manifest, args.runtime_gate, args.substrate_selection)) or not (
        args.substrate_fixture or args.fetch_substrate_fixture
    ):
        parser.error("v1 release proof requires manifest, gate, selection, and one fixture source")

    release = _load(args.manifest)
    gate = _load(args.runtime_gate)
    selection = _load(args.substrate_selection)
    validate_release_manifest(release, gate)
    validate_substrate_selection(release, selection)
    if args.fetch_substrate_fixture:
        fixture, _ = fetch_selected_gateway_fixture(release, selection)
    else:
        raw_fixture = _load_bytes(
            args.substrate_fixture,
            label="selected Substrate fixture",
        )
        fixture = _decode_object(raw_fixture, label="selected Substrate fixture")
        validate_selected_gateway_fixture(release, selection, fixture, raw_fixture)
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
