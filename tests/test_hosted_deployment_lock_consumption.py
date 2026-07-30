from __future__ import annotations

import hashlib
import importlib.util
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PREPARE = ROOT / "infra/scripts/prepare_hosted_release.py"
VERIFIER = ROOT / "infra/scripts/verify_hosted_release.py"


def _module(path: Path = PREPARE):
    spec = importlib.util.spec_from_file_location(path.stem, path)
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


def _write_evidence(lock: dict[str, object], directory: Path) -> None:
    directory.mkdir()
    runtime = lock["components"]["runtime"]  # type: ignore[index]
    composition = lock["composition"]  # type: ignore[index]
    forward = {**lock["runtimeTarget"], "runtimeImage": runtime["image"], "sourceCommit": runtime["sourceCommit"]}  # type: ignore[index]
    authority = {
        "artifact": "exomem-hosted-authoritative-legacy-v1-release-set",
        "schemaVersion": 1,
        "units": [
            {
                "releaseVersion": unit["releaseVersion"],
                "protocolVersion": unit["protocolVersion"],
                "runtimeImage": unit["runtimeImage"],
                "sourceCommit": unit["sourceCommit"],
            }
            for unit in composition["legacyCatalog"]
        ],
    }
    legacy_contract = composition["legacyCatalog"][0]["contract"]
    legacy_manifest = {
        "artifact": "exomem-hosted-release",
        "schemaVersion": 1,
        "sourceRepository": "https://github.com/Artexis10/exomem",
        "sourceCommit": legacy_contract["sourceCommit"],
        "release": legacy_contract["releaseVersion"],
        "hostedProtocol": legacy_contract["protocolVersion"],
        "releaseBuildTime": "2026-07-30T00:00:00Z",
        "runtimeImage": legacy_contract["runtimeImage"],
        "publishedTag": f"ghcr.io/artexis10/exomem:{legacy_contract['sourceCommit']}-hosted",
        "operatorContractSha256": "3" * 64,
        "gatewayContractSha256": legacy_contract["gatewayContractDigest"],
        "commandRegistry": [
            {"name": "status", "readOnly": True, "mode": "read", "tier": 1, "capability": "core"}
        ],
    }
    corpus = [{"request": "frozen-v1", "response": "accepted"}]
    evidence = [
        forward,
        authority,
        legacy_manifest,
        corpus,
        *(unit["contract"] for unit in composition["legacyCatalog"]),
    ]
    digests = [hashlib.sha256(_canonical(item)).hexdigest() for item in evidence]
    composition["forwardContractSha256"] = digests[0]
    composition["authoritativeLegacyReleaseSetSha256"] = digests[1]
    lock["rollback"]["legacyManifestSha256"] = digests[2]  # type: ignore[index]
    lock["rollback"]["v1CorpusSha256"] = digests[3]  # type: ignore[index]
    for index, value in enumerate(evidence):
        (directory / f"{index}.json").write_bytes(_canonical(value))


def test_fixed_lock_evidence_revalidates_all_reviewed_inputs(tmp_path: Path) -> None:
    verifier = _module(VERIFIER)
    pair = _pair()
    lock = pair["locks"][0]  # type: ignore[index]
    evidence = tmp_path / "evidence"
    _write_evidence(lock, evidence)

    verifier._verify_lock_evidence(lock, evidence, verifier._load_script("hosted_composition_lock.py"))


def test_fixed_lock_evidence_rejects_duplicate_digest_matches(tmp_path: Path) -> None:
    verifier = _module(VERIFIER)
    pair = _pair()
    lock = pair["locks"][0]  # type: ignore[index]
    evidence = tmp_path / "evidence"
    _write_evidence(lock, evidence)
    forward = next(evidence.glob("0.json"))
    (evidence / "duplicate.json").write_bytes(forward.read_bytes())

    with pytest.raises(ValueError, match="exactly one"):
        verifier._verify_lock_evidence(lock, evidence, verifier._load_script("hosted_composition_lock.py"))


def test_candidate_selection_uses_the_candidate_verifier_bounded_reader(tmp_path: Path) -> None:
    verifier = _module(VERIFIER)
    candidate = tmp_path / "candidate.candidate-v1.json"
    candidate.write_bytes(b"candidate")

    class CandidateLoader:
        MAX_CANDIDATE_BYTES = 128
        reads: list[Path] = []

        @classmethod
        def _read_regular(cls, path: Path, *, label: str, maximum: int) -> bytes:
            assert label == "candidate"
            assert maximum == cls.MAX_CANDIDATE_BYTES
            cls.reads.append(path)
            return path.read_bytes()

        @staticmethod
        def load_candidate(path: Path) -> dict[str, str]:
            return {"path": str(path)}

    assert verifier._one_candidate(
        [candidate], hashlib.sha256(candidate.read_bytes()).hexdigest(), CandidateLoader
    ) == candidate
    assert CandidateLoader.reads == [candidate]


def test_candidate_selection_rejects_unbounded_discovery() -> None:
    verifier = _module(VERIFIER)

    with pytest.raises(ValueError, match="count"):
        verifier._one_candidate([], "0" * 64, object())


def test_runtime_candidate_listing_rejects_unbounded_assets(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = _module(VERIFIER)
    monkeypatch.setattr(
        verifier,
        "_run",
        lambda _: SimpleNamespace(
            stdout=json.dumps(
                {
                    "assets": [
                        {"name": f"candidate-{index}.candidate-v1.json", "size": 1}
                        for index in range(33)
                    ]
                }
            )
        ),
    )

    with pytest.raises(ValueError, match="count"):
        verifier._release_candidate_assets("gh", "v0.35.1")


def test_oci_candidate_manifest_rejects_unexpected_or_unbounded_layers() -> None:
    verifier = _module(VERIFIER)
    candidate_media_type = "application/vnd.exomem.hosted-image-candidate.v1+json"
    bundle_media_type = "application/vnd.dev.sigstore.bundle.v0.3+json"

    def descriptor(media_type: str, digest: str, size: int) -> dict[str, object]:
        return {
            "mediaType": media_type,
            "digest": f"sha256:{digest * 64}",
            "size": size,
        }

    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "artifactType": candidate_media_type,
        "config": descriptor("application/vnd.oci.empty.v1+json", "a", 2),
        "layers": [
            descriptor(candidate_media_type, "b", 1),
            descriptor(bundle_media_type, "c", 1),
            descriptor(bundle_media_type, "d", 1),
        ],
    }

    with pytest.raises(ValueError, match="layer count"):
        verifier._validate_oci_candidate_manifest(manifest)


def test_oci_preflight_rejects_manifest_before_pull(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = _module(VERIFIER)
    image = "ghcr.io/artexis10/exomem-provisioner@sha256:" + "a" * 64
    descriptor = {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "digest": "sha256:" + "b" * 64,
        "size": 1,
        "artifactType": "application/vnd.exomem.hosted-image-candidate.v1+json",
    }
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "artifactType": "application/vnd.exomem.hosted-image-candidate.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.empty.v1+json",
            "digest": "sha256:" + "c" * 64,
            "size": 2,
        },
        "subject": {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": "sha256:" + "a" * 64,
            "size": 1,
        },
        "layers": [],
    }
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(command)
        if command[1] == "discover":
            return SimpleNamespace(stdout=json.dumps({"manifests": [descriptor]}))
        assert command[1:3] == ["manifest", "fetch"]
        return SimpleNamespace(stdout=json.dumps(manifest))

    monkeypatch.setattr(verifier, "_run", run)
    with pytest.raises(ValueError, match="layer count"):
        verifier._verified_provisioner_candidate(
            image=image,
            source_commit="d" * 40,
            expected_sha256=None,
            directory=tmp_path,
            candidate_tool=object(),
            oras_binary="oras",
            gh_binary="gh",
        )
    assert all(command[1] != "pull" for command in calls)


def test_rollback_substrate_consumer_requires_the_exact_pinned_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = _module(VERIFIER)
    monkeypatch.setattr(verifier, "_run", lambda _: SimpleNamespace(stdout="e" * 40 + "\n"))

    with pytest.raises(ValueError, match="consumer commit"):
        verifier._verify_substrate_v1_consumer("d" * 40, "gh")


def test_deploy_runbook_mandates_the_selected_lock_verifier() -> None:
    runbook = (ROOT / "docs/runbooks/hosted/deploy.md").read_text(encoding="utf-8")

    assert "infra/scripts/verify_hosted_release.py \\" in runbook
    assert '--phase "$EXOMEM_DEPLOYMENT_PHASE"' in runbook
    assert "--repository ." in runbook


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
