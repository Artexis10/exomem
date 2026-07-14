from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

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


def test_hosted_ci_wires_every_static_security_gate() -> None:
    workflow = (ROOT / ".github/workflows/hosted-infrastructure.yml").read_text(
        encoding="utf-8"
    )
    validator = (INFRA / "scripts/validate.sh").read_text(encoding="utf-8")
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
    assert "uvx --from \"ruff==${RUFF_VERSION}\"" in validator
    assert "uvx --from \"mypy==${MYPY_VERSION}\"" in validator
    assert '--skip-dirs charts "${repo_root}"' in validator
    for action_line in (
        line.strip() for line in workflow.splitlines() if line.strip().startswith("- uses:")
    ):
        assert len(action_line.rsplit("@", 1)[1].split()[0]) == 40


def test_k3s_snapshot_and_break_glass_contract_is_off_host_and_versioned() -> None:
    config = (INFRA / "ansible/roles/k3s/templates/config.yaml.j2").read_text(
        encoding="utf-8"
    )
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
    module = _load(
        "infra/scripts/validate_sops_ciphertext.py", "validate_sops_ciphertext_test"
    )
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


def test_rotation_retirement_gate_covers_every_independent_rotation() -> None:
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
    for name, rotation in contract["rotations"].items():
        recorded_at = "2026-07-14T12:00:00Z"
        evidence = {
            "schema_version": 1,
            "contract_sha256": module.contract_digest(contract),
            "drill_id": "123e4567-e89b-42d3-a456-426614174000",
            "recorded_at": recorded_at,
            "rotation": name,
            "old_version": "v1",
            "new_version": "v2",
            "observations": {
                key: {
                    "passed": True,
                    "observed_at": recorded_at,
                    "proof_type": "probe",
                    "reference": f"probe:{index:08d}",
                }
                for index, key in enumerate(rotation["retirement_requires"], start=1)
            },
        }
        result = module.verify_evidence(contract, evidence)
        assert result.rotation == name
        assert result.new_version == "v2"

        missing = json.loads(json.dumps(evidence))
        missing["observations"].pop(rotation["retirement_requires"][0])
        with pytest.raises(module.RotationGateError, match="retirement evidence is incomplete"):
            module.verify_evidence(contract, missing)

        boolean_only = json.loads(json.dumps(evidence))
        boolean_only["observations"] = {
            key: True for key in rotation["retirement_requires"]
        }
        with pytest.raises(module.RotationGateError, match="incomplete"):
            module.verify_evidence(contract, boolean_only)


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
    observation = {
        "schema_version": 1,
        "source": "kubernetes-api",
        "cluster_uid": "cluster-uid-1234",
        "observed_at": "2026-07-14T12:00:00Z",
        "reference": "k8s-observation:capacity-0001",
        "active_user_cells": 5,
        "attached_volumes": 5,
    }
    assert module.evaluate(contract, observation).allowed is False

    proven = json.loads(json.dumps(contract))
    proven["live_costs_verified"] = True
    proven["paddle"]["actual_fee_tax_verified"] = True
    proven["paddle"].update(
        {
            "fee_model": "verified-live-statement",
            "tax_treatment": "merchant-of-record",
            "net_receipt_eur_for_friend_price": 4.1,
            "evidence_recorded_at": "2026-07-14T11:00:00Z",
        }
    )
    proven["monthly_costs_eur_ex_vat"] = {
        key: 1.0 for key in proven["monthly_costs_eur_ex_vat"]
    }
    proven["evidence"] = {
        "provider_invoice_reference": "invoice:hetzner-0001",
        "paddle_statement_reference": "statement:paddle-0001",
        "recorded_at": "2026-07-14T11:00:00Z",
    }
    assert module.evaluate(proven, observation).allowed is True
    blocked_observation = {**observation, "active_user_cells": 6, "attached_volumes": 6}
    blocked = module.evaluate(proven, blocked_observation)
    assert blocked.allowed is False
    assert blocked.reason == "active-user-cell-capacity-exhausted"

    unattributed = {
        "active_user_cells": 0,
        "attached_volumes": 0,
    }
    assert module.evaluate(proven, unattributed).reason == "invalid-capacity-observation"


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
    manifest = module.render_probe_manifest(plan)
    assert "automountServiceAccountToken: false" in manifest
    assert "exomem.io/network-probe" in manifest
    assert "unlabelled-platform-ingress" in manifest
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
        module.LiveTargets.from_document(
            {**targets.as_document(), "neon_host": "missing host!"}
        )


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

with pathlib.Path(os.environ['CALLS']).open('a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1] == 'apply':
    sys.stdin.buffer.read()
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
if sys.argv[1] == 'wait' and os.environ.get('FAIL_POSITIVE') == '1' and 'positive-controls' in sys.argv[-1]:
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
    manifest = module.render_probe_manifest(plan)
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
        manifest, plan, str(kubectl), "cell-alpha", targets=targets
    )
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert sum(item[0] == "exec" for item in invocations) == 5
    assert sum(item[0] == "get" for item in invocations) == 5
    assert sum(item[0] == "apply" for item in invocations) == 2
    assert sum(item[0] == "wait" for item in invocations) == 2
    assert next(index for index, item in enumerate(invocations) if item[0] == "wait") < next(
        index for index, item in enumerate(invocations) if item[0] == "exec"
    )

    monkeypatch.setenv("ALLOW_TARGET", "neon.invalid")
    with pytest.raises(module.NetworkProbeError, match="neon"):
        module.execute_probe_manifest(
            manifest, plan, str(kubectl), "cell-alpha", targets=targets
        )

    monkeypatch.setenv("ALLOW_TARGET", "none")
    monkeypatch.setenv("FAIL_POSITIVE", "1")
    with pytest.raises(module.NetworkProbeError, match="positive-controls"):
        module.execute_probe_manifest(
            manifest, plan, str(kubectl), "cell-alpha", targets=targets
        )
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    final_positive_wait = max(
        index
        for index, item in enumerate(invocations)
        if item[0] == "wait" and "positive-controls" in item[-1]
    )
    assert not any(item[0] == "exec" for item in invocations[final_positive_wait + 1 :])


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
        "external_blackbox.py",
    ):
        mode = (INFRA / "scripts" / name).stat().st_mode
        assert stat.S_IMODE(mode) & stat.S_IXUSR, name
