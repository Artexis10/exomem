from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"


def _load(relative: str, name: str) -> ModuleType:
    path = ROOT / relative
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
    assert "openspec validate add-hosted-private-alpha-infrastructure --strict" in workflow
    assert 'UV_VERSION: "0.11.28"' in workflow
    assert 'PYTHON_VERSION: "3.13.5"' in workflow
    assert 'NODE_VERSION: "22.17.1"' in workflow
    assert "go install github.com/aquasecurity/trivy" not in workflow
    assert "https://github.com/aquasecurity/trivy/releases/download/" in workflow
    assert 'install -d -m 0755 "$HOME/.local/bin"' in workflow
    assert "${TRIVY_LINUX_AMD64_SHA256}" in workflow
    assert (
        "TRIVY_LINUX_AMD64_SHA256="
        "bbb64b9695866ce4a7a8f5c9592002c5961cab378577fa3f8a040df362b9b2ea"
    ) in tool_versions
    parsed = yaml.safe_load(workflow)
    assert parsed["jobs"]["static"]["name"] == "Offline static validation (not release proof)"
    release_job = parsed["jobs"]["release-proof"]
    assert release_job["needs"] == "static"
    assert "inputs.release_proof" in release_job["if"]
    assert "--fetch-substrate-fixture" in workflow
    assert "--probe-image" in workflow
    assert "--require-published" in workflow
    assert "substrate-gateway-contract-selection-v1.json" in workflow
    assert 'uvx --from "ruff==${RUFF_VERSION}"' in validator
    assert 'uvx --from "mypy==${MYPY_VERSION}"' in validator
    assert '(cd "${repo_root}" && uv lock --check)' in validator
    assert 'terraform_bin="$(resolve_executable "${TERRAFORM_BIN:-terraform}")"' in validator
    assert (
        '"${helm_bin}" lint "${infra_dir}/helm/platform" --strict \\\n  --namespace exomem-platform'
    ) in validator
    assert '--skip-dirs charts "${repo_root}"' in validator
    for action_line in (
        line.strip() for line in workflow.splitlines() if line.strip().startswith("- uses:")
    ):
        assert len(action_line.rsplit("@", 1)[1].split()[0]) == 40


def test_hosted_validator_canonicalizes_relative_terraform_binary(tmp_path: Path) -> None:
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


def test_hosted_provisioner_publish_workflow_is_source_bound_and_smoke_verified() -> None:
    workflow_path = ROOT / ".github/workflows/publish-hosted-provisioner.yml"
    workflow = workflow_path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(workflow)
    triggers = parsed.get("on", parsed.get(True))

    assert triggers["push"]["branches"] == ["main"]
    assert "workflow_dispatch" in triggers
    job = parsed["jobs"]["publish"]
    assert job["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "packages": "write",
    }
    assert "infra/provisioner/Dockerfile" in workflow
    assert "ghcr.io/artexis10/exomem-provisioner:${{ github.sha }}" in workflow
    assert "ghcr.io/artexis10/exomem-provisioner:latest" not in workflow
    assert "push: true" in workflow
    assert "provenance: mode=max" in workflow
    assert "sbom: true" in workflow
    assert "steps.build.outputs.digest" in workflow
    assert "infra/scripts/verify_provisioner_image.py" in workflow
    assert "--require-published" in workflow
    assert "infra/scripts/verify_hosted_release.py" in workflow
    assert "--fetch-substrate-fixture" in workflow
    assert "--probe-image" in workflow
    assert "substrate-gateway-contract-selection-v1.json" in workflow
    for action_line in (
        line.strip() for line in workflow.splitlines() if line.strip().startswith("- uses:")
    ):
        assert len(action_line.rsplit("@", 1)[1].split()[0]) == 40


def test_k3s_snapshot_and_break_glass_contract_is_off_host_and_versioned() -> None:
    config = (INFRA / "ansible/roles/k3s/templates/config.yaml.j2").read_text(encoding="utf-8")
    matrix = json.loads((INFRA / "contracts/secret-destinations-v1.json").read_text())
    destinations = matrix["secrets"]["k3s_server_token"]["destinations"]

    assert "etcd-s3: true" in config
    assert "etcd-s3-skip-ssl-verify: false" in config
    assert "etcd-s3-folder: exomem-private-alpha/etcd" in config
    assert "secrets-encryption: true" in config
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
    assert applied["stringData"] == {"secret": "must-not-be-printed"}


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


def test_rotation_retirement_gate_covers_every_independent_rotation(
    tmp_path: Path,
) -> None:
    module = _load("infra/scripts/rotation_gate.py", "rotation_gate_test")
    contract = json.loads((INFRA / "contracts/rotation-drills-v1.json").read_text())
    assert set(contract["rotations"]) == {
        "cloudflare-access",
        "cloudflare-tunnel",
        "provisioner-credential",
        "cell-credential",
        "hosted-scheduler",
        "provisioner-wrapping-key",
    }
    assert contract["receipt_authentication"] == {
        "algorithm": "ed25519",
        "domain": "exomem.rotation-drill-receipt.v1",
        "ttl_seconds": 86400,
        "private_key_custody": "drill-collector-only",
        "public_key_id": None,
        "verifier_material": "public-key-only",
    }
    receipt_private_key = Ed25519PrivateKey.generate()
    receipt_public_key = receipt_private_key.public_key()
    receipt_public_bytes = receipt_public_key.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    contract["receipt_authentication"]["public_key_id"] = hashlib.sha256(
        receipt_public_bytes
    ).hexdigest()
    assert not hasattr(module, "sign_receipt")
    for rotation_index, (name, rotation) in enumerate(contract["rotations"].items()):
        recorded_at = "2026-07-14T12:00:00Z"
        receipt_root = tmp_path / name
        receipt_root.mkdir()
        observations = {}
        unsigned_receipts: dict[Path, dict[str, object]] = {}
        for index, requirement in enumerate(rotation["retirement_requires"], start=1):
            unsigned_receipt = {
                "schema_version": 1,
                "issuer": "exomem-rotation-drill-v1",
                "receipt_id": f"123e4567-e89b-42d3-a456-{index:012d}",
                "drill_id": "123e4567-e89b-42d3-a456-426614174000",
                "rotation": name,
                "requirement": requirement,
                "old_version": "v1",
                "new_version": "v2",
                "observed_at": recorded_at,
                "expires_at": "2026-07-15T12:00:00Z",
                "passed": True,
            }
            receipt = _signed_receipt(
                unsigned_receipt,
                receipt_private_key,
                domain=b"exomem.rotation-drill-receipt.v1\0",
            )
            receipt_path = receipt_root / f"receipt-{index:02d}.json"
            unsigned_receipts[receipt_path] = unsigned_receipt
            raw = (json.dumps(receipt, separators=(",", ":"), sort_keys=True) + "\n").encode()
            receipt_path.write_bytes(raw)
            receipt_path.chmod(0o600)
            observations[requirement] = {
                "receipt_path": receipt_path.name,
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        evidence = {
            "schema_version": 2,
            "contract_sha256": module.contract_digest(contract),
            "drill_id": "123e4567-e89b-42d3-a456-426614174000",
            "recorded_at": recorded_at,
            "rotation": name,
            "old_version": "v1",
            "new_version": "v2",
            "observations": observations,
        }
        result = module.verify_evidence(
            contract,
            evidence,
            receipt_root=receipt_root,
            receipt_public_key=receipt_public_key,
            now=datetime(2026, 7, 14, 12, 1, tzinfo=UTC),
        )
        assert result.rotation == name
        assert result.new_version == "v2"

        if rotation_index == 0:
            substituted_private_key = Ed25519PrivateKey.generate()
            for _requirement, reference in observations.items():
                receipt_path = receipt_root / reference["receipt_path"]
                raw = (
                    json.dumps(
                        _signed_receipt(
                            unsigned_receipts[receipt_path],
                            substituted_private_key,
                            domain=b"exomem.rotation-drill-receipt.v1\0",
                        ),
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    + "\n"
                ).encode()
                receipt_path.write_bytes(raw)
                reference["sha256"] = hashlib.sha256(raw).hexdigest()
            with pytest.raises(module.RotationGateError, match="not trusted"):
                module.verify_evidence(
                    contract,
                    evidence,
                    receipt_root=receipt_root,
                    receipt_public_key=substituted_private_key.public_key(),
                    now=datetime(2026, 7, 14, 12, 1, tzinfo=UTC),
                )
            for _requirement, reference in observations.items():
                receipt_path = receipt_root / reference["receipt_path"]
                raw = (
                    json.dumps(
                        _signed_receipt(
                            unsigned_receipts[receipt_path],
                            receipt_private_key,
                            domain=b"exomem.rotation-drill-receipt.v1\0",
                        ),
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    + "\n"
                ).encode()
                receipt_path.write_bytes(raw)
                reference["sha256"] = hashlib.sha256(raw).hexdigest()

        missing = json.loads(json.dumps(evidence))
        missing["observations"].pop(rotation["retirement_requires"][0])
        with pytest.raises(module.RotationGateError, match="retirement evidence is incomplete"):
            module.verify_evidence(
                contract,
                missing,
                receipt_root=receipt_root,
                receipt_public_key=receipt_public_key,
                now=datetime(2026, 7, 14, 12, 1, tzinfo=UTC),
            )

        if rotation_index == 0:
            first_reference = next(iter(observations.values()))
            tampered_path = receipt_root / first_reference["receipt_path"]
            linked = receipt_root / "linked-receipt.json"
            linked.symlink_to(tampered_path.name)
            unsafe = json.loads(json.dumps(evidence))
            first_requirement = next(iter(unsafe["observations"]))
            unsafe["observations"][first_requirement]["receipt_path"] = linked.name
            with pytest.raises(module.RotationGateError, match="unsafe"):
                module.verify_evidence(
                    contract,
                    unsafe,
                    receipt_root=receipt_root,
                    receipt_public_key=receipt_public_key,
                    now=datetime(2026, 7, 14, 12, 1, tzinfo=UTC),
                )
            tampered = json.loads(tampered_path.read_text(encoding="utf-8"))
            tampered["passed"] = False
            raw = (json.dumps(tampered, separators=(",", ":"), sort_keys=True) + "\n").encode()
            tampered_path.write_bytes(raw)
            first_reference["sha256"] = hashlib.sha256(raw).hexdigest()
            with pytest.raises(module.RotationGateError, match="unauthenticated"):
                module.verify_evidence(
                    contract,
                    evidence,
                    receipt_root=receipt_root,
                    receipt_public_key=receipt_public_key,
                    now=datetime(2026, 7, 14, 12, 1, tzinfo=UTC),
                )

            write_expired = json.loads(tampered_path.read_text(encoding="utf-8"))
            write_expired["passed"] = True
            write_expired = _signed_receipt(
                {key: value for key, value in write_expired.items() if key != "authentication"},
                receipt_private_key,
                domain=b"exomem.rotation-drill-receipt.v1\0",
            )
            raw = (json.dumps(write_expired, separators=(",", ":"), sort_keys=True) + "\n").encode()
            tampered_path.write_bytes(raw)
            first_reference["sha256"] = hashlib.sha256(raw).hexdigest()
            with pytest.raises(module.RotationGateError, match="expired"):
                module.verify_evidence(
                    contract,
                    evidence,
                    receipt_root=receipt_root,
                    receipt_public_key=receipt_public_key,
                    now=datetime(2026, 7, 15, 12, 0, 1, tzinfo=UTC),
                )


def test_capacity_gate_blocks_unknown_economics_and_seventh_user(tmp_path: Path) -> None:
    module = _load("infra/scripts/capacity_gate.py", "capacity_gate_test")
    contract = json.loads((INFRA / "operations/private-alpha-capacity-v1.json").read_text())
    assert contract["limits"] == {
        "active_user_cells": 6,
        "reserved_volume_attachments": 2,
        "provider_volume_attachment_limit": 16,
        "minimum_unused_provider_headroom": 8,
    }
    assert contract["pricing"]["friend_price_eur_gross"] == 5
    assert contract["pricing"]["public_price_eur_gross_range"] == [10, 15]
    assert contract["live_costs_verified"] is False
    assert contract["receipt_authentication"] == {
        "algorithm": "ed25519",
        "capacity_domain": "exomem.capacity-live-receipt.v1",
        "economics_domain": "exomem.capacity-economics-receipt.v1",
        "capacity_ttl_seconds": 300,
        "economics_ttl_seconds": 2678400,
        "capacity_private_key_custody": "kubernetes-hcloud-collector-only",
        "capacity_public_key_id": None,
        "economics_private_key_custody": "provider-paddle-collector-only",
        "economics_public_key_id": None,
        "gate_material": "public-keys-only",
    }
    contract["live_costs_verified"] = True
    contract["monthly_costs_eur_ex_vat"] = {
        key: 1.0 for key in contract["monthly_costs_eur_ex_vat"]
    }
    contract["paddle"] = {
        "actual_fee_tax_verified": True,
        "fee_model": "verified-live-statement",
        "tax_treatment": "merchant-of-record",
        "net_receipt_eur_for_friend_price": 4.1,
        "evidence_recorded_at": "2026-07-14T11:00:00Z",
    }
    contract["evidence"] = {
        "provider_invoice_reference": "a" * 64,
        "paddle_statement_reference": "b" * 64,
        "recorded_at": "2026-07-14T11:00:00Z",
    }
    capacity_private_key = Ed25519PrivateKey.generate()
    economics_private_key = Ed25519PrivateKey.generate()
    capacity_public_key = capacity_private_key.public_key()
    economics_public_key = economics_private_key.public_key()
    raw_public_path = tmp_path / "capacity-public-key"
    raw_public_path.write_text(
        base64.urlsafe_b64encode(
            capacity_public_key.public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        )
        .decode("ascii")
        .rstrip("="),
        encoding="ascii",
    )
    raw_public_path.chmod(0o600)
    assert (
        module._public_key_id(module._load_public_key(raw_public_path))
        == hashlib.sha256(
            capacity_public_key.public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        ).hexdigest()
    )
    contract["receipt_authentication"]["capacity_public_key_id"] = hashlib.sha256(
        capacity_public_key.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    ).hexdigest()
    contract["receipt_authentication"]["economics_public_key_id"] = hashlib.sha256(
        economics_public_key.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    ).hexdigest()
    assert not hasattr(module, "sign_receipt")
    capacity = {
        "schema_version": 1,
        "issuer": "exomem-live-kubernetes-hcloud-v1",
        "contract_sha256": module.contract_digest(contract),
        "receipt_id": "123e4567-e89b-42d3-a456-426614174001",
        "sequence": 41,
        "cluster_uid": "cluster-uid-1234",
        "observed_at": "2026-07-14T12:00:00Z",
        "expires_at": "2026-07-14T12:05:00Z",
        "active_user_cells": 5,
        "attached_volumes": 5,
    }
    economics = {
        "schema_version": 1,
        "issuer": "exomem-live-provider-paddle-v1",
        "contract_sha256": module.contract_digest(contract),
        "receipt_id": "123e4567-e89b-42d3-a456-426614174002",
        "sequence": 7,
        "observed_at": "2026-07-14T11:00:00Z",
        "expires_at": "2026-08-13T11:00:00Z",
        "monthly_costs_eur_ex_vat": contract["monthly_costs_eur_ex_vat"],
        "paddle": {
            "actual_fee_tax_verified": True,
            "fee_model": "verified-live-statement",
            "tax_treatment": "merchant-of-record",
            "net_receipt_eur_for_friend_price": 4.1,
        },
        "provider_invoice_sha256": "a" * 64,
        "paddle_statement_sha256": "b" * 64,
    }
    capacity_path = tmp_path / "capacity.json"
    economics_path = tmp_path / "economics.json"

    def write_receipt(
        path: Path, receipt: dict[str, object], private_key: Ed25519PrivateKey
    ) -> None:
        domain = (
            b"exomem.capacity-live-receipt.v1\0"
            if receipt["issuer"] == "exomem-live-kubernetes-hcloud-v1"
            else b"exomem.capacity-economics-receipt.v1\0"
        )
        path.write_text(
            json.dumps(_signed_receipt(receipt, private_key, domain=domain), sort_keys=True),
            encoding="utf-8",
        )
        path.chmod(0o600)

    write_receipt(capacity_path, capacity, capacity_private_key)
    write_receipt(economics_path, economics, economics_private_key)
    decision = module.evaluate_files(
        contract,
        capacity_receipt=capacity_path,
        economics_receipt=economics_path,
        capacity_public_key=capacity_public_key,
        economics_public_key=economics_public_key,
        now=datetime(2026, 7, 14, 12, 1, tzinfo=UTC),
    )
    assert decision.allowed is True

    replay_state = tmp_path / "capacity-gate-state.json"
    assert module.evaluate_files(
        contract,
        capacity_receipt=capacity_path,
        economics_receipt=economics_path,
        capacity_public_key=capacity_public_key,
        economics_public_key=economics_public_key,
        replay_state_path=replay_state,
        now=datetime(2026, 7, 14, 12, 1, tzinfo=UTC),
    ).allowed
    assert stat.S_IMODE(replay_state.stat().st_mode) == 0o600
    with pytest.raises(module.CapacityGateError, match="replayed"):
        module.evaluate_files(
            contract,
            capacity_receipt=capacity_path,
            economics_receipt=economics_path,
            capacity_public_key=capacity_public_key,
            economics_public_key=economics_public_key,
            replay_state_path=replay_state,
            now=datetime(2026, 7, 14, 12, 1, tzinfo=UTC),
        )

    substituted_capacity_key = Ed25519PrivateKey.generate()
    write_receipt(capacity_path, capacity, substituted_capacity_key)
    with pytest.raises(module.CapacityGateError, match="not trusted"):
        module.evaluate_files(
            contract,
            capacity_receipt=capacity_path,
            economics_receipt=economics_path,
            capacity_public_key=substituted_capacity_key.public_key(),
            economics_public_key=economics_public_key,
            now=datetime(2026, 7, 14, 12, 1, tzinfo=UTC),
        )
    write_receipt(capacity_path, capacity, capacity_private_key)

    write_receipt(economics_path, economics, capacity_private_key)
    with pytest.raises(module.CapacityGateError, match="unauthenticated"):
        module.evaluate_files(
            contract,
            capacity_receipt=capacity_path,
            economics_receipt=economics_path,
            capacity_public_key=capacity_public_key,
            economics_public_key=economics_public_key,
            now=datetime(2026, 7, 14, 12, 1, tzinfo=UTC),
        )
    write_receipt(economics_path, economics, economics_private_key)

    blocked_capacity = {**capacity, "active_user_cells": 6, "attached_volumes": 6}
    write_receipt(capacity_path, blocked_capacity, capacity_private_key)
    blocked = module.evaluate_files(
        contract,
        capacity_receipt=capacity_path,
        economics_receipt=economics_path,
        capacity_public_key=capacity_public_key,
        economics_public_key=economics_public_key,
        now=datetime(2026, 7, 14, 12, 1, tzinfo=UTC),
    )
    assert blocked.allowed is False
    assert blocked.reason == "active-user-cell-capacity-exhausted"

    tampered = json.loads(capacity_path.read_text(encoding="utf-8"))
    tampered["active_user_cells"] = 0
    capacity_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(module.CapacityGateError, match="unauthenticated"):
        module.evaluate_files(
            contract,
            capacity_receipt=capacity_path,
            economics_receipt=economics_path,
            capacity_public_key=capacity_public_key,
            economics_public_key=economics_public_key,
            now=datetime(2026, 7, 14, 12, 1, tzinfo=UTC),
        )

    write_receipt(capacity_path, capacity, capacity_private_key)
    with pytest.raises(module.CapacityGateError, match="expired"):
        module.evaluate_files(
            contract,
            capacity_receipt=capacity_path,
            economics_receipt=economics_path,
            capacity_public_key=capacity_public_key,
            economics_public_key=economics_public_key,
            now=datetime(2026, 7, 14, 12, 6, tzinfo=UTC),
        )

    cross_domain = _signed_receipt(
        capacity,
        capacity_private_key,
        domain=b"exomem.capacity-economics-receipt.v1\0",
    )
    capacity_path.write_text(json.dumps(cross_domain), encoding="utf-8")
    with pytest.raises(module.CapacityGateError, match="unauthenticated"):
        module.evaluate_files(
            contract,
            capacity_receipt=capacity_path,
            economics_receipt=economics_path,
            capacity_public_key=capacity_public_key,
            economics_public_key=economics_public_key,
            now=datetime(2026, 7, 14, 12, 1, tzinfo=UTC),
        )


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
    with pytest.raises(module.ActiveSecretRegistryError, match="not trusted"):
        module.load_registry(
            matrix_path=matrix_path,
            registry_path=write_registry("v2", artifact_v2),
            public_key_path=public_key_path,
            trust_contract_path=trust_contract_path,
        )


def test_operational_receipt_collectors_issue_only_domain_bound_attestations(
    tmp_path: Path,
) -> None:
    module = _load(
        "infra/helm/platform/files/operational_receipt_collector.py",
        "operational_receipt_collector_test",
    )
    contract = json.loads((INFRA / "operations/private-alpha-capacity-v1.json").read_text())
    contract["live_costs_verified"] = True
    contract["monthly_costs_eur_ex_vat"] = {
        key: 1.0 for key in contract["monthly_costs_eur_ex_vat"]
    }
    contract["paddle"] = {
        "actual_fee_tax_verified": True,
        "fee_model": "verified-live-statement",
        "tax_treatment": "merchant-of-record",
        "net_receipt_eur_for_friend_price": 4.1,
        "evidence_recorded_at": "2026-07-14T12:00:00Z",
    }
    contract["evidence"] = {
        "provider_invoice_reference": hashlib.sha256(b"reviewed provider invoice").hexdigest(),
        "paddle_statement_reference": hashlib.sha256(b"reviewed Paddle statement").hexdigest(),
        "recorded_at": "2026-07-14T12:00:00Z",
    }
    capacity_private_key = Ed25519PrivateKey.generate()
    economics_private_key = Ed25519PrivateKey.generate()
    capacity_public_key = capacity_private_key.public_key()
    economics_public_key = economics_private_key.public_key()
    contract["receipt_authentication"]["capacity_public_key_id"] = hashlib.sha256(
        capacity_public_key.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    ).hexdigest()
    contract["receipt_authentication"]["economics_public_key_id"] = hashlib.sha256(
        economics_public_key.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    ).hexdigest()
    snapshot = module.capacity_snapshot_from_documents(
        tenant_namespaces={
            "kind": "NamespaceList",
            "items": [{"metadata": {"name": "must-not-leak"}} for _ in range(4)],
        },
        cluster_namespace={"metadata": {"uid": "cluster-uid-1234"}},
        hcloud_pages=[
            {
                "volumes": [
                    {"id": 11, "server": 101},
                    {"id": 12, "server": None},
                    {"id": 13, "server": 102},
                ],
                "meta": {"pagination": {"next_page": None}},
            }
        ],
    )
    observed_at = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    capacity = module.build_capacity_receipt(
        contract=contract,
        snapshot=snapshot,
        sequence=42,
        observed_at=observed_at,
        private_key=capacity_private_key,
        receipt_id="123e4567-e89b-42d3-a456-426614174001",
    )
    assert capacity["active_user_cells"] == 4
    assert capacity["attached_volumes"] == 2
    assert capacity["expires_at"] == "2026-07-14T12:05:00Z"
    assert "must-not-leak" not in json.dumps(capacity)

    provider_invoice = tmp_path / "provider-invoice.pdf"
    paddle_statement = tmp_path / "paddle-statement.csv"
    provider_invoice.write_bytes(b"reviewed provider invoice")
    paddle_statement.write_bytes(b"reviewed Paddle statement")
    economics = module.build_economics_receipt(
        contract=contract,
        evidence={
            "monthly_costs_eur_ex_vat": {key: 1.0 for key in contract["monthly_costs_eur_ex_vat"]},
            "paddle": {
                "actual_fee_tax_verified": True,
                "fee_model": "verified-live-statement",
                "tax_treatment": "merchant-of-record",
                "net_receipt_eur_for_friend_price": 4.1,
            },
        },
        provider_invoice=provider_invoice,
        paddle_statement=paddle_statement,
        sequence=7,
        observed_at=observed_at,
        private_key=economics_private_key,
        receipt_id="123e4567-e89b-42d3-a456-426614174002",
    )
    assert (
        economics["provider_invoice_sha256"]
        == hashlib.sha256(provider_invoice.read_bytes()).hexdigest()
    )
    assert (
        economics["paddle_statement_sha256"]
        == hashlib.sha256(paddle_statement.read_bytes()).hexdigest()
    )

    for receipt, public_key, domain in (
        (capacity, capacity_public_key, b"exomem.capacity-live-receipt.v1\0"),
        (economics, economics_public_key, b"exomem.capacity-economics-receipt.v1\0"),
    ):
        authentication = receipt["authentication"]
        unsigned = {key: value for key, value in receipt.items() if key != "authentication"}
        canonical = json.dumps(unsigned, separators=(",", ":"), sort_keys=True).encode()
        public_key.verify(bytes.fromhex(authentication["signature"]), domain + canonical)
        with pytest.raises(InvalidSignature):
            public_key.verify(bytes.fromhex(authentication["signature"]), canonical)


def test_rotation_collector_rejects_uncontracted_or_failed_observations(tmp_path: Path) -> None:
    module = _load(
        "infra/helm/platform/files/operational_receipt_collector.py",
        "operational_rotation_collector_test",
    )
    contract = json.loads((INFRA / "contracts/rotation-drills-v1.json").read_text())
    private_key = Ed25519PrivateKey.generate()
    contract["receipt_authentication"]["public_key_id"] = hashlib.sha256(
        private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    ).hexdigest()
    observation = {
        "drill_id": "123e4567-e89b-42d3-a456-426614174000",
        "rotation": "cloudflare-tunnel",
        "requirement": "cloudflared_rollout_ready",
        "old_version": "v1",
        "new_version": "v2",
        "passed": True,
    }
    receipt = module.build_rotation_receipt(
        contract=contract,
        observation=observation,
        observed_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
        private_key=private_key,
        receipt_id="123e4567-e89b-42d3-a456-426614174003",
    )
    assert receipt["expires_at"] == "2026-07-15T12:00:00Z"
    unsigned = {key: value for key, value in receipt.items() if key != "authentication"}
    private_key.public_key().verify(
        bytes.fromhex(receipt["authentication"]["signature"]),
        b"exomem.rotation-drill-receipt.v1\0"
        + json.dumps(unsigned, separators=(",", ":"), sort_keys=True).encode(),
    )
    with pytest.raises(module.ReceiptCollectorError, match="not contracted"):
        module.build_rotation_receipt(
            contract=contract,
            observation={**observation, "requirement": "operator_says_it_is_fine"},
            observed_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
            private_key=private_key,
            receipt_id="123e4567-e89b-42d3-a456-426614174004",
        )
    with pytest.raises(module.ReceiptCollectorError, match="did not pass"):
        module.build_rotation_receipt(
            contract=contract,
            observation={**observation, "passed": False},
            observed_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
            private_key=private_key,
            receipt_id="123e4567-e89b-42d3-a456-426614174005",
        )


def test_operational_collector_alerts_are_exact_https_and_content_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load(
        "infra/helm/platform/files/operational_receipt_collector.py",
        "operational_receipt_alert_test",
    )
    target = "https://alerts.example.invalid/hooks/opaque"

    class Response:
        status = 204

        def __init__(self, final_url: str) -> None:
            self.final_url = final_url

        def geturl(self) -> str:
            return self.final_url

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class Opener:
        final_url = target

        def open(self, request: object, timeout: int) -> Response:
            assert timeout == 10
            headers = dict(request.header_items())
            assert headers["X-exomem-alert-transition"] == ("capacity-receipt-collection-failed")
            payload = json.loads(request.data)
            assert payload == {
                "schema_version": 1,
                "source": {"component": "capacity-receipt-collector"},
                "transition": {"active": True, "code": "collection-failed"},
            }
            return Response(self.final_url)

    opener = Opener()
    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *_args: opener)
    module.deliver_alert(
        webhook_url=target,
        component="capacity-receipt-collector",
        code="collection-failed",
        transition_id="capacity-receipt-collection-failed",
    )
    opener.final_url = "https://alerts.example.invalid/redirected"
    with pytest.raises(module.ReceiptCollectorError, match="delivery failed"):
        module.deliver_alert(
            webhook_url=target,
            component="capacity-receipt-collector",
            code="collection-failed",
            transition_id="capacity-receipt-collection-failed",
        )


def test_runbook_index_is_complete_and_executable_by_default() -> None:
    contract = json.loads((INFRA / "contracts/runbooks-v1.json").read_text())
    required = {
        "backend",
        "deploy",
        "secrets",
        "cell",
        "maintenance",
        "volume-rebind",
        "backup-restore",
        "deletion",
        "node-replacement",
        "break-glass",
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


def test_production_composition_contract_binds_release_and_operator_actions() -> None:
    contract = json.loads(
        (INFRA / "contracts/platform-composition-v1.json").read_text(encoding="utf-8")
    )
    assert contract["schema_version"] == 1
    assert contract["release"] == {
        "config_map": "exomem-hosted-release-v1",
        "key": "exomem-hosted-release-v1.json",
        "mount_path": "/etc/exomem/release/exomem-hosted-release-v1.json",
        "worker_environment": "EXOMEM_PROVISIONER_RELEASE_MANIFEST_PATH",
    }
    assert contract["provisioner"]["api_deployment"] == "exomem-provisioner-api"
    assert contract["provisioner"]["worker_deployment"] == "exomem-provisioner-worker"
    assert contract["provisioner"]["service"] == {
        "name": "exomem-provisioner",
        "namespace": "exomem-platform",
        "port": 8080,
    }
    assert contract["provisioner"]["protocol"] == "exomem-cell-provisioner.v1"
    assert set(contract["provisioner"]["actions"]) == {
        "provision",
        "health",
        "rotate-credential",
        "quiesce",
        "resume",
        "stop",
        "export",
        "export-release",
        "export-delete",
        "restore",
        "export-download",
        "seal",
        "discard",
        "destroy",
    }
    capacity = contract["capacity_gate"]
    assert capacity["implementation"] == "KubernetesHCloudCapacityGate"
    assert capacity["invoked_before"] == ["namespace-create", "pvc-create"]
    assert capacity["limits"] == {
        "active_user_cells": 6,
        "reserved_volume_attachments": 2,
        "provider_volume_attachment_limit": 16,
        "minimum_unused_provider_headroom": 8,
    }
    assert contract["volume_rebind"]["public_endpoint"] is None
    assert contract["volume_rebind"]["worker_primitive"] == ("VolumeLifecycleWorker.rebind_static")
    assert contract["provider_recovery_identity"] == {
        "algorithm": "ed25519",
        "version": 1,
        "annotation": "exomem.io/recovery-envelope",
        "cell_values": "providerRecoveryEnvelopes",
        "identity_issuer_environment": "EXOMEM_PROVIDER_RECOVERY_SIGNING_KEY",
        "routine_worker_environment": "EXOMEM_PROVIDER_RECOVERY_PUBLIC_KEY",
        "routine_worker_material": "public-key-only",
        "signing_seed_mounts": [
            "exomem-provisioner-api",
            "exomem-durability-backup",
            "exomem-database-backup",
            "exomem-volume-worker",
        ],
    }
    assert contract["durability"] == {
        "workload_contract": "infra/contracts/durability-workloads-v1.json",
        "storage_contract": "infra/contracts/durability-storage-v1.json",
        "storage_config_map": "exomem-durability-storage",
        "workloads": {
            "delivery_gc": "exomem-export-gc",
            "vault_backup": "exomem-durability-backup",
            "database_backup": "exomem-database-backup",
            "deletion": "exomem-deletion-worker",
            "volume_lifecycle": "exomem-volume-worker",
        },
        "private_signers": ["exomem-provider-recovery-signer/private-key"],
        "public_verifier": "exomem-provider-recovery-verifier/public-key",
    }
    secret_contract = json.loads(
        (INFRA / "contracts/secret-destinations-v1.json").read_text(encoding="utf-8")
    )["secrets"]
    provisioner_destinations = [
        destination
        for secret in secret_contract.values()
        for name, destination in secret["destinations"].items()
        if name.startswith("k3s.provisioner")
    ]
    assert provisioner_destinations
    assert all(
        destination["namespace"] == "exomem-platform" for destination in provisioner_destinations
    )

    runbook_expectations = {
        "deploy.md": [
            "exomem-hosted-release-v1",
            "exomem-provisioner-api",
            "exomem-provisioner-worker",
            "verify_provisioner_image.py",
            "durability-values.json",
            "recovery_bucket_name",
            "user_export_bucket_name",
            "database_backup_bucket_name",
        ],
        "cell.md": ["/cells/health", "X-Exomem-Provisioner-Protocol"],
        "maintenance.md": [
            "/cells/quiesce",
            "/cells/resume",
            "X-Exomem-Provisioner-Protocol",
        ],
        "backup-restore.md": [
            "/cells/restore",
            "X-Exomem-Provisioner-Protocol",
            "exomem-durability-backup",
            "exomem-database-backup",
            "exomem-export-gc",
            "exomem-durability-storage",
        ],
        "deletion.md": [
            "/cells/destroy",
            "X-Exomem-Provisioner-Protocol",
            "exomem-deletion-worker",
        ],
        "volume-rebind.md": [
            "/cells/restore",
            "VolumeLifecycleWorker.rebind_static",
        ],
    }
    for filename, markers in runbook_expectations.items():
        text = (ROOT / "docs/runbooks/hosted" / filename).read_text(encoding="utf-8")
        for marker in markers:
            assert marker in text, (filename, marker)


def test_secret_matrix_materializes_every_hosted_platform_workload_secret() -> None:
    matrix = json.loads(
        (INFRA / "contracts/secret-destinations-v1.json").read_text(encoding="utf-8")
    )
    materialized = {
        f"{destination['kubernetes_secret']}/{destination['key']}"
        for secret in matrix["secrets"].values()
        for destination in secret["destinations"].values()
        if destination["kind"] == "sops_k8s_secret"
    }
    required = {
        "exomem-provisioner-auth/credential",
        "exomem-provisioner-database/url",
        "exomem-provisioner-wrapping-key/key-material",
        "exomem-provider-recovery-signer/private-key",
        "exomem-provider-recovery-verifier/public-key",
        "exomem-capacity-receipt-signer/private-key",
        "exomem-hcloud-capacity-reader/token",
        "exomem-provisioner-hcloud-token/token",
        "exomem-recovery-upload-key-id/application-key-id",
        "exomem-recovery-upload-key/application-key",
        "exomem-recovery-delete-key-id/application-key-id",
        "exomem-recovery-delete-key/application-key",
        "exomem-user-export-delete-key-id/application-key-id",
        "exomem-user-export-delete-key/application-key",
        "exomem-database-backup-upload-key-id/application-key-id",
        "exomem-database-backup-upload-key/application-key",
        "exomem-database-backup-pg-service/pg_service.conf",
        "exomem-database-backup-pgpass/pgpass",
    }
    assert required <= materialized
    assert "exomem-provider-recovery-volume-signer/private-key" not in materialized
    assert not any("database-backup-delete" in value for value in materialized)

    terraform_outputs = {
        source["output"]
        for secret in matrix["secrets"].values()
        for source in secret["sources"]
        if source["kind"] == "terraform" and source["root"] == "durability"
    }
    assert {
        "user_export_upload_application_key_id",
        "user_export_upload_application_key",
        "user_export_restore_application_key_id",
        "user_export_restore_application_key",
        "user_export_delivery_application_key_id",
        "user_export_delivery_application_key",
        "database_backup_restore_application_key_id",
        "database_backup_restore_application_key",
        "etcd_snapshot_upload_application_key_id",
        "etcd_snapshot_upload_application_key",
        "etcd_snapshot_restore_application_key_id",
        "etcd_snapshot_restore_application_key",
    } <= terraform_outputs
    assert "database_backup_application_key_id" not in terraform_outputs
    assert "database_backup_application_key" not in terraform_outputs


def test_receipt_keypairs_have_atomic_generated_trust_roots() -> None:
    matrix_path = INFRA / "contracts/secret-destinations-v1.json"
    matrix_document = json.loads(matrix_path.read_text(encoding="utf-8"))
    secrets = matrix_document["secrets"]
    for pair_name in (
        "provider_recovery",
        "capacity_receipt",
        "economics_receipt",
        "rotation_receipt",
    ):
        assert secrets[f"{pair_name}_signing_key"]["sources"] == [
            {"kind": "generated-ed25519-private"}
        ]
        assert secrets[f"{pair_name}_public_key"]["sources"] == [{"kind": "derived-ed25519-public"}]
    assert (
        "escrow.provider-recovery-signer.active"
        in secrets["provider_recovery_signing_key"]["destinations"]
    )
    handoff = _load("infra/scripts/secret_handoff.py", "atomic_secret_handoff_test")
    loaded = handoff.load_matrix(matrix_path)
    assert loaded.secrets["provider_recovery_signing_key"].sources[0].kind == (
        "generated-ed25519-private"
    )
    assert (INFRA / "scripts/provider_recovery_keypair_handoff.py").is_file()


@pytest.mark.parametrize(
    ("pair_name", "expected_destination_kinds"),
    [
        ("provider_recovery", ["sops_k8s_secret", "sops_escrow", "sops_k8s_secret"]),
        ("capacity_receipt", ["sops_k8s_secret", "sops_escrow"]),
        ("economics_receipt", ["sops_escrow", "sops_escrow"]),
        ("rotation_receipt", ["sops_escrow", "sops_escrow"]),
    ],
)
def test_atomic_receipt_keypair_handoff_routes_matching_halves(
    pair_name: str,
    expected_destination_kinds: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff = _load("infra/scripts/secret_handoff.py", f"secret_handoff_{pair_name}")
    keypair = _load(
        "infra/scripts/provider_recovery_keypair_handoff.py",
        f"keypair_handoff_{pair_name}",
    )
    monkeypatch.setattr(keypair, "_load_handoff", lambda: handoff)
    sealed: list[tuple[str, bytes]] = []

    def seal(*, destination, secret, version, repository_root, **_kwargs) -> None:
        target = repository_root / destination.fields["target"].format(version=version)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"sops-ciphertext")
        sealed.append((destination.kind, secret))

    monkeypatch.setattr(handoff, "_seal_k8s_secret", seal)
    monkeypatch.setattr(handoff, "_seal_named_document", seal)
    keypair.execute_keypair_handoff(
        matrix_path=INFRA / "contracts/secret-destinations-v1.json",
        repository_root=tmp_path,
        version="v1",
        sops_bin="sops",
        pair_name=pair_name,
    )

    assert [kind for kind, _ in sealed] == expected_destination_kinds
    matrix = handoff.load_matrix(INFRA / "contracts/secret-destinations-v1.json")
    private_count = len(matrix.secrets[f"{pair_name}_signing_key"].destinations)
    private_values = [value for _, value in sealed[:private_count]]
    public_value = sealed[private_count][1]
    assert len(set(private_values)) == 1
    private_raw = base64.urlsafe_b64decode(private_values[0] + b"==")
    expected_public = (
        Ed25519PrivateKey.from_private_bytes(private_raw)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )
    if pair_name in {"economics_receipt", "rotation_receipt"}:
        public_key = serialization.load_pem_public_key(public_value)
        assert (
            public_key.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            == expected_public
        )
    else:
        assert base64.urlsafe_b64decode(public_value + b"==") == expected_public


def test_release_manifest_is_one_fail_closed_deployment_unit(tmp_path: Path) -> None:
    module = _load("infra/scripts/prepare_hosted_release.py", "prepare_hosted_release_test")
    validation_values = yaml.safe_load(
        (INFRA / "helm/platform/values.validation.yaml").read_text(encoding="utf-8")
    )
    registry = json.loads(validation_values["provisioner"]["releaseManifestJson"])[
        "commandRegistry"
    ]
    manifest = {
        "artifact": "exomem-hosted-release",
        "schemaVersion": 1,
        "sourceRepository": "https://github.com/Artexis10/exomem",
        "sourceCommit": "a" * 40,
        "release": "0.22.0",
        "hostedProtocol": "1",
        "releaseBuildTime": "2026-07-14T12:00:00Z",
        "runtimeImage": "ghcr.io/artexis10/exomem@sha256:" + "b" * 64,
        "publishedTag": "ghcr.io/artexis10/exomem:" + "a" * 40 + "-hosted",
        "operatorContractSha256": "c" * 64,
        "gatewayContractSha256": "d" * 64,
        "commandRegistry": registry,
    }
    manifest_path = tmp_path / "release.json"
    values_path = tmp_path / "release-values.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o644)

    module.prepare(
        manifest_path=manifest_path,
        values_path=values_path,
        provisioner_image="ghcr.io/artexis10/exomem-provisioner@sha256:" + "e" * 64,
        control_hostname="memory.example.test",
        transfer_hostname="transfer.example.test",
    )
    assert stat.S_IMODE(values_path.stat().st_mode) == 0o600
    values = json.loads(values_path.read_text(encoding="utf-8"))
    assert values == {
        "provisioner": {
            "image": "ghcr.io/artexis10/exomem-provisioner@sha256:" + "e" * 64,
            "releaseManifestJson": json.dumps(manifest, separators=(",", ":"), sort_keys=True),
            "controlHostname": "memory.example.test",
            "transferHostname": "transfer.example.test",
        },
    }
    partial = dict(manifest)
    partial.pop("gatewayContractSha256")
    manifest_path.write_text(json.dumps(partial), encoding="utf-8")
    with pytest.raises(module.ReleaseManifestError, match="exact field set"):
        module.prepare(
            manifest_path=manifest_path,
            values_path=values_path,
            provisioner_image="ghcr.io/artexis10/exomem-provisioner@sha256:" + "e" * 64,
            control_hostname="memory.example.test",
            transfer_hostname="transfer.example.test",
        )

    mutable = {**manifest, "runtimeImage": "ghcr.io/artexis10/exomem:latest"}
    manifest_path.write_text(json.dumps(mutable), encoding="utf-8")
    with pytest.raises(module.ReleaseManifestError, match="immutable digest"):
        module.prepare(
            manifest_path=manifest_path,
            values_path=values_path,
            provisioner_image="ghcr.io/artexis10/exomem-provisioner@sha256:" + "e" * 64,
            control_hostname="memory.example.test",
            transfer_hostname="transfer.example.test",
        )

    noncanonical = json.loads(json.dumps(manifest))
    noncanonical["commandRegistry"][0]["name"] = "forged_command"
    manifest_path.write_text(json.dumps(noncanonical), encoding="utf-8")
    with pytest.raises(module.ReleaseManifestError, match="canonical"):
        module.prepare(
            manifest_path=manifest_path,
            values_path=values_path,
            provisioner_image="ghcr.io/artexis10/exomem-provisioner@sha256:" + "e" * 64,
            control_hostname="memory.example.test",
            transfer_hostname="transfer.example.test",
        )

    boolean_schema_version = {**manifest, "schemaVersion": True}
    manifest_path.write_text(json.dumps(boolean_schema_version), encoding="utf-8")
    with pytest.raises(module.ReleaseManifestError, match="identity"):
        module.prepare(
            manifest_path=manifest_path,
            values_path=values_path,
            provisioner_image="ghcr.io/artexis10/exomem-provisioner@sha256:" + "e" * 64,
            control_hostname="memory.example.test",
            transfer_hostname="transfer.example.test",
        )


def test_provisioner_image_smoke_checks_every_deployed_entrypoint_without_network() -> None:
    module = _load("infra/scripts/verify_provisioner_image.py", "verify_provisioner_image_test")
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert kwargs == {"check": False, "capture_output": True, "text": True}
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    image = "ghcr.io/artexis10/exomem-provisioner@sha256:" + "e" * 64
    module.verify(image=image, container_binary="docker", run=run)
    assert len(calls) == 1
    argv = calls[0]
    assert argv[:7] == [
        "docker",
        "run",
        "--rm",
        "--network=none",
        "--entrypoint",
        "python",
        image,
    ]
    probe = argv[-1]
    for command in (
        "exomem-provisioner-api",
        "exomem-provisioner-worker",
        "exomem-provisioner-volume-rebind",
        "exomem-durability-actions",
        "exomem-restore-fetch",
        "exomem-export-gc",
        "exomem-durability-backup-worker",
        "exomem-database-backup-worker",
        "exomem-deletion-worker",
        "exomem-volume-worker",
    ):
        assert command in probe
    assert "entry.load()" in probe
    assert "/usr/bin/pg_dump" in probe

    with pytest.raises(module.ProvisionerImageVerificationError, match="immutable"):
        module.verify(
            image="ghcr.io/artexis10/exomem-provisioner:latest",
            container_binary="docker",
            run=run,
        )

    def fail(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, stdout="secret output", stderr="more secret")

    with pytest.raises(module.ProvisionerImageVerificationError, match="smoke failed") as caught:
        module.verify(image=image, container_binary="docker", run=fail)
    assert "secret output" not in str(caught.value)


def test_provisioner_release_proof_pulls_and_confirms_the_published_digest() -> None:
    module = _load(
        "infra/scripts/verify_provisioner_image.py",
        "verify_published_provisioner_image_test",
    )
    image = "ghcr.io/artexis10/exomem-provisioner@sha256:" + "e" * 64
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        stdout = json.dumps([image]) if argv[1:3] == ["image", "inspect"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    module.verify(
        image=image,
        container_binary="docker",
        require_published=True,
        run=run,
    )
    assert [call[1:3] for call in calls] == [
        ["pull", image],
        ["image", "inspect"],
        ["run", "--rm"],
    ]

    def missing_digest(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        stdout = "[]" if argv[1:3] == ["image", "inspect"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    with pytest.raises(module.ProvisionerImageVerificationError, match="published digest"):
        module.verify(
            image=image,
            container_binary="docker",
            require_published=True,
            run=missing_digest,
        )


def test_full_validation_runs_published_provisioner_image_smoke_when_selected() -> None:
    validation = (INFRA / "scripts/validate.sh").read_text(encoding="utf-8")
    assert 'if [[ -n "${PROVISIONER_IMAGE:-}" ]]' in validation
    assert '"${script_dir}/verify_provisioner_image.py"' in validation
    assert '--image "${PROVISIONER_IMAGE}"' in validation


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
    for name in (
        "verify_ansible_convergence.py",
        "apply_sops_secret.py",
        "rotation_gate.py",
        "capacity_gate.py",
        "network_policy_probes.py",
        "prepare_hosted_release.py",
        "verify_provisioner_image.py",
        "external_blackbox.py",
    ):
        mode = (INFRA / "scripts" / name).stat().st_mode
        assert stat.S_IMODE(mode) & stat.S_IXUSR, name
