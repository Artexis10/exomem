from __future__ import annotations

import hashlib
import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PREPARE = ROOT / "infra/scripts/prepare_hosted_release.py"


def _module():
    spec = importlib.util.spec_from_file_location("prepare_hosted_release", PREPARE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _member(mode: str) -> dict[str, object]:
    runtime_image = "ghcr.io/artexis10/exomem@sha256:" + "a" * 64
    provisioner_image = "ghcr.io/artexis10/exomem-provisioner@sha256:" + "b" * 64
    commit = "c" * 40
    legacy_contract = {
        "releaseVersion": "0.35.0",
        "protocolVersion": "1",
        "agentProfile": "hosted-alpha-agent-v1",
        "gatewayContractDigest": "6" * 64,
        "commandFingerprint": "7" * 64,
        "schemaDigest": "8" * 64,
        "runtimeImage": runtime_image,
        "sourceCommit": commit,
    }
    return {
        "artifact": "exomem-hosted-deployment-lock",
        "schemaVersion": 2,
        "admissionMode": mode,
        "components": {
            "runtime": {"image": runtime_image, "sourceCommit": commit, "candidateSha256": "d" * 64},
            "provisioner": {
                "image": provisioner_image,
                "sourceCommit": commit,
                "candidateSha256": "e" * 64,
                "wireProtocol": "exomem-cell-provisioner.v2",
            },
        },
        "runtimeTarget": {
            "releaseVersion": "0.35.1",
            "protocolVersion": "1",
            "agentProfile": "hosted-alpha-agent-v1",
            "gatewayContractDigest": "f" * 64,
            "commandFingerprint": "1" * 64,
            "schemaDigest": "2" * 64,
        },
        "composition": {
            "commit": commit,
            "sourceClosure": {
                "runtime": {
                    "candidateCommit": commit,
                    "compositionCommit": commit,
                    "paths": ["Dockerfile", ".dockerignore", "pyproject.toml", "uv.lock", "README.md", "LICENSE", "src/**"],
                },
                "provisioner": {
                    "candidateCommit": commit,
                    "compositionCommit": commit,
                    "paths": ["infra/provisioner/Dockerfile", "infra/provisioner/pyproject.toml", "infra/provisioner/uv.lock", "infra/provisioner/README.md", "infra/provisioner/alembic.ini", "infra/provisioner/src/**", "infra/provisioner/alembic/**", "infra/helm/cell/**", ".dockerignore"],
                },
            },
            "forwardContractSha256": "3" * 64,
            "authoritativeLegacyReleaseSetSha256": "4" * 64,
            "legacyCatalog": [
                {
                    "releaseVersion": "0.35.0",
                    "protocolVersion": "1",
                    "runtimeImage": runtime_image,
                    "sourceCommit": commit,
                    "contractSha256": hashlib.sha256(_canonical(legacy_contract)).hexdigest(),
                    "contract": legacy_contract,
                }
            ],
            "legacyReleaseSetSha256": hashlib.sha256(
                _canonical([{"releaseVersion": "0.35.0", "protocolVersion": "1"}])
            ).hexdigest(),
        },
        "rollback": {
            "provisionerImage": provisioner_image,
            "provisionerSourceCommit": commit,
            "v1CorpusSha256": "9" * 64,
            "legacyManifestSha256": "0" * 64,
            "substrateV1ConsumerCommit": "a" * 40,
        },
    }


def _pair() -> dict[str, object]:
    expand = _member("expand")
    contract = deepcopy(expand)
    contract["admissionMode"] = "contract"
    return {"artifact": "exomem-hosted-deployment-lock-pair", "schemaVersion": 2, "locks": [expand, contract]}


def _write_pair(path: Path) -> tuple[dict[str, object], str]:
    pair = _pair()
    path.write_bytes(_canonical(pair))
    return pair, hashlib.sha256(_canonical(pair["locks"][0])).hexdigest()  # type: ignore[index]


def test_prepare_v2_derives_all_deploy_inputs_from_one_exact_pair_member(tmp_path: Path) -> None:
    prepare = _module()
    pair_path = tmp_path / "pair.json"
    pair, member_sha256 = _write_pair(pair_path)
    values_path = tmp_path / "values.json"

    prepare.prepare_v2(
        lock_pair_path=pair_path,
        values_path=values_path,
        phase="expand",
        member_sha256=member_sha256,
        control_hostname="control.example.test",
        transfer_hostname="transfer.example.test",
    )

    values = json.loads(values_path.read_text())
    selected = pair["locks"][0]  # type: ignore[index]
    assert values["provisioner"] == {
        "deploymentLockJson": _canonical(selected).decode(),
        "deploymentLockSha256": member_sha256,
        "controlHostname": "control.example.test",
        "transferHostname": "transfer.example.test",
    }


@pytest.mark.parametrize(
    ("phase", "member_sha256"),
    [("expand", "0" * 64), ("unknown", None)],
)
def test_prepare_v2_rejects_wrong_or_missing_exact_member_identity(
    tmp_path: Path, phase: str, member_sha256: str | None
) -> None:
    prepare = _module()
    pair_path = tmp_path / "pair.json"
    _pair, expected_member_sha256 = _write_pair(pair_path)

    with pytest.raises(prepare.ReleaseManifestError):
        prepare.prepare_v2(
            lock_pair_path=pair_path,
            values_path=tmp_path / "values.json",
            phase=phase,
            member_sha256=member_sha256,
            control_hostname="control.example.test",
            transfer_hostname="transfer.example.test",
        )
    assert expected_member_sha256


def test_prepare_v2_rejects_pair_member_tampering_and_free_overrides(tmp_path: Path) -> None:
    prepare = _module()
    pair_path = tmp_path / "pair.json"
    pair, member_sha256 = _write_pair(pair_path)
    pair["locks"][0]["components"]["runtime"]["image"] = "ghcr.io/artexis10/exomem:latest"  # type: ignore[index]
    pair_path.write_bytes(_canonical(pair))

    with pytest.raises(prepare.ReleaseManifestError):
        prepare.prepare_v2(
            lock_pair_path=pair_path,
            values_path=tmp_path / "values.json",
            phase="expand",
            member_sha256=member_sha256,
            control_hostname="control.example.test",
            transfer_hostname="transfer.example.test",
        )

    assert "provisioner_image" not in prepare.prepare_v2.__annotations__
