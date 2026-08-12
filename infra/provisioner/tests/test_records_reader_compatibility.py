from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from exomem_provisioner.config import load_deployment_lock


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()


def _v3_lock() -> dict[str, object]:
    digest = "a" * 64
    commit = "b" * 40
    image = f"ghcr.io/artexis10/exomem@sha256:{digest}"
    target = {
        "releaseVersion": "0.35.1",
        "protocolVersion": "1",
        "agentProfile": "hosted-alpha-agent-v2",
        "gatewayContractDigest": digest,
        "commandFingerprint": "c" * 64,
        "schemaDigest": "d" * 64,
    }
    legacy_target = {
        **target,
        "releaseVersion": "0.35.0",
        "agentProfile": "hosted-alpha-agent-v1",
    }
    legacy_contract = {**legacy_target, "runtimeImage": image, "sourceCommit": commit}
    return {
        "artifact": "exomem-hosted-deployment-lock",
        "schemaVersion": 3,
        "admissionMode": "expand",
        "components": {
            "runtime": {"image": image, "sourceCommit": commit, "candidateSha256": digest},
            "provisioner": {
                "image": f"ghcr.io/artexis10/exomem-provisioner@sha256:{'e' * 64}",
                "sourceCommit": commit,
                "candidateSha256": "e" * 64,
                "wireProtocol": "exomem-cell-provisioner.v2",
            },
        },
        "runtimeTarget": target,
        "composition": {
            "commit": commit,
            "sourceClosure": {
                name: {"candidateCommit": commit, "compositionCommit": commit, "paths": ["src/**"]}
                for name in ("runtime", "provisioner")
            },
            "forwardContractSha256": digest,
            "authoritativeLegacyReleaseSetSha256": "f" * 64,
            "legacyCatalog": [
                {
                    "releaseVersion": "0.35.0",
                    "protocolVersion": "1",
                    "runtimeImage": image,
                    "sourceCommit": commit,
                    "contractSha256": _canonical_sha256(legacy_contract),
                    "contract": legacy_contract,
                }
            ],
            "legacyReleaseSetSha256": _canonical_sha256(
                [{"releaseVersion": "0.35.0", "protocolVersion": "1"}]
            ),
        },
        "rollback": {
            "provisionerImage": f"ghcr.io/artexis10/exomem-provisioner@sha256:{'e' * 64}",
            "provisionerSourceCommit": commit,
            "v1CorpusSha256": digest,
            "legacyManifestSha256": digest,
            "substrateV1ConsumerCommit": commit,
        },
        "recordsCompatibility": {
            "minimum_records_reader_version": 2,
            "activeProfile": "hosted-alpha-agent-v2",
            "activeLifecycleActionsEnabled": True,
            "rollbackProfile": "hosted-alpha-agent-v1",
            "rollbackLifecycleActionsEnabled": False,
            "rollbackRuntime": {
                "image": image,
                "sourceCommit": commit,
                "candidateSha256": digest,
                "recordsReaderVersion": 2,
                "readerStatusProof": {
                    "profile": "hosted-alpha-agent-v1",
                    "recordsReaderVersion": 2,
                    "lifecycleActionsEnabled": False,
                    "issuedAt": "2026-08-12T10:00:00Z",
                    "expiresAt": "2026-08-12T11:00:00Z",
                    "signerWorkflow": "Artexis10/exomem/.github/workflows/release-please.yml",
                    "signerWorkflowDigest": commit,
                },
                "runtimeTarget": legacy_target,
            },
        },
    }


def test_v3_lock_requires_reader_two_and_explicit_compatible_rollback_runtime(tmp_path: Path) -> None:
    path = tmp_path / "selected-lock.json"
    path.write_text(json.dumps(_v3_lock()), encoding="utf-8")

    lock = load_deployment_lock(path)

    assert lock.schemaVersion == 3
    assert lock.records_compatibility.minimum_records_reader_version == 2
    assert lock.records_compatibility.activeLifecycleActionsEnabled is True
    assert lock.records_compatibility.rollbackLifecycleActionsEnabled is False
    assert lock.records_compatibility.rollbackRuntime.recordsReaderVersion == 2
    assert lock.records_compatibility.rollbackRuntime.readerStatusProof.profile == "hosted-alpha-agent-v1"


def test_v3_runtime_selection_returns_one_consistent_rollback_unit(tmp_path: Path) -> None:
    path = tmp_path / "selected-lock.json"
    payload = _v3_lock()
    rollback = payload["recordsCompatibility"]["rollbackRuntime"]  # type: ignore[index]
    rollback["image"] = f"ghcr.io/artexis10/exomem@sha256:{'f' * 64}"
    path.write_text(json.dumps(payload), encoding="utf-8")

    lock = load_deployment_lock(path)
    selected = lock.selected_runtime("rollback")

    assert selected.image == rollback["image"]
    assert selected.runtimeTarget.model_dump(mode="json") == rollback["runtimeTarget"]
    assert selected.recordsReaderVersion == 2
    assert selected.lifecycleActionsEnabled is False
    assert lock.matches_runtime_request(
        {"runtimeTarget": rollback["runtimeTarget"]},
        wire_protocol="exomem-cell-provisioner.v2",
        selection="rollback",
    )
    assert not lock.matches_runtime_request(
        {"runtimeTarget": payload["runtimeTarget"]},
        wire_protocol="exomem-cell-provisioner.v2",
        selection="rollback",
    )


@pytest.mark.parametrize("selection", (None, "unknown"))
def test_v3_runtime_selection_requires_an_explicit_known_value(tmp_path: Path, selection: object) -> None:
    path = tmp_path / "selected-lock.json"
    path.write_text(json.dumps(_v3_lock()), encoding="utf-8")

    with pytest.raises(ValueError, match="runtime selection"):
        load_deployment_lock(path).selected_runtime(selection)  # type: ignore[arg-type]


def test_v2_runtime_selection_refuses_rollback(tmp_path: Path) -> None:
    payload = _v3_lock()
    payload["schemaVersion"] = 2
    payload.pop("recordsCompatibility")
    path = tmp_path / "selected-lock.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="v2"):
        load_deployment_lock(path).selected_runtime("rollback")


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("recordsCompatibility", "minimum_records_reader_version"), 1),
        (("recordsCompatibility", "activeLifecycleActionsEnabled"), False),
        (("recordsCompatibility", "rollbackLifecycleActionsEnabled"), True),
        (("recordsCompatibility", "rollbackRuntime", "recordsReaderVersion"), 1),
        (("recordsCompatibility", "rollbackRuntime", "readerStatusProof", "recordsReaderVersion"), 1),
        (("recordsCompatibility", "rollbackRuntime", "readerStatusProof", "lifecycleActionsEnabled"), True),
    ],
)
def test_v3_lock_fails_closed_when_reader_compatibility_is_downgraded(
    tmp_path: Path, path: tuple[str, ...], value: object
) -> None:
    payload = _v3_lock()
    location: dict[str, object] = payload
    for key in path[:-1]:
        location = location[key]  # type: ignore[assignment,index]
    location[path[-1]] = value
    selected = tmp_path / "selected-lock.json"
    selected.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="deployment lock is invalid"):
        load_deployment_lock(selected)
