from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml
from benchmark_capabilities import (
    require_posix_executable_scripts,
    require_posix_file_modes,
    require_procfs_descriptor_paths,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

pytestmark = pytest.mark.reads_repository_docs

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"

_NAMESPACE_OWNED_MARKERS = (
    *(
        ("labels", marker)
        for marker in (
            "exomem.io/tenant-cell",
            "exomem.io/cell-resource",
        )
    ),
    *(
        ("annotations", marker)
        for marker in (
            "exomem.io/tenant-id",
            "exomem.io/cell-id",
            "exomem.io/operation-id",
            "exomem.io/tenant-digest",
            "exomem.io/subject-digest",
            "exomem.io/operation-digest",
            "exomem.io/fence",
            "exomem.io/recovery-envelope",
            "exomem.io/resource-name",
            "exomem.io/pvc-name",
            "exomem.io/credentials-secret-name",
            "exomem.io/init-request-configmap-name",
            "exomem.io/provision-mode",
            "exomem.io/vault-id",
            "exomem.io/expected-release",
            "exomem.io/worker-policy-digest",
            "exomem.io/browser-origin",
            "exomem.io/transfer-hostname",
            "exomem.io/runtime-admitted",
        )
    ),
)


def _load(relative: str, name: str) -> ModuleType:
    path = ROOT / relative
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o700)


def _signed_receipt(
    receipt: dict[str, object], private_key: Ed25519PrivateKey, *, domain: bytes = b""
) -> dict[str, object]:
    canonical = json.dumps(receipt, separators=(",", ":"), sort_keys=True).encode()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return {
        **receipt,
        "authentication": {
            "algorithm": "ed25519",
            "key_id": hashlib.sha256(public_key).hexdigest(),
            "signature": private_key.sign(domain + canonical).hex(),
        },
    }


def _write_active_secret_completion(registry_path: Path, public_key_path: Path) -> None:
    registry_path.with_name(f"{registry_path.name}.complete").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "registry": registry_path.name,
                "public_key": public_key_path.name,
                "registry_sha256": hashlib.sha256(registry_path.read_bytes()).hexdigest(),
                "public_key_sha256": hashlib.sha256(public_key_path.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )


def test_hosted_ci_wires_every_static_security_gate() -> None:
    workflow = (ROOT / ".github/workflows/hosted-infrastructure.yml").read_text(encoding="utf-8")
    validator = (INFRA / "scripts/validate.sh").read_text(encoding="utf-8")
    tool_versions = (INFRA / "tool-versions.env").read_text(encoding="utf-8")
    combined = workflow + validator
    for required in (
        "terraform",
        "tflint",
        "checkov",
        "ansible-lint",
        "helm",
        "kubeconform",
        "conftest",
        "sops",
        "ruff",
        "mypy",
        "pytest",
        "trivy",
        "shellcheck",
    ):
        assert required in combined
    assert "secret" in combined.lower()
    assert 'HELM_REPOSITORY_CONFIG="${helm_repository_config}"' in validator
    assert 'HELM_REPOSITORY_CACHE="${helm_repository_cache}"' in validator
    assert 'UV_VERSION: "0.11.28"' in workflow
    assert 'PYTHON_VERSION: "3.13.5"' in workflow
    assert 'NODE_VERSION: "22.17.1"' in workflow
    assert 'GO_VERSION: "1.26.5"' in workflow
    assert "go-version: ${{ env.GO_VERSION }}" in workflow
    assert "GO_VERSION=1.26.5" in tool_versions
    assert "go install github.com/aquasecurity/trivy" not in workflow
    assert "https://github.com/aquasecurity/trivy/releases/download/" in workflow
    assert 'install -d -m 0755 "$HOME/.local/bin"' in workflow
    assert "${TRIVY_LINUX_AMD64_SHA256}" in workflow
    assert (
        "TRIVY_LINUX_AMD64_SHA256=bbb64b9695866ce4a7a8f5c9592002c5961cab378577fa3f8a040df362b9b2ea"
    ) in tool_versions
    parsed = yaml.safe_load(workflow)
    assert parsed["concurrency"] == {
        "group": (
            "hosted-infrastructure-${{ github.event_name == 'pull_request' "
            "&& github.event.pull_request.number || github.run_id }}"
        ),
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }
    triggers = parsed.get("on", parsed.get(True))
    blackbox_input = triggers["workflow_dispatch"]["inputs"]["external_blackbox"]
    assert blackbox_input["default"] is False
    assert blackbox_input["type"] == "boolean"
    static_job = parsed["jobs"]["static"]
    assert static_job["name"] == "Offline static validation"
    validation_step = next(
        step
        for step in static_job["steps"]
        if step.get("name")
        == "Terraform, TFLint, Checkov, Ansible, Helm, policy, SOPS, types, tests, and secret scan"
    )
    assert "RUN_K3S_ADMISSION_TEST=1" in validation_step["run"]
    assert "infra/scripts/validate.sh" in validation_step["run"]
    assert 'pytest -q "${repo_root}"/tests/test_hosted_*.py' in validator
    cache_step = next(
        step for step in static_job["steps"] if step.get("name") == "Restore exact validator bundle"
    )
    assert cache_step["id"] == "validator-cache"
    assert cache_step["uses"] == "actions/cache@27d5ce7f107fe9357f9df03efb73ab90386fccae"
    assert set(cache_step["with"]["path"].splitlines()) == {
        "~/go/bin/tflint",
        "~/go/bin/helm",
        "~/go/bin/kubeconform",
        "~/go/bin/conftest",
        "~/go/bin/sops",
        "~/go/bin/age-keygen",
        "~/.local/bin/trivy",
        "~/.local/bin/shellcheck",
        "~/.local/bin/oras",
        ".venv-hosted-ci",
        "~/.ansible/collections",
    }
    assert "restore-keys" not in cache_step["with"]
    assert cache_step["with"]["key"] == (
        "hosted-validators-v1-${{ runner.os }}-${{ runner.arch }}-"
        "${{ hashFiles('infra/tool-versions.env', "
        "'infra/ansible/collections/requirements.yml') }}"
    )
    install_step = next(
        step
        for step in static_job["steps"]
        if step.get("name") == "Install exact infrastructure validators"
    )
    assert install_step["if"] == "steps.validator-cache.outputs.cache-hit != 'true'"
    expose_step = next(
        step
        for step in static_job["steps"]
        if step.get("name") == "Expose infrastructure validators"
    )
    assert 'echo "$(go env GOPATH)/bin" >> "$GITHUB_PATH"' in expose_step["run"]
    assert 'echo "$PWD/.venv-hosted-ci/bin" >> "$GITHUB_PATH"' in expose_step["run"]
    audit_step = next(
        step for step in static_job["steps"] if step.get("name") == "Audit exact validator versions"
    )
    for required_audit in (
        "require_go_module_version tflint github.com/terraform-linters/tflint",
        "require_go_module_version helm helm.sh/helm/v3",
        "require_go_module_version kubeconform github.com/yannh/kubeconform",
        "require_go_module_version conftest github.com/open-policy-agent/conftest",
        "require_go_module_version sops github.com/getsops/sops/v3",
        "require_go_module_version age-keygen filippo.io/age",
        "require_python_distribution ansible-core",
        "require_python_distribution ansible-lint",
        "require_python_distribution checkov",
        "collection list community.general",
        "collection list community.library_inventory_filtering_v1",
        "collection list community.postgresql",
        "collection list community.docker",
        "require_output_version trivy",
        "require_output_version shellcheck",
        "require_output_version oras",
    ):
        assert required_audit in audit_step["run"]
    assert set(parsed["jobs"]) == {"static", "external-blackbox"}
    blackbox_job = parsed["jobs"]["external-blackbox"]
    assert blackbox_job["if"] == (
        "github.event_name == 'schedule' || "
        "(github.event_name == 'workflow_dispatch' && inputs.external_blackbox)"
    )
    assert 'uvx --from "ruff==${RUFF_VERSION}"' in validator
    assert 'uvx --from "mypy==${MYPY_VERSION}"' in validator
    assert 'uv run --frozen pytest -q "${repo_root}"/tests/test_hosted_*.py' in validator
    assert '(cd "${repo_root}" && uv lock --check)' in validator
    assert 'terraform_bin="$(resolve_executable "${TERRAFORM_BIN:-terraform}")"' in validator
    assert (
        '"${helm_bin}" lint "${infra_dir}/helm/platform" --strict \\\n  --namespace exomem-platform'
    ) in validator
    assert "--skip-dirs '**/__pycache__'" in validator
    assert '--skip-dirs charts "${repo_root}"' in validator
    for action_line in (
        line.strip() for line in workflow.splitlines() if line.strip().startswith("- uses:")
    ):
        assert len(action_line.rsplit("@", 1)[1].split()[0]) == 40


def test_hosted_validator_canonicalizes_relative_terraform_binary(tmp_path: Path) -> None:
    require_posix_executable_scripts()
    terraform = tmp_path / "terraform"
    _write_executable(terraform, "#!/usr/bin/env bash\nexit 0\n")
    validator = INFRA / "scripts/validate.sh"

    resolved = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; resolve_executable ./terraform',
            "bash",
            str(validator),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert resolved.returncode == 0, resolved.stderr
    assert resolved.stdout.strip() == str(terraform)


def test_k3s_snapshot_and_break_glass_contract_is_off_host_and_versioned() -> None:
    config = (INFRA / "ansible/roles/k3s/templates/config.yaml.j2").read_text(encoding="utf-8")
    matrix = json.loads((INFRA / "contracts/secret-destinations-v1.json").read_text())
    destinations = matrix["secrets"]["k3s_server_token"]["destinations"]

    assert "etcd-s3: true" in config
    assert "etcd-s3-skip-ssl-verify: false" in config
    assert "secrets-encryption: true" in config

    # The folder must match the prefix the B2 keys permit. It did not, and every
    # upload AND every restore listing was rejected as `not entitled` for the
    # life of the cluster — silently, because the only signal was a journal line.
    # Pinning the folder as a literal string is what let the two drift apart, so
    # cross-check them instead: this assertion fails on either side changing
    # alone.
    storage = (INFRA / "terraform/durability/storage.tf").read_text(encoding="utf-8")
    folder = re.search(r"^etcd-s3-folder:\s*(\S+)\s*$", config, re.MULTILINE)
    assert folder is not None, "etcd-s3-folder is not set"
    for key in ("etcd_snapshot_upload", "etcd_snapshot_restore"):
        block = storage.split(f'resource "b2_application_key" "{key}"', 1)[1].split(
            "\nresource ", 1
        )[0]
        prefix = re.search(r'name_prefix\s*=\s*"([^"]+)"', block)
        assert prefix is not None, key
        assert prefix.group(1) == f"{folder.group(1)}/", (
            f"{key} permits {prefix.group(1)!r} but k3s writes to "
            f"{folder.group(1)!r}/ — B2 will reject every object as 'not entitled'"
        )
    assert set(destinations) == {
        "ansible.hosted-node.k3s-server-token.active",
        "escrow.k3s-server-token.active",
    }
    escrow = destinations["escrow.k3s-server-token.active"]
    assert escrow == {
        "kind": "sops_escrow",
        "slot": "active",
        "target": "infra/secrets/escrow/k3s-server-token.{version}.sops.json",
        "secret_key": "token",
    }


def test_convergence_runner_requires_two_successful_runs_and_zero_second_run_changes(
    tmp_path: Path,
) -> None:
    runner = INFRA / "scripts/verify_ansible_convergence.py"
    fake = tmp_path / "ansible-with-sops"
    calls = tmp_path / "calls"
    _write_executable(
        fake,
        """#!/usr/bin/env python3
import json
import os
import pathlib

calls = pathlib.Path(os.environ['CALLS'])
count = int(calls.read_text()) + 1 if calls.exists() else 1
calls.write_text(str(count))
changed = 4 if count == 1 else int(os.environ.get('SECOND_CHANGED', '0'))
print(json.dumps({'stats': {'exomem-alpha': {
    'changed': changed, 'failures': 0, 'unreachable': 0, 'ok': 12, 'skipped': 0
}}}))
""",
    )
    inventory = tmp_path / "inventory.json"
    inventory.write_text("{}", encoding="utf-8")
    inventory.chmod(0o600)
    secret = tmp_path / "secret.v1.sops.json"
    secret.write_text('{"sops": {"age": []}}', encoding="utf-8")

    command = [
        sys.executable,
        str(runner),
        "--runner",
        str(fake),
        "--inventory",
        str(inventory),
        "--vars",
        str(secret),
    ]
    result = subprocess.run(
        command,
        cwd=ROOT,
        env={**os.environ, "CALLS": str(calls)},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "Ansible convergence verified: second run changed=0\n"
    assert calls.read_text() == "2"

    calls.unlink()
    failed = subprocess.run(
        command,
        cwd=ROOT,
        env={**os.environ, "CALLS": str(calls), "SECOND_CHANGED": "1"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert failed.returncode == 2
    assert "second Ansible run changed 1 task" in failed.stderr


def test_static_secret_application_validates_exact_destination_before_kubectl(
    tmp_path: Path,
) -> None:
    require_posix_executable_scripts()
    script = INFRA / "scripts/apply_sops_secret.py"
    artifact = tmp_path / "hosted-scheduler.v2.sops.json"
    artifact.write_text('{"sops": {"age": []}}', encoding="utf-8")
    matrix = tmp_path / "matrix.json"
    matrix.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "secrets": {
                    "hosted_scheduler_secret": {
                        "destinations": {
                            "k3s.scheduler.active": {
                                "kind": "sops_k8s_secret",
                                "slot": "active",
                                "target": str(artifact),
                                "namespace": "exomem-platform",
                                "kubernetes_secret": "exomem-hosted-scheduler",
                                "key": "secret",
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    fake_sops = tmp_path / "sops"
    fake_kubectl = tmp_path / "kubectl"
    kubectl_input = tmp_path / "applied.json"
    _write_executable(
        fake_sops,
        """#!/usr/bin/env python3
import json
print(json.dumps({
  'apiVersion': 'v1', 'kind': 'Secret',
  'metadata': {'name': 'exomem-hosted-scheduler', 'namespace': 'exomem-platform',
    'labels': {'app.kubernetes.io/managed-by': 'exomem-secret-handoff',
               'exomem.io/secret-version': 'v2'}},
  'type': 'Opaque', 'stringData': {'secret': 'must-not-be-printed'}
}))
""",
    )
    _write_executable(
        fake_kubectl,
        """#!/usr/bin/env python3
import os
import pathlib
import sys
if len(sys.argv) > 1 and sys.argv[1] == 'get':
    print('secret')
    raise SystemExit(0)
pathlib.Path(os.environ['KUBECTL_INPUT']).write_bytes(sys.stdin.buffer.read())
""",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--matrix",
            str(matrix),
            "--destination",
            "k3s.scheduler.active",
            "--artifact",
            str(artifact),
            "--sops",
            str(fake_sops),
            "--kubectl",
            str(fake_kubectl),
        ],
        cwd=ROOT,
        env={**os.environ, "KUBECTL_INPUT": str(kubectl_input)},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "Applied exomem-platform/exomem-hosted-scheduler at v2\n"
    assert "must-not-be-printed" not in result.stdout + result.stderr
    applied = json.loads(kubectl_input.read_text(encoding="utf-8"))
    assert "stringData" not in applied
    assert applied["data"] == {"secret": base64.b64encode(b"must-not-be-printed").decode()}


def test_sops_ciphertext_validator_binds_every_leaf_to_one_destination(
    tmp_path: Path,
) -> None:
    module = _load("infra/scripts/validate_sops_ciphertext.py", "validate_sops_ciphertext_test")
    fixture_root = ROOT / "tests/fixtures/hosted-sops"
    fixture = fixture_root / "cloudflared-token.v1.sops.json"
    matrix = fixture_root / "secret-destinations-v1.json"
    assert module.validate(matrix_path=matrix, artifacts=[fixture], root=ROOT) == 1

    repository = tmp_path / "repository"
    target = repository / "infra/secrets/platform/cloudflared-token.v1.sops.json"
    target.parent.mkdir(parents=True)
    document = json.loads(fixture.read_text(encoding="utf-8"))
    document["metadata"]["namespace"] = "exomem-platform"
    target.write_text(json.dumps(document), encoding="utf-8")
    test_matrix = repository / "matrix.json"
    test_matrix.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "secrets": {
                    "cloudflared": {
                        "destinations": {
                            "k3s.cloudflared.active": {
                                "kind": "sops_k8s_secret",
                                "slot": "active",
                                "target": "infra/secrets/platform/cloudflared-token.{version}.sops.json",
                                "namespace": "exomem-platform",
                                "kubernetes_secret": "exomem-cloudflared-token",
                                "key": "token",
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="plaintext payload leaf"):
        module.validate(matrix_path=test_matrix, artifacts=[target], root=repository)

    document["metadata"]["namespace"] = fixture_document = json.loads(
        fixture.read_text(encoding="utf-8")
    )["metadata"]["namespace"]
    assert fixture_document.startswith("ENC[")
    document["stringData"]["decoy"] = document["stringData"]["token"]
    target.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(RuntimeError, match="stringData shape"):
        module.validate(matrix_path=test_matrix, artifacts=[target], root=repository)


@pytest.mark.parametrize(
    "fields,extra_live_key,expected_success",
    [
        ({"current": "hidden-alpha", "currentVersion": "v2"}, "", True),
        (
            {
                "current": "hidden-beta",
                "currentVersion": "v2",
                "previous": "hidden-alpha",
                "previousVersion": "v1",
            },
            "",
            True,
        ),
        ({"current": "hidden-beta", "currentVersion": "v3"}, "previous", False),
    ],
)
def test_bundle_apply_uses_data_and_checks_live_field_set(
    tmp_path: Path, fields: dict[str, str], extra_live_key: str, expected_success: bool
) -> None:
    require_posix_executable_scripts()
    artifact = tmp_path / "bundle.v2.sops.json"
    artifact.write_text('{"sops": {}}', encoding="utf-8")
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
                                "target": str(artifact),
                                "namespace": "exomem-platform",
                                "kubernetes_secret": "bundle",
                                "key_sets": [
                                    ["current", "currentVersion"],
                                    ["current", "currentVersion", "previous", "previousVersion"],
                                ],
                            }
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    plaintext = tmp_path / "plaintext.json"
    plaintext.write_text(
        json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": "bundle",
                    "namespace": "exomem-platform",
                    "labels": {
                        "app.kubernetes.io/managed-by": "exomem-secret-handoff",
                        "exomem.io/secret-version": "v2",
                    },
                },
                "type": "Opaque",
                "stringData": fields,
            }
        ),
        encoding="utf-8",
    )
    fake_sops = tmp_path / "sops"
    _write_executable(
        fake_sops,
        "#!/usr/bin/env python3\nimport os, pathlib, sys\nsys.stdout.buffer.write(pathlib.Path(os.environ['PLAINTEXT']).read_bytes())\n",
    )
    fake_kubectl = tmp_path / "kubectl"
    applied_path = tmp_path / "applied.json"
    _write_executable(
        fake_kubectl,
        """#!/usr/bin/env python3
import json, os, pathlib, sys
if sys.argv[1] == 'apply':
    pathlib.Path(os.environ['APPLIED']).write_bytes(sys.stdin.buffer.read())
else:
    keys = list(json.loads(pathlib.Path(os.environ['APPLIED']).read_text())['data'])
    if os.environ['EXTRA_LIVE_KEY']:
        keys.append(os.environ['EXTRA_LIVE_KEY'])
    print('\\n'.join(keys))
""",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(INFRA / "scripts/apply_sops_secret.py"),
            "--matrix",
            str(matrix),
            "--destination",
            "k3s.bundle.active",
            "--artifact",
            str(artifact),
            "--sops",
            str(fake_sops),
            "--kubectl",
            str(fake_kubectl),
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PLAINTEXT": str(plaintext),
            "APPLIED": str(applied_path),
            "EXTRA_LIVE_KEY": extra_live_key,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) == expected_success
    assert "stringData" not in json.loads(applied_path.read_text())
    assert json.loads(applied_path.read_text())["data"] == {
        key: base64.b64encode(value.encode()).decode() for key, value in fields.items()
    }
    assert all(
        value not in result.stdout + result.stderr
        for key, value in fields.items()
        if key in {"current", "previous"}
    )
    if not expected_success:
        assert "verification failed" in result.stderr
        assert "Applied" not in result.stdout


def test_bundle_ciphertext_requires_exact_encrypted_field_set(tmp_path: Path) -> None:
    module = _load("infra/scripts/validate_sops_ciphertext.py", "bundle_ciphertext_validator")
    repository = tmp_path / "repository"
    target = repository / "infra/secrets/platform/bundle.v2.sops.json"
    target.parent.mkdir(parents=True)
    matrix = repository / "matrix.json"
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
                                "target": "infra/secrets/platform/bundle.{version}.sops.json",
                                "namespace": "exomem-platform",
                                "kubernetes_secret": "bundle",
                                "key_sets": [
                                    ["current", "currentVersion"],
                                    ["current", "currentVersion", "previous", "previousVersion"],
                                ],
                            }
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    fixture = json.loads(
        (ROOT / "tests/fixtures/hosted-sops/cloudflared-token.v1.sops.json").read_text()
    )
    encrypted = fixture["stringData"]["token"]
    fixture["stringData"] = {"current": encrypted, "currentVersion": encrypted}
    target.write_text(json.dumps(fixture), encoding="utf-8")
    assert module.validate(matrix_path=matrix, artifacts=[target], root=repository) == 1

    fixture["stringData"]["previous"] = encrypted
    target.write_text(json.dumps(fixture), encoding="utf-8")
    with pytest.raises(RuntimeError, match="stringData shape"):
        module.validate(matrix_path=matrix, artifacts=[target], root=repository)

    fixture["stringData"]["previousVersion"] = "unsealed-version"
    target.write_text(json.dumps(fixture), encoding="utf-8")
    with pytest.raises(RuntimeError, match="plaintext payload leaf"):
        module.validate(matrix_path=matrix, artifacts=[target], root=repository)

    fixture["stringData"] = {"current": encrypted, "currentVersion": encrypted}
    duplicate = json.dumps(fixture).replace(
        f'"current": "{encrypted}"',
        f'"current": "unsealed-bundle-value", "current": "{encrypted}"',
        1,
    )
    target.write_text(duplicate, encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid"):
        module.validate(matrix_path=matrix, artifacts=[target], root=repository)


@pytest.mark.parametrize(
    "change",
    [
        "half-previous",
        "unknown-key",
        "nested-value",
        "newline-value",
        "placeholder",
        "wrong-name",
        "duplicate-key",
    ],
)
def test_bundle_apply_rejects_malformed_decrypted_document(change: str) -> None:
    module = _load("infra/scripts/apply_sops_secret.py", "bundle_apply_shape_test")
    destination = module.Destination(
        target="bundle.{version}.sops.json",
        namespace="exomem-platform",
        secret_name="bundle",
        key=None,
        key_sets=(
            frozenset({"current", "currentVersion"}),
            frozenset({"current", "currentVersion", "previous", "previousVersion"}),
        ),
    )
    document = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "bundle",
            "namespace": "exomem-platform",
            "labels": {
                "app.kubernetes.io/managed-by": "exomem-secret-handoff",
                "exomem.io/secret-version": "v2",
            },
        },
        "type": "Opaque",
        "stringData": {"current": "hidden-key", "currentVersion": "2"},
    }
    if change == "half-previous":
        document["stringData"]["previous"] = "hidden-previous"
    elif change == "unknown-key":
        document["stringData"]["extra"] = "hidden-extra"
    elif change == "nested-value":
        document["stringData"]["current"] = {"nested": "hidden-key"}
    elif change == "newline-value":
        document["stringData"]["current"] = "hidden\nkey"
    elif change == "placeholder":
        document["stringData"]["current"] = "[REDACTED]"
    elif change == "wrong-name":
        document["metadata"]["name"] = "other"
    raw = json.dumps(document, separators=(",", ":")).encode()
    if change == "duplicate-key":
        raw = raw.replace(b'"current":"hidden-key"', b'"current":"hidden-key","current":"other"')
    with pytest.raises(module.SecretApplyError, match="invalid Kubernetes shape") as error:
        module._validate_plaintext(raw, destination, "v2")
    assert "hidden" not in str(error.value)


def test_scalar_apply_rejects_invalid_unicode_without_exposing_value() -> None:
    module = _load("infra/scripts/apply_sops_secret.py", "scalar_apply_unicode_test")
    destination = module.Destination(
        target="scalar.{version}.sops.json",
        namespace="exomem-platform",
        secret_name="scalar",
        key="secret",
    )
    raw = (
        b'{"apiVersion":"v1","kind":"Secret","metadata":{"name":"scalar",'
        b'"namespace":"exomem-platform","labels":{"app.kubernetes.io/managed-by":'
        b'"exomem-secret-handoff","exomem.io/secret-version":"v2"}},'
        b'"type":"Opaque","stringData":{"secret":"\\ud800"}}'
    )
    with pytest.raises(module.SecretApplyError, match="invalid Kubernetes shape"):
        module._validate_plaintext(raw, destination, "v2")




def test_active_secret_registry_is_signed_complete_and_explicit(tmp_path: Path) -> None:
    module = _load("infra/scripts/apply_active_sops_secrets.py", "apply_active_sops_secrets_test")
    matrix_path = tmp_path / "matrix.json"
    artifact_v1 = tmp_path / "secret.v1.sops.json"
    artifact_v2 = tmp_path / "secret.v2.sops.json"
    artifact_v1.write_text('{"sops": {}}', encoding="utf-8")
    artifact_v2.write_text('{"sops": {}}', encoding="utf-8")
    matrix = {
        "schema_version": 1,
        "secrets": {
            "example": {
                "destinations": {
                    "k3s.example.active": {
                        "kind": "sops_k8s_secret",
                        "slot": "active",
                        "target": str(tmp_path / "secret.{version}.sops.json"),
                        "namespace": "exomem-platform",
                        "kubernetes_secret": "example",
                        "key": "value",
                    }
                }
            }
        },
    }
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    private_key = Ed25519PrivateKey.generate()
    public_key_path = tmp_path / "registry-public.pem"
    public_key_path.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    public_key_path.chmod(0o644)
    public_key_id = hashlib.sha256(
        private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    ).hexdigest()
    trust_contract_path = tmp_path / "active-secret-registry-trust.json"
    trust_contract_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "algorithm": "ed25519",
                "public_key_id": public_key_id,
                "private_key_custody": "secret-release-custodian-only",
            }
        ),
        encoding="utf-8",
    )

    def write_registry(version: str, artifact: Path) -> Path:
        unsigned = {
            "schema_version": 1,
            "matrix_sha256": hashlib.sha256(matrix_path.read_bytes()).hexdigest(),
            "destinations": {
                "k3s.example.active": {
                    "secret": "example",
                    "version": version,
                    "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                }
            },
        }
        registry = _signed_receipt(unsigned, private_key)
        path = tmp_path / "active-secret-registry.json"
        path.write_text(json.dumps(registry), encoding="utf-8")
        path.chmod(0o644)
        _write_active_secret_completion(path, public_key_path)
        return path

    registry_path = write_registry("v2", artifact_v2)
    entries = module.load_registry(
        matrix_path=matrix_path,
        registry_path=registry_path,
        public_key_path=public_key_path,
        trust_contract_path=trust_contract_path,
    )
    assert [(item.destination, item.version, item.artifact) for item in entries] == [
        ("k3s.example.active", "v2", artifact_v2)
    ]

    tampered = json.loads(registry_path.read_text(encoding="utf-8"))
    tampered["destinations"]["k3s.example.active"]["version"] = "v1"
    registry_path.write_text(json.dumps(tampered), encoding="utf-8")
    _write_active_secret_completion(registry_path, public_key_path)
    with pytest.raises(module.ActiveSecretRegistryError, match="signature"):
        module.load_registry(
            matrix_path=matrix_path,
            registry_path=registry_path,
            public_key_path=public_key_path,
            trust_contract_path=trust_contract_path,
        )

    incomplete = _signed_receipt(
        {
            "schema_version": 1,
            "matrix_sha256": hashlib.sha256(matrix_path.read_bytes()).hexdigest(),
            "destinations": {},
        },
        private_key,
    )
    registry_path.write_text(json.dumps(incomplete), encoding="utf-8")
    _write_active_secret_completion(registry_path, public_key_path)
    with pytest.raises(module.ActiveSecretRegistryError, match="exact active destination set"):
        module.load_registry(
            matrix_path=matrix_path,
            registry_path=registry_path,
            public_key_path=public_key_path,
            trust_contract_path=trust_contract_path,
        )

    substituted_private_key = Ed25519PrivateKey.generate()
    public_key_path.write_bytes(
        substituted_private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    _write_active_secret_completion(write_registry("v2", artifact_v2), public_key_path)
    with pytest.raises(module.ActiveSecretRegistryError, match="not trusted"):
        module.load_registry(
            matrix_path=matrix_path,
            registry_path=write_registry("v2", artifact_v2),
            public_key_path=public_key_path,
            trust_contract_path=trust_contract_path,
        )


def test_active_secret_selection_is_complete_and_the_signer_publishes_a_verified_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The signer verifies its staged registry through
    # `/proc/self/fd/<dirfd>/<name>` before it publishes, so that what it
    # checks is what it links rather than whatever the name resolves to a
    # moment later. That is a Linux facility with no portable equivalent; off
    # procfs the verification raises, `main` maps it to exit 2, and the
    # refusal never reaches pytest as an error it could classify.
    require_procfs_descriptor_paths()
    signer = _load(
        "infra/scripts/sign_active_secret_registry.py", "sign_active_secret_registry_test"
    )
    applier = _load(
        "infra/scripts/apply_active_sops_secrets.py", "apply_active_sops_secrets_signer_test"
    )
    matrix = json.loads((INFRA / "contracts/secret-destinations-v1.json").read_text())
    selection = json.loads((INFRA / "contracts/active-secret-selection-v1.json").read_text())
    expected = {
        destination_id
        for secret in matrix["secrets"].values()
        for destination_id, destination in secret["destinations"].items()
        if destination.get("kind") == "sops_k8s_secret" and destination.get("slot") == "active"
    }
    assert {name for name in expected if name.startswith("k3s.cloud.")} == {
        f"k3s.cloud.{name}.active"
        for name in (
            "exomem-cellctl-database-dsn",
            "exomem-cloud-gateway-database",
            "exomem-cloud-gateway-control-plane-key",
            "exomem-cloud-cell-token-key",
            "exomem-cloud-backup-master-key",
            "exomem-cloud-b2-key-management",
            "exomem-cloud-cell-data-staging",
            "exomem-cloud-cell-data-backup-list",
            "exomem-cloud-hetzner-read-token",
            "exomem-cloudflare-dns-token",
            "exomem-cloud-volume-encryption",
        )
    }
    assert selection["schema_version"] == 1
    assert set(selection["destinations"]) == expected
    assert all(
        re.fullmatch(r"v[1-9][0-9]*", version) for version in selection["destinations"].values()
    )
    assert all(
        (
            ROOT / destination["target"].format(version=selection["destinations"][destination_id])
        ).is_file()
        for secret in matrix["secrets"].values()
        for destination_id, destination in secret["destinations"].items()
        if destination.get("kind") == "sops_k8s_secret" and destination.get("slot") == "active"
    )

    private_key = Ed25519PrivateKey.generate()
    raw_public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    trust = json.loads((INFRA / "contracts/active-secret-registry-v1.json").read_text())
    trust["public_key_id"] = hashlib.sha256(raw_public_key).hexdigest()
    trust_path = tmp_path / "trust.json"
    trust_path.write_text(json.dumps(trust), encoding="utf-8")
    output = tmp_path / "registry-output"
    output.mkdir(mode=0o700)
    key = bytearray(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    monkeypatch.setattr(
        sys, "stdin", type("Input", (), {"buffer": type("Buffer", (), {"read": lambda _: key})()})()
    )
    registry_path = output / "registry.json"
    public_key_path = output / "registry-public.pem"

    assert (
        signer.main(
            [
                "--matrix",
                str(INFRA / "contracts/secret-destinations-v1.json"),
                "--selection",
                str(INFRA / "contracts/active-secret-selection-v1.json"),
                "--trust-contract",
                str(trust_path),
                "--private-key-stdin",
                "--registry-output",
                str(registry_path),
                "--public-key-output",
                str(public_key_path),
            ]
        )
        == 0
    )
    assert not any(key)
    assert (
        len(
            applier.load_registry(
                matrix_path=INFRA / "contracts/secret-destinations-v1.json",
                registry_path=registry_path,
                public_key_path=public_key_path,
                trust_contract_path=trust_path,
            )
        )
        == len(expected)
    )


def test_active_secret_registry_signer_rejects_unsafe_inputs_and_leaves_no_partial_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The signer verifies its staged registry through
    # `/proc/self/fd/<dirfd>/<name>` before it publishes, so that what it
    # checks is what it links rather than whatever the name resolves to a
    # moment later. That is a Linux facility with no portable equivalent; off
    # procfs the verification raises, `main` maps it to exit 2, and the
    # refusal never reaches pytest as an error it could classify.
    require_procfs_descriptor_paths()
    signer = _load(
        "infra/scripts/sign_active_secret_registry.py", "active_secret_registry_signer_safety"
    )
    private_key = Ed25519PrivateKey.generate()
    public = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    trust_path = tmp_path / "trust.json"
    trust_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "algorithm": "ed25519",
                "public_key_id": hashlib.sha256(public).hexdigest(),
                "private_key_custody": "secret-release-custodian-only",
            }
        ),
        encoding="utf-8",
    )
    malformed = bytearray(b"-----BEGIN OPENSSH PRIVATE KEY-----\nnot-a-key\n")
    with pytest.raises(signer.ActiveSecretRegistrySigningError, match="key is invalid"):
        signer.load_private_key(malformed, trust_path)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version": 1, "schema_version": 1}', encoding="utf-8")
    with pytest.raises(signer.ActiveSecretRegistrySigningError, match="input JSON is invalid"):
        signer._strict_json(duplicate, "input")

    matrix_path = INFRA / "contracts/secret-destinations-v1.json"
    matrix_raw, destinations = signer.load_active_destinations(matrix_path)
    selection = signer.load_selection(
        INFRA / "contracts/active-secret-selection-v1.json", set(destinations)
    )
    registry = signer.build_registry(matrix_raw, destinations, selection, ROOT, private_key)
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    registry_path = output / "registry.json"
    public_path = output / "public.pem"
    real_link = signer.os.link

    def fail_second_link(source: str, target: str, **kwargs: object) -> None:
        if target == public_path.name:
            raise OSError("injected second publication failure")
        real_link(source, target, **kwargs)

    monkeypatch.setattr(signer.os, "link", fail_second_link)
    with pytest.raises(signer.ActiveSecretRegistrySigningError, match="publication failed"):
        signer.publish_verified_pair(
            matrix_path=matrix_path,
            trust_path=trust_path,
            registry_output=registry_path,
            public_key_output=public_path,
            registry=registry,
            private_key=private_key,
        )
    assert not registry_path.exists()
    assert not public_path.exists()

    registry_path.write_text("already-present", encoding="utf-8")
    with pytest.raises(signer.ActiveSecretRegistrySigningError, match="already exists"):
        signer.publish_verified_pair(
            matrix_path=matrix_path,
            trust_path=trust_path,
            registry_output=registry_path,
            public_key_output=public_path,
            registry=registry,
            private_key=private_key,
        )
    assert registry_path.read_text(encoding="utf-8") == "already-present"

    monkeypatch.setattr(signer.os, "link", real_link)
    swapped_output = tmp_path / "swapped-output"
    swapped_output.mkdir(mode=0o700)
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    moved = tmp_path / "moved-output"
    swapped_registry = swapped_output / "registry.json"
    swapped_public = swapped_output / "public.pem"

    def swap_parent_after_first_link(source: str, target: str, **kwargs: object) -> None:
        real_link(source, target, **kwargs)
        if target == swapped_registry.name:
            swapped_output.rename(moved)
            swapped_output.symlink_to(attacker, target_is_directory=True)

    monkeypatch.setattr(signer.os, "link", swap_parent_after_first_link)
    signer.publish_verified_pair(
        matrix_path=matrix_path,
        trust_path=trust_path,
        registry_output=swapped_registry,
        public_key_output=swapped_public,
        registry=registry,
        private_key=private_key,
    )
    assert not any(attacker.iterdir())
    assert (moved / "registry.json.complete").is_file()


def test_active_secret_registry_signer_rejects_a_parent_swap_during_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signer = _load("infra/scripts/sign_active_secret_registry.py", "active_registry_openat_test")
    stable = tmp_path / "stable"
    middle = stable / "middle"
    output = middle / "output"
    output.mkdir(parents=True, mode=0o700)
    output.chmod(0o700)
    attacker = tmp_path / "attacker"
    (attacker / "output").mkdir(parents=True, mode=0o700)
    (attacker / "output").chmod(0o700)
    real_open = signer.os.open

    def swap_before_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if path == "middle":
            middle.rename(stable / "moved")
            middle.symlink_to(attacker, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(signer.os, "open", swap_before_open)
    with pytest.raises(signer.ActiveSecretRegistrySigningError, match="directory"):
        signer._open_output_directory(output / "registry.json", output / "public.pem")


def test_active_secret_registry_signer_parser_never_echoes_mistaken_key_material(
    capsys: pytest.CaptureFixture[str],
) -> None:
    signer = _load(
        "infra/scripts/sign_active_secret_registry.py", "active_secret_registry_signer_parser_test"
    )
    sentinel = "PRIVATE-KEY-SENTINEL-MUST-NOT-PRINT"

    assert signer.main([f"--private-key-stdin={sentinel}"]) == 2
    assert signer.main(["--private-key", sentinel]) == 2
    captured = capsys.readouterr()
    assert sentinel not in captured.out + captured.err


def test_active_secret_registry_signer_rejects_ambiguous_artifacts_and_invalid_matrix_targets(
    tmp_path: Path,
) -> None:
    signer = _load(
        "infra/scripts/sign_active_secret_registry.py", "active_secret_registry_signer_matrix_test"
    )
    artifact = tmp_path / "artifact.v1.sops.json"
    artifact.write_text(
        '{"stringData":{"url":"first","url":"second"},"sops":{},"sops":{}}',
        encoding="utf-8",
    )
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "secrets": {
                    "example": {
                        "destinations": {
                            "k3s.example.active": {
                                "kind": "sops_k8s_secret",
                                "slot": "active",
                                "target": "artifact.{version}.sops.json",
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    matrix_raw, destinations = signer.load_active_destinations(matrix_path)
    private_key = Ed25519PrivateKey.generate()
    with pytest.raises(signer.ActiveSecretRegistrySigningError, match="input JSON"):
        signer.build_registry(
            matrix_raw,
            destinations,
            {"k3s.example.active": "v1"},
            tmp_path,
            private_key,
        )

    invalid_matrix = tmp_path / "invalid-matrix.json"
    invalid_matrix.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "secrets": {
                    "first": {
                        "destinations": {
                            "shared.destination": {
                                "kind": "sops_k8s_secret",
                                "slot": "active",
                                "target": "../escape.{version}.sops.json",
                            }
                        }
                    },
                    "second": {
                        "destinations": {
                            "shared.destination": {
                                "kind": "sops_escrow",
                                "slot": "active",
                                "target": "/absolute.{version}.sops.json",
                            }
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(signer.ActiveSecretRegistrySigningError, match="matrix"):
        signer.load_active_destinations(invalid_matrix)


def test_active_secret_registry_loader_requires_a_completed_pair(tmp_path: Path) -> None:
    module = _load(
        "infra/scripts/apply_active_sops_secrets.py", "active_secret_registry_completion_test"
    )
    artifact = tmp_path / "secret.v1.sops.json"
    artifact.write_text('{"sops": {}}', encoding="utf-8")
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "secrets": {
                    "example": {
                        "destinations": {
                            "k3s.example.active": {
                                "kind": "sops_k8s_secret",
                                "slot": "active",
                                "target": str(tmp_path / "secret.{version}.sops.json"),
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    private_key = Ed25519PrivateKey.generate()
    public = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    trust_path = tmp_path / "trust.json"
    trust_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "algorithm": "ed25519",
                "public_key_id": hashlib.sha256(public).hexdigest(),
                "private_key_custody": "secret-release-custodian-only",
            }
        ),
        encoding="utf-8",
    )
    public_path = tmp_path / "public.pem"
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    unsigned = {
        "schema_version": 1,
        "matrix_sha256": hashlib.sha256(matrix_path.read_bytes()).hexdigest(),
        "destinations": {
            "k3s.example.active": {
                "secret": "example",
                "version": "v1",
                "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }
        },
    }
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(_signed_receipt(unsigned, private_key)), encoding="utf-8")
    with pytest.raises(module.ActiveSecretRegistryError, match="completion"):
        module.load_registry(
            matrix_path=matrix_path,
            registry_path=registry_path,
            public_key_path=public_path,
            trust_contract_path=trust_path,
        )


def test_active_secret_registry_verify_only_never_starts_a_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The registry and its key must be non-writable regular files; the script
    # refuses anything it cannot prove that about, and reports it on stderr
    # rather than through the exception this suite's capability hook reads.
    require_posix_file_modes()
    module = _load(
        "infra/scripts/apply_active_sops_secrets.py", "apply_active_sops_secrets_verify_test"
    )
    artifact = tmp_path / "secret.v1.sops.json"
    artifact.write_text('{"sops": {}}', encoding="utf-8")
    matrix_path = tmp_path / "matrix.json"
    matrix = {
        "schema_version": 1,
        "secrets": {
            "example": {
                "destinations": {
                    "k3s.example.active": {
                        "kind": "sops_k8s_secret",
                        "slot": "active",
                        "target": str(tmp_path / "secret.{version}.sops.json"),
                    }
                }
            }
        },
    }
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    private_key = Ed25519PrivateKey.generate()
    public = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    key_id = hashlib.sha256(public).hexdigest()
    trust_path = tmp_path / "trust.json"
    trust_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "algorithm": "ed25519",
                "public_key_id": key_id,
                "private_key_custody": "secret-release-custodian-only",
            }
        ),
        encoding="utf-8",
    )
    public_key_path = tmp_path / "public.pem"
    public_key_path.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    unsigned = {
        "schema_version": 1,
        "matrix_sha256": hashlib.sha256(matrix_path.read_bytes()).hexdigest(),
        "destinations": {
            "k3s.example.active": {
                "secret": "example",
                "version": "v1",
                "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }
        },
    }
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(_signed_receipt(unsigned, private_key)), encoding="utf-8")
    _write_active_secret_completion(registry_path, public_key_path)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: pytest.fail("subprocess"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "apply_active_sops_secrets.py",
            "--matrix",
            str(matrix_path),
            "--registry",
            str(registry_path),
            "--registry-public-key",
            str(public_key_path),
            "--trust-contract",
            str(trust_path),
            "--verify-only",
        ],
    )

    assert module.main() == 0
    assert (
        capsys.readouterr().out
        == f"Verified 1 active destinations with trusted public-key ID {key_id}\n"
    )












def test_runbook_index_is_complete_and_executable_by_default() -> None:
    contract = json.loads((INFRA / "contracts/runbooks-v1.json").read_text())
    required = {
        "backend",
        "deploy",
        "secrets",
        "node-replacement",
        "node-pool",
        "dedicated-host",
        "break-glass",
        "cell-deletion",
        "hosted-retirement",
    }
    assert set(contract["runbooks"]) == required
    for name, item in contract["runbooks"].items():
        path = ROOT / item["path"]
        assert path.is_file(), name
        text = path.read_text(encoding="utf-8")
        assert "## Preconditions" in text
        assert "## Verify" in text
        assert "```bash" in text
        if item["destructive"]:
            assert item["approval_flag"] in text


def test_secret_matrix_materializes_every_hosted_platform_workload_secret() -> None:
    matrix = json.loads(
        (INFRA / "contracts/secret-destinations-v1.json").read_text(encoding="utf-8")
    )
    materialized = {
        f"{destination['kubernetes_secret']}/{key}"
        for secret in matrix["secrets"].values()
        for destination in secret["destinations"].values()
        if destination["kind"] == "sops_k8s_secret"
        for key_set in (
            [[destination["key"]]] if "key" in destination else destination["key_sets"]
        )
        for key in key_set
    }
    required = {
        "exomem-cellctl-database-dsn/dsn",
        "exomem-cloud-gateway-database/url",
        "exomem-cloud-gateway-control-plane-key/key",
        "exomem-cloud-cell-token-key/current",
        "exomem-cloud-cell-token-key/currentVersion",
        "exomem-cloud-backup-master-key/keys",
        "exomem-cloud-backup-master-key/currentVersion",
        "exomem-cloud-b2-key-management/keyId",
        "exomem-cloud-b2-key-management/applicationKey",
        "exomem-key-custodian-b2/keyId",
        "exomem-key-custodian-b2/applicationKey",
        "exomem-cloud-cell-data-staging/keyId",
        "exomem-cloud-cell-data-staging/applicationKey",
        "exomem-cloud-cell-data-backup-list/keyId",
        "exomem-cloud-cell-data-backup-list/applicationKey",
        "exomem-cloud-hetzner-read-token/token",
        "exomem-cloudflare-dns-token/token",
        "exomem-cloud-volume-encryption/encryption-passphrase",
    }
    assert required <= materialized
    assert not any(value.startswith("exomem-provisioner-") for value in materialized)
    assert not any("database-backup" in value for value in materialized)

    terraform_outputs = {
        source["output"]
        for secret in matrix["secrets"].values()
        for source in secret["sources"]
        if source["kind"] == "terraform" and source["root"] == "durability"
    }
    assert {
        "etcd_snapshot_upload_application_key_id",
        "etcd_snapshot_upload_application_key",
        "etcd_snapshot_restore_application_key_id",
        "etcd_snapshot_restore_application_key",
    } <= terraform_outputs






def test_network_probe_plan_contains_every_denied_boundary() -> None:
    module = _load("infra/scripts/network_policy_probes.py", "network_policy_probes_test")
    plan = module.build_probe_plan(
        source_namespace="cell-alpha-test",
        peer_namespace="cell-beta-test",
        cell_service="cell-alpha",
        peer_service="cell-beta",
        neon_host="neon.invalid",
        b2_host="b2.invalid",
    )
    assert {probe.name for probe in plan} == {
        "cell-to-cell",
        "kubernetes-api",
        "neon",
        "b2",
        "cloud-metadata",
        "unlabelled-platform-ingress",
    }
    assert all(probe.expect_denied for probe in plan)
    assert next(probe for probe in plan if probe.name == "cell-to-cell").target.startswith(
        "http://cell-beta.cell-beta-test.svc.cluster.local:8765"
    )
    assert next(probe for probe in plan if probe.name == "cloud-metadata").target.startswith(
        "http://169.254.169.254"
    )
    manifest = module.render_probe_manifest(plan, run_id="abcdef123456")
    assert "automountServiceAccountToken: false" in manifest
    assert "exomem.io/network-probe" in manifest
    assert "exomem-deny-unlabelled-abcdef123456" in manifest
    assert "cell-to-cell" not in manifest

    targets = module.LiveTargets.from_document(
        {
            "schema_version": 1,
            "cluster_uid": "cluster-uid-1",
            "source": {
                "namespace": "cell-alpha-test",
                "service": "cell-alpha",
                "uid": "service-alpha-uid",
            },
            "peer": {
                "namespace": "cell-beta-test",
                "service": "cell-beta",
                "uid": "service-beta-uid",
            },
            "neon_host": "neon.example.com",
            "b2_host": "b2.example.com",
        }
    )
    assert targets.as_document()["cluster_uid"] == "cluster-uid-1"
    with pytest.raises(module.NetworkProbeError, match="incomplete"):
        module.LiveTargets.from_document({**targets.as_document(), "neon_host": "missing host!"})


def test_network_probe_executor_fails_if_any_denied_connection_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The probe executor runs `kubectl`; this stands one in with a shebang,
    # which Windows cannot execute as a program.
    require_posix_executable_scripts()
    module = _load("infra/scripts/network_policy_probes.py", "network_policy_executor_test")
    kubectl = tmp_path / "kubectl"
    calls = tmp_path / "calls.jsonl"
    _write_executable(
        kubectl,
        """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

call_path = pathlib.Path(os.environ['CALLS'])
with call_path.open('a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1] == 'create':
    sys.stdin.buffer.read()
if sys.argv[1] == 'delete' and os.environ.get('FAIL_CLEANUP') == '1':
    raise SystemExit(1)
if sys.argv[1] == 'get':
    kind = sys.argv[2]
    name = sys.argv[3]
    if kind == 'namespace':
        print(json.dumps({'metadata': {'uid': 'cluster-uid-1'}}))
    elif kind == 'service':
        uid = 'service-alpha-uid' if name == 'cell-alpha' else 'service-beta-uid'
        print(json.dumps({'metadata': {'uid': uid}, 'spec': {'clusterIP': '10.43.8.7'}}))
    elif kind == 'endpoints':
        print(json.dumps({'subsets': [{'addresses': [{'ip': '10.42.0.7'}]}]}))
    elif kind == 'jobs':
        print(json.dumps({'items': []}))
    elif kind == 'job':
        run_id = name[-12:]
        job_gets = sum(
            1 for line in call_path.read_text().splitlines()
            if json.loads(line)[:3] == ['get', 'job', name]
        )
        uid_prefix = 'stale-' if os.environ.get('STALE_UID') == '1' and job_gets > 1 else 'uid-'
        print(json.dumps({
            'metadata': {'uid': uid_prefix + name, 'labels': {'exomem.io/network-probe-run': run_id}},
            'status': {'startTime': '2026-07-14T10:00:00Z',
                       'completionTime': '2026-07-14T10:00:01Z', 'succeeded': 1,
                       'conditions': [{'type': 'Complete', 'status': 'True'}]}
        }))
if sys.argv[1] == 'wait' and os.environ.get('FAIL_POSITIVE') == '1' and 'network-positive' in sys.argv[-1]:
    raise SystemExit(1)
if sys.argv[1] == 'exec' and os.environ.get('ALLOW_TARGET') in sys.argv:
    raise SystemExit(23)
""",
    )
    plan = module.build_probe_plan(
        source_namespace="cell-alpha-test",
        peer_namespace="cell-beta-test",
        cell_service="cell-alpha",
        peer_service="cell-beta",
        neon_host="neon.invalid",
        b2_host="b2.invalid",
    )
    targets = module.LiveTargets.from_document(
        {
            "schema_version": 1,
            "cluster_uid": "cluster-uid-1",
            "source": {
                "namespace": "cell-alpha-test",
                "service": "cell-alpha",
                "uid": "service-alpha-uid",
            },
            "peer": {
                "namespace": "cell-beta-test",
                "service": "cell-beta",
                "uid": "service-beta-uid",
            },
            "neon_host": "neon.invalid",
            "b2_host": "b2.invalid",
        }
    )
    monkeypatch.setenv("CALLS", str(calls))
    monkeypatch.setenv("ALLOW_TARGET", "none")
    module.execute_probe_manifest(
        plan,
        str(kubectl),
        "cell-alpha",
        targets=targets,
        run_id="abcdef123456",
    )
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert sum(item[0] == "exec" for item in invocations) == 5
    assert sum(item[0] == "create" for item in invocations) == 2
    assert sum(item[0] == "wait" for item in invocations) == 2
    created_names = [item[-1] for item in invocations if item[0] == "wait"]
    assert all("abcdef123456" in name for name in created_names)
    assert invocations[0][0] == "delete"
    assert invocations[1][:2] == ["get", "jobs"]
    assert next(index for index, item in enumerate(invocations) if item[0] == "wait") < next(
        index for index, item in enumerate(invocations) if item[0] == "exec"
    )

    monkeypatch.setenv("ALLOW_TARGET", "neon.invalid")
    with pytest.raises(module.NetworkProbeError, match="neon"):
        module.execute_probe_manifest(
            plan,
            str(kubectl),
            "cell-alpha",
            targets=targets,
            run_id="123456abcdef",
        )

    monkeypatch.setenv("ALLOW_TARGET", "none")
    monkeypatch.setenv("FAIL_POSITIVE", "1")
    with pytest.raises(module.NetworkProbeError, match="network-positive"):
        module.execute_probe_manifest(
            plan,
            str(kubectl),
            "cell-alpha",
            targets=targets,
            run_id="fedcba654321",
        )
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    final_positive_wait = max(
        index
        for index, item in enumerate(invocations)
        if item[0] == "wait" and "network-positive" in item[-1]
    )
    assert not any(item[0] == "exec" for item in invocations[final_positive_wait + 1 :])

    monkeypatch.delenv("FAIL_POSITIVE")
    monkeypatch.setenv("STALE_UID", "1")
    with pytest.raises(module.NetworkProbeError, match="completion is not fresh"):
        module.execute_probe_manifest(
            plan,
            str(kubectl),
            "cell-alpha",
            targets=targets,
            run_id="0abcde123456",
        )

    monkeypatch.delenv("STALE_UID")
    monkeypatch.setenv("FAIL_CLEANUP", "1")
    with pytest.raises(module.NetworkProbeError, match="cleanup"):
        module.execute_probe_manifest(
            plan,
            str(kubectl),
            "cell-alpha",
            targets=targets,
            run_id="012345abcdef",
        )


def test_external_probe_never_returns_response_body_or_target() -> None:
    module = _load("infra/scripts/external_blackbox.py", "external_blackbox_test")

    class Response:
        status = 200
        body = b"credential filename note query private"
        headers = {"content-type": "application/json"}

        def geturl(self) -> str:
            return "https://secret-host.invalid/private/path"

    observation = module.observe(
        name="control",
        target="https://secret-host.invalid/private/path",
        fetch=lambda _target, _timeout: Response(),
        timeout_seconds=5,
    )
    rendered = json.dumps(observation.as_dict())
    assert observation.ok is True
    assert "secret-host" not in rendered
    assert "credential" not in rendered
    assert "body" not in rendered

    class Redirected(Response):
        def geturl(self) -> str:
            return "https://other.invalid/private/path"

    redirected = module.observe(
        name="control",
        target="https://secret-host.invalid/private/path",
        fetch=lambda _target, _timeout: Redirected(),
        timeout_seconds=5,
    )
    assert redirected.ok is False
    assert redirected.reason == "redirected"
    insecure = module.observe(
        name="control",
        target="http://secret-host.invalid/private/path",
        fetch=lambda _target, _timeout: Response(),
        timeout_seconds=5,
    )
    assert insecure.ok is False
    assert insecure.reason == "transport-failed"


def test_new_operator_scripts_are_executable() -> None:
    # The executable bit is the property under test, and Windows has none:
    # `os.stat` there reports 0666 or 0444 whatever git recorded.
    require_posix_file_modes()
    for name in (
        "verify_ansible_convergence.py",
        "apply_sops_secret.py",
        "network_policy_probes.py",
        "external_blackbox.py",
    ):
        mode = (INFRA / "scripts" / name).stat().st_mode
        assert stat.S_IMODE(mode) & stat.S_IXUSR, name
