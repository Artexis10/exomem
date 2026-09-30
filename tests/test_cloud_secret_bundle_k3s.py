"""Opt-in field-ownership check against a disposable K3s API server."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
IMAGE = json.loads((ROOT / "infra/contracts/exomem-hosted-runtime-k3s-gate-v1.json").read_text())[
    "k3sImage"
]


def _run(command: list[str], *, input: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, input=input, capture_output=True, check=False, timeout=45)


@pytest.mark.skipif(
    os.environ.get("RUN_CLOUD_SECRET_BUNDLE_K3S_TEST") != "1",
    reason="set RUN_CLOUD_SECRET_BUNDLE_K3S_TEST=1 for disposable K3s",
)
def test_bundle_rotation_and_foreign_field_ownership(tmp_path: Path) -> None:
    name = f"exomem-bundle-k3s-{uuid.uuid4().hex[:12]}"
    started = _run(
        [
            "docker",
            "run",
            "--privileged",
            "--detach",
            "--name",
            name,
            IMAGE,
            "server",
            "--disable=traefik",
            "--disable=servicelb",
        ]
    )
    assert started.returncode == 0, started.stderr.decode(errors="replace")
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            ready = _run(["docker", "exec", name, "kubectl", "get", "--raw=/readyz"])
            if ready.returncode == 0 and ready.stdout.strip() == b"ok":
                break
            time.sleep(2)
        else:
            pytest.fail("disposable K3s API did not become ready")

        matrix = tmp_path / "matrix.json"
        matrix.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "secrets": {
                        "bundle": {
                            "value_shape": "json-object",
                            "destinations": {
                                "k3s.bundle.active": {
                                    "kind": "sops_k8s_secret",
                                    "slot": "active",
                                    "target": str(tmp_path / "bundle.{version}.sops.json"),
                                    "namespace": "default",
                                    "kubernetes_secret": "bundle",
                                    "key_sets": [
                                        ["current", "currentVersion"],
                                        [
                                            "current",
                                            "currentVersion",
                                            "previous",
                                            "previousVersion",
                                        ],
                                    ],
                                }
                            },
                        }
                    },
                }
            )
        )
        fake_sops = tmp_path / "sops"
        fake_sops.write_text(
            "#!/usr/bin/env python3\nimport os, pathlib, sys\nsys.stdout.buffer.write(pathlib.Path(os.environ['BUNDLE_PLAINTEXT']).read_bytes())\n"
        )
        fake_sops.chmod(0o700)
        kubectl = tmp_path / "kubectl"
        kubectl.write_text(f'#!/bin/sh\nexec docker exec -i {name} kubectl "$@"\n')
        kubectl.chmod(0o700)

        current = "ab" * 32
        previous = "cd" * 32
        versions = [
            {"current": current, "currentVersion": "1"},
            {
                "current": previous,
                "currentVersion": "2",
                "previous": current,
                "previousVersion": "1",
            },
            {"current": previous, "currentVersion": "2"},
        ]
        for number, fields in enumerate(versions, start=1):
            artifact = tmp_path / f"bundle.v{number}.sops.json"
            artifact.write_text('{"sops": {}}')
            plaintext = tmp_path / f"plaintext-v{number}.json"
            plaintext.write_text(
                json.dumps(
                    {
                        "apiVersion": "v1",
                        "kind": "Secret",
                        "metadata": {
                            "name": "bundle",
                            "namespace": "default",
                            "labels": {
                                "app.kubernetes.io/managed-by": "exomem-secret-handoff",
                                "exomem.io/secret-version": f"v{number}",
                            },
                        },
                        "type": "Opaque",
                        "stringData": fields,
                    }
                )
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "infra/scripts/apply_sops_secret.py"),
                    "--matrix",
                    str(matrix),
                    "--destination",
                    "k3s.bundle.active",
                    "--artifact",
                    str(artifact),
                    "--sops",
                    str(fake_sops),
                    "--kubectl",
                    str(kubectl),
                ],
                env={**os.environ, "BUNDLE_PLAINTEXT": str(plaintext)},
                capture_output=True,
                check=False,
                timeout=60,
            )
            assert result.returncode == 0, result.stderr.decode(errors="replace")
            observed = _run(
                [
                    "docker",
                    "exec",
                    name,
                    "kubectl",
                    "get",
                    "secret",
                    "bundle",
                    '-o=go-template={{range $key, $value := .data}}{{$key}}{{"\\n"}}{{end}}',
                ]
            )
            assert set(observed.stdout.decode().splitlines()) == set(fields)

        foreign = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "bundle", "namespace": "default"},
            "data": {
                "previous": base64.b64encode(current.encode()).decode(),
                "previousVersion": base64.b64encode(b"1").decode(),
            },
        }
        added = _run(
            [
                "docker",
                "exec",
                "-i",
                name,
                "kubectl",
                "apply",
                "--server-side",
                "--field-manager=foreign",
                "-f",
                "-",
            ],
            input=json.dumps(foreign).encode(),
        )
        assert added.returncode == 0, added.stderr.decode(errors="replace")
        repeated = subprocess.run(
            [
                sys.executable,
                str(ROOT / "infra/scripts/apply_sops_secret.py"),
                "--matrix",
                str(matrix),
                "--destination",
                "k3s.bundle.active",
                "--artifact",
                str(tmp_path / "bundle.v3.sops.json"),
                "--sops",
                str(fake_sops),
                "--kubectl",
                str(kubectl),
            ],
            env={**os.environ, "BUNDLE_PLAINTEXT": str(tmp_path / "plaintext-v3.json")},
            capture_output=True,
            check=False,
            timeout=60,
        )
        assert repeated.returncode == 2
        assert b"key verification failed" in repeated.stderr
        assert b"Applied" not in repeated.stdout
        assert current.encode() not in repeated.stdout + repeated.stderr
    finally:
        _run(["docker", "rm", "--force", name])
