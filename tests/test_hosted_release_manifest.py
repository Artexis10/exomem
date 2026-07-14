from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RELEASE_MANIFEST = ROOT / "infra/contracts/exomem-hosted-release-v1.json"
RUNTIME_GATE = ROOT / "infra/contracts/exomem-hosted-runtime-k3s-gate-v1.json"
VERIFIER = ROOT / "infra/scripts/verify_hosted_release.py"

EXPECTED_COMMAND_REGISTRY = [
    {
        "name": "coordination_status",
        "readOnly": True,
        "mode": "read",
        "tier": 1,
        "capability": "core",
    },
    {"name": "bootstrap", "readOnly": True, "mode": "read", "tier": 1, "capability": "core"},
    {"name": "ask_memory", "readOnly": True, "mode": "read", "tier": 1, "capability": "core"},
    {"name": "read_memory", "readOnly": True, "mode": "read", "tier": 1, "capability": "core"},
    {"name": "browse_memory", "readOnly": True, "mode": "read", "tier": 1, "capability": "core"},
    {"name": "remember", "readOnly": False, "mode": "write", "tier": 1, "capability": "core"},
    {"name": "edit_memory", "readOnly": False, "mode": "write", "tier": 1, "capability": "core"},
    {"name": "replace_memory", "readOnly": False, "mode": "write", "tier": 1, "capability": "core"},
    {"name": "capture_source", "readOnly": False, "mode": "write", "tier": 1, "capability": "core"},
    {"name": "compile_source", "readOnly": True, "mode": "read", "tier": 1, "capability": "core"},
    {
        "name": "preserve_evidence",
        "readOnly": False,
        "mode": "write",
        "tier": 1,
        "capability": "core",
    },
    {
        "name": "transfer_artifact",
        "readOnly": False,
        "mode": "write",
        "tier": 1,
        "capability": "core",
    },
    {"name": "review_memory", "readOnly": True, "mode": "read", "tier": 1, "capability": "core"},
    {
        "name": "review_item_context",
        "readOnly": True,
        "mode": "read",
        "tier": 1,
        "capability": "core",
    },
    {"name": "triage_memory", "readOnly": False, "mode": "write", "tier": 1, "capability": "core"},
    {"name": "connect_memory", "readOnly": False, "mode": "write", "tier": 1, "capability": "core"},
    {"name": "adopt_vault", "readOnly": False, "mode": "write", "tier": 1, "capability": "core"},
    {
        "name": "maintain_memory",
        "readOnly": False,
        "mode": "write",
        "tier": 1,
        "capability": "core",
    },
    {"name": "schema_memory", "readOnly": False, "mode": "write", "tier": 1, "capability": "core"},
    {
        "name": "manage_memory_file",
        "readOnly": False,
        "mode": "write",
        "tier": 2,
        "capability": "tier-2",
    },
    {"name": "query_dataset", "readOnly": True, "mode": "read", "tier": 2, "capability": "tier-2"},
]


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _verifier_module():
    spec = importlib.util.spec_from_file_location("verify_hosted_release", VERIFIER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_manifest_is_one_complete_immutable_unit() -> None:
    release = _load(RELEASE_MANIFEST)
    gate = _load(RUNTIME_GATE)

    assert set(release) == {
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
    assert release["artifact"] == "exomem-hosted-release"
    assert release["schemaVersion"] == 1
    for key in (
        "sourceRepository",
        "sourceCommit",
        "release",
        "hostedProtocol",
        "releaseBuildTime",
        "operatorContractSha256",
    ):
        assert release[key] == gate[key]

    source_commit = release["sourceCommit"]
    assert source_commit == "54618b931dec8f0ad053dce48dd80cc36c95c549"
    assert release["release"] == "0.22.0"
    assert release["hostedProtocol"] == "1"
    assert release["gatewayContractSha256"] == (
        "49ac4d346991f0f1de5f692a78ad043de6020f9a1692cafc951ec84490f02940"
    )
    assert re.fullmatch(
        r"ghcr\.io/artexis10/exomem@sha256:[0-9a-f]{64}", str(release["runtimeImage"])
    )
    assert release["publishedTag"] == (f"ghcr.io/artexis10/exomem:{source_commit}-hosted")
    assert release["commandRegistry"] == EXPECTED_COMMAND_REGISTRY


def test_release_validator_rejects_mutable_or_partial_overrides() -> None:
    verifier = _verifier_module()
    release = _load(RELEASE_MANIFEST)
    gate = _load(RUNTIME_GATE)

    verifier.validate_release_manifest(release, gate)

    for key, value in (
        ("runtimeImage", "ghcr.io/artexis10/exomem:hosted"),
        ("release", "0.22.1"),
        ("hostedProtocol", "2"),
        ("sourceCommit", "a" * 40),
    ):
        changed = copy.deepcopy(release)
        changed[key] = value
        with pytest.raises(ValueError):
            verifier.validate_release_manifest(changed, gate)

    for changed in (
        {**release, "unknown": True},
        {key: value for key, value in release.items() if key != "runtimeImage"},
    ):
        with pytest.raises(ValueError):
            verifier.validate_release_manifest(changed, gate)


def test_real_substrate_fixture_must_match_the_complete_release_unit() -> None:
    verifier = _verifier_module()
    release = _load(RELEASE_MANIFEST)
    fixture_path = Path("/tmp/substrate-exomem-hosted") / (
        "src/lib/exomem-hosted/__tests__/gateway-contract-0-22-0.json"
    )
    if not fixture_path.is_file():
        pytest.skip("the selected Substrate release worktree is not available")
    fixture = _load(fixture_path)

    verifier.validate_gateway_fixture(release, fixture)

    for field, value in (
        ("exomem_release", "0.22.1"),
        ("protocol_version", "2"),
        ("digest", {"algorithm": "sha256", "value": "b" * 64}),
        ("commands", fixture["commands"][:-1]),
    ):
        changed = copy.deepcopy(fixture)
        changed[field] = value
        with pytest.raises(ValueError):
            verifier.validate_gateway_fixture(release, changed)

    for field, value in (
        ("gatewayContractSha256", "b" * 64),
        ("commandRegistry", EXPECTED_COMMAND_REGISTRY[:-1]),
    ):
        changed_release = copy.deepcopy(release)
        changed_release[field] = value
        with pytest.raises(ValueError):
            verifier.validate_gateway_fixture(changed_release, fixture)


def test_image_provenance_must_bind_source_target_and_build_time() -> None:
    verifier = _verifier_module()
    release = _load(RELEASE_MANIFEST)
    provenance = {
        "SLSA": {
            "buildDefinition": {
                "externalParameters": {
                    "request": {
                        "root": {
                            "request": {
                                "args": {
                                    "build-arg:EXOMEM_RELEASE_BUILD_TIME": release[
                                        "releaseBuildTime"
                                    ],
                                    "target": "hosted",
                                    "vcs:revision": release["sourceCommit"],
                                    "vcs:source": release["sourceRepository"],
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    verifier.validate_image_provenance(release, provenance)
    for key, value in (
        ("target", "lean"),
        ("vcs:revision", "a" * 40),
        ("vcs:source", "https://example.invalid/exomem"),
        ("build-arg:EXOMEM_RELEASE_BUILD_TIME", "2026-01-01T00:00:00Z"),
    ):
        changed = copy.deepcopy(provenance)
        changed["SLSA"]["buildDefinition"]["externalParameters"]["request"]["root"]["request"][
            "args"
        ][key] = value
        with pytest.raises(ValueError):
            verifier.validate_image_provenance(release, changed)


@pytest.mark.skipif(
    os.environ.get("RUN_HOSTED_RELEASE_IMAGE_TEST") != "1",
    reason="set RUN_HOSTED_RELEASE_IMAGE_TEST=1 for the published-image route drill",
)
@pytest.mark.timeout(900)
def test_published_image_contract_route_exactly_matches_substrate_fixture() -> None:
    fixture_path = Path(
        os.environ.get(
            "SUBSTRATE_GATEWAY_FIXTURE",
            "/tmp/substrate-exomem-hosted/"
            "src/lib/exomem-hosted/__tests__/gateway-contract-0-22-0.json",
        )
    )
    assert fixture_path.is_file(), "set SUBSTRATE_GATEWAY_FIXTURE to the selected fixture"
    result = subprocess.run(
        [
            "python3",
            str(VERIFIER),
            "--manifest",
            str(RELEASE_MANIFEST),
            "--runtime-gate",
            str(RUNTIME_GATE),
            "--substrate-fixture",
            str(fixture_path),
            "--probe-image",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=900,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "hosted release verified\n"
