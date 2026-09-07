from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from test_hosted_secret_handoff import _load_module, _matrix

ROOT = Path(__file__).resolve().parents[1]


def test_production_control_key_declares_a_matching_bws_binding():
    module = _load_module()
    matrix = module.load_matrix(ROOT / "infra/contracts/secret-destinations-v1.json")
    sources = matrix.secrets["control_plane_key"].sources
    assert sources[0].kind == "bws"
    document = json.loads((ROOT / sources[0].bindings).read_text())
    binding = document["bindings"][sources[0].binding]
    assert document["environment"] == "production"
    assert binding["format"] == "base64url-32"
    destination = next(iter(matrix.secrets["control_plane_key"].destinations.values()))
    assert binding["env"] == destination.fields["name"]


def test_production_provisioner_database_declares_a_matching_bws_binding():
    module = _load_module()
    matrix = module.load_matrix(ROOT / "infra/contracts/secret-destinations-v1.json")
    spec = matrix.secrets["provisioner_database_url"]
    source = spec.sources[0]
    assert source.kind == "bws"
    document = json.loads((ROOT / source.bindings).read_text())
    binding = document["bindings"][source.binding]
    assert document["product"] == "exomem-hosted"
    assert document["environment"] == "production"
    assert binding["format"] == "opaque-line"
    assert binding["expected_key"] == binding["env"] == "EXOMEM_HOSTED_PROVISIONER_DATABASE_URL"
    destination = spec.destinations["k3s.provisioner.database-url.active"]
    assert destination.fields["kubernetes_secret"] == "exomem-provisioner-database"
    assert destination.fields["key"] == "url"


def source_matrix(tmp_path: Path, **overrides: object) -> Path:
    matrix = _matrix()
    matrix["secrets"]["control_plane_key"]["sources"] = [
        {
            "kind": "bws",
            "bindings": "infra/contracts/bws-production-v1.json",
            "binding": "control-plane-key",
            **overrides,
        }
    ]
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(matrix))
    binding = tmp_path / "infra/contracts/bws-production-v1.json"
    binding.parent.mkdir(parents=True, exist_ok=True)
    binding.write_text("{}")
    return path


def test_bws_source_uses_only_the_matrix_binding(tmp_path, monkeypatch):
    module = _load_module()
    matrix = module.load_matrix(source_matrix(tmp_path))
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command, 0, stdout=b"fixture-control-key", stderr=b"ignored"
        )

    monkeypatch.setattr(module.subprocess, "run", run)
    result = module._read_secret(
        source_kind="bws",
        secret_spec=matrix.secrets["control_plane_key"],
        repository_root=tmp_path,
        terraform_bin="must-not-run",
    )
    assert result == b"fixture-control-key"
    assert calls == [
        (
            [
                "bwsx-secret",
                "get",
                "--bindings",
                str(tmp_path / "infra/contracts/bws-production-v1.json"),
                "control-plane-key",
            ],
            {"capture_output": True, "check": False, "timeout": 30},
        )
    ]


@pytest.mark.parametrize(
    "bindings",
    [
        "../other.json",
        "/tmp/other.json",
        "infra/contracts/../other.json",
        "infra/not-contracts/other.json",
        "infra/contracts/file.txt",
        "infra/contracts\\other.json",
    ],
)
def test_bws_matrix_rejects_non_contract_paths(tmp_path, bindings):
    module = _load_module()
    with pytest.raises(module.HandoffError, match="BWS binding"):
        module.load_matrix(source_matrix(tmp_path, bindings=bindings))


@pytest.mark.parametrize("failure", ["exit", "timeout", "missing", "placeholder"])
def test_bws_source_failure_is_content_free_and_never_falls_back(tmp_path, monkeypatch, failure):
    module = _load_module()
    matrix = module.load_matrix(source_matrix(tmp_path))
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 30, output=b"private-value")
        if failure == "missing":
            raise FileNotFoundError("private-value")
        return subprocess.CompletedProcess(
            command, 1 if failure == "exit" else 0, stdout=b"[SENSITIVE]", stderr=b"private-value"
        )

    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(module.HandoffError) as error:
        module._read_secret(
            source_kind="bws",
            secret_spec=matrix.secrets["control_plane_key"],
            repository_root=tmp_path,
            terraform_bin="must-not-run",
        )
    assert "private-value" not in str(error.value)
    assert len(calls) == 1


def test_bws_dry_run_reads_no_secret(tmp_path, monkeypatch):
    module = _load_module()
    matrix_path = source_matrix(tmp_path)
    # A valid existing local destination lets this test isolate dry-run ordering.
    raw = json.loads(matrix_path.read_text())
    raw["secrets"]["control_plane_key"]["destinations"] = {
        "escrow.control.active": {
            "kind": "sops_escrow",
            "slot": "active",
            "target": "infra/secrets/key.{version}.sops.json",
            "secret_key": "key",
        },
    }
    matrix_path.write_text(json.dumps(raw))

    def forbidden(*args, **kwargs):
        pytest.fail("dry-run contacted a secret source")

    monkeypatch.setattr(module.subprocess, "run", forbidden)
    module.execute_handoff(
        matrix_path=matrix_path,
        repository_root=tmp_path,
        secret_name="control_plane_key",
        version="v1",
        destination_ids=("escrow.control.active",),
        source_kind="bws",
        terraform_bin="terraform",
        sops_bin="sops",
        vercel_bin="vercel",
        vercel_project=None,
        dry_run=True,
    )
    assert not (tmp_path / "infra/secrets").exists()
