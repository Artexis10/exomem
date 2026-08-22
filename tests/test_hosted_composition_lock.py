from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra" / "scripts" / "hosted_composition_lock.py"
COMMIT = "a" * 40
DIGEST = "b" * 64


def _module():
    spec = importlib.util.spec_from_file_location("hosted_composition_lock", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, value: object) -> str:
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _candidate(kind: str, image: str, *, records_compatibility: bool = False) -> dict[str, object]:
    repository = image.split("@", 1)[0]
    source_ref = "refs/tags/v0.35.1" if kind == "runtime" else "refs/heads/main"
    workflow = (
        "Artexis10/exomem/.github/workflows/release-please.yml"
        if kind == "runtime"
        else "Artexis10/exomem/.github/workflows/publish-hosted-provisioner.yml"
    )
    storage: dict[str, str]
    release: dict[str, str] | None
    if kind == "runtime":
        release = {"tag": "v0.35.1", "version": "0.35.1"}
        storage = {
            "kind": "github-release",
            "subject": image,
            "uri": "https://github.com/Artexis10/exomem/releases/download/v0.35.1/candidate.json",
        }
        discovery_tag = f"{repository}:{COMMIT}-hosted"
    else:
        release = None
        storage = {"kind": "oci-referrer", "subject": image, "uri": f"oci://{image}"}
        discovery_tag = f"{repository}:{COMMIT}"
    candidate: dict[str, object] = {
        "schemaVersion": 2 if records_compatibility else 1,
        "kind": kind,
        "image": {
            "repository": repository,
            "digest": image.rsplit("@", 1)[1],
            "reference": image,
            "discoveryTag": discovery_tag,
        },
        "source": {"repository": "Artexis10/exomem", "checkoutRef": source_ref, "commit": COMMIT},
        "release": release,
        "workflow": {
            "producerRepository": "Artexis10/exomem",
            "signerWorkflow": workflow,
            "signerWorkflowDigest": COMMIT,
            "oidcSourceRef": source_ref if kind == "runtime" else "refs/heads/main",
            "oidcSourceCommit": COMMIT,
            "event": "workflow_dispatch" if kind == "runtime" else "push",
            "runId": "1",
            "runAttempt": "1",
        },
        "attestation": {
            "predicateType": "https://slsa.dev/provenance/v1",
            "subjectName": repository,
            "subjectDigest": image.rsplit("@", 1)[1],
            "bundleSha256": DIGEST,
        },
        "storage": storage,
    }
    if records_compatibility:
        issued_at = datetime.now(UTC).replace(microsecond=0)
        candidate["recordsCompatibility"] = {
            "profile": "hosted-alpha-agent-v1",
            "recordsReaderVersion": 2,
            "lifecycleActionsEnabled": False,
            "issuedAt": issued_at.isoformat().replace("+00:00", "Z"),
            "expiresAt": (issued_at + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "signerWorkflow": "Artexis10/exomem/.github/workflows/release-please.yml",
            "signerWorkflowDigest": COMMIT,
            "runtimeTarget": {
                "releaseVersion": "0.35.1",
                "protocolVersion": "1",
                "agentProfile": "hosted-alpha-agent-v1",
                "gatewayContractDigest": "c" * 64,
                "commandFingerprint": "d" * 64,
                "schemaDigest": "e" * 64,
            },
        }
    return candidate


def _contract(image: str) -> dict[str, str]:
    return {
        "releaseVersion": "0.35.1",
        "protocolVersion": "1",
        "agentProfile": "hosted-alpha-agent-v1",
        "gatewayContractDigest": "c" * 64,
        "commandFingerprint": "d" * 64,
        "schemaDigest": "e" * 64,
        "runtimeImage": image,
        "sourceCommit": COMMIT,
    }


def _request(module, tmp_path: Path):
    runtime_image = f"ghcr.io/artexis10/exomem@sha256:{DIGEST}"
    provisioner_image = f"ghcr.io/artexis10/exomem-provisioner@sha256:{'c' * 64}"
    runtime_candidate = tmp_path / "runtime-candidate.json"
    provisioner_candidate = tmp_path / "provisioner-candidate.json"
    runtime_candidate_sha = _write_json(runtime_candidate, _candidate("runtime", runtime_image))
    provisioner_candidate_sha = _write_json(
        provisioner_candidate, _candidate("provisioner", provisioner_image)
    )
    forward = tmp_path / "forward.json"
    forward_sha = _write_json(forward, _contract(runtime_image))
    legacy_contract = tmp_path / "legacy.json"
    legacy_sha = _write_json(legacy_contract, _contract(runtime_image))
    authority = tmp_path / "authoritative-release-set.json"
    authority_sha = _write_json(
        authority,
        {
            "artifact": "exomem-hosted-authoritative-legacy-v1-release-set",
            "schemaVersion": 1,
            "units": [
                {
                    "releaseVersion": "0.35.1",
                    "protocolVersion": "1",
                    "runtimeImage": runtime_image,
                    "sourceCommit": COMMIT,
                }
            ],
        },
    )
    catalog = tmp_path / "catalog.json"
    catalog_sha = _write_json(
        catalog,
        {
            "schemaVersion": 1,
            "units": [
                {
                    "releaseVersion": "0.35.1",
                    "protocolVersion": "1",
                    "runtimeImage": runtime_image,
                    "sourceCommit": COMMIT,
                    "contractSha256": legacy_sha,
                }
            ],
        },
    )
    rollback = tmp_path / "rollback.json"
    rollback_sha = _write_json(
        rollback,
        {
            "provisionerImage": provisioner_image,
            "provisionerSourceCommit": COMMIT,
            "v1CorpusSha256": "f" * 64,
            "legacyManifestSha256": "1" * 64,
            "substrateV1ConsumerCommit": "2" * 40,
        },
    )
    for name in (
        "runtime.bundle",
        "runtime.candidate.bundle",
        "provisioner.bundle",
        "provisioner.candidate.bundle",
    ):
        (tmp_path / name).write_bytes(b"bundle")
    return module.CompositionRequest(
        repository=tmp_path,
        composition_commit=COMMIT,
        runtime=module.CandidateInput(
            runtime_candidate,
            runtime_candidate_sha,
            tmp_path / "runtime.bundle",
            tmp_path / "runtime.candidate.bundle",
        ),
        provisioner=module.CandidateInput(
            provisioner_candidate,
            provisioner_candidate_sha,
            tmp_path / "provisioner.bundle",
            tmp_path / "provisioner.candidate.bundle",
        ),
        forward_contract=module.HashedInput(forward, forward_sha),
        authoritative_legacy_release_set=module.HashedInput(authority, authority_sha),
        legacy_catalog=module.HashedInput(catalog, catalog_sha),
        legacy_contracts=(module.HashedInput(legacy_contract, legacy_sha),),
        rollback=module.HashedInput(rollback, rollback_sha),
        output=tmp_path / "lock-pair.json",
    )


def test_composer_verifies_candidates_and_writes_deterministic_lock_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    composer = _module()
    request = _request(composer, tmp_path)
    verified: list[Path] = []
    monkeypatch.setattr(
        composer.hosted_image_candidate,
        "verify_candidate",
        lambda path, **_kwargs: verified.append(path),
    )
    monkeypatch.setattr(
        composer,
        "verify_source_closure",
        lambda _repository, candidate, composition, paths: {
            "candidateCommit": candidate,
            "compositionCommit": composition,
            "paths": list(paths),
        },
    )

    composer.compose_locks(request)
    first = request.output.read_bytes()
    composer.compose_locks(request)

    pair = json.loads(first)
    expand, contract = pair["locks"]
    assert [path.name for path in verified] == [
        request.runtime.candidate.name,
        request.provisioner.candidate.name,
    ] * 2
    assert request.output.read_bytes() == first
    assert expand["admissionMode"] == "expand"
    assert contract["admissionMode"] == "contract"
    assert {key: value for key, value in expand.items() if key != "admissionMode"} == {
        key: value for key, value in contract.items() if key != "admissionMode"
    }
    assert (
        expand["components"]["runtime"]["candidateSha256"]
        == hashlib.sha256(request.runtime.candidate.read_bytes()).hexdigest()
    )


def test_composer_closes_each_immutable_component_at_its_own_source_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    composer = _module()
    request = _request(composer, tmp_path)
    runtime_commit = "f" * 40
    candidate = json.loads(request.runtime.candidate.read_text())
    candidate["source"]["commit"] = runtime_commit
    candidate["image"]["discoveryTag"] = (
        candidate["image"]["repository"] + ":" + runtime_commit + "-hosted"
    )
    candidate["workflow"]["oidcSourceCommit"] = runtime_commit
    runtime_sha = _write_json(request.runtime.candidate, candidate)
    forward = json.loads(request.forward_contract.path.read_text())
    forward["sourceCommit"] = runtime_commit
    forward_sha = _write_json(request.forward_contract.path, forward)
    request = replace(
        request,
        runtime=replace(request.runtime, sha256=runtime_sha),
        forward_contract=replace(request.forward_contract, sha256=forward_sha),
    )
    closures: list[tuple[str, str, tuple[str, ...]]] = []
    monkeypatch.setattr(composer.hosted_image_candidate, "verify_candidate", lambda *_a, **_k: None)

    def verify(
        _repository: Path, candidate_commit: str, composition_commit: str, paths: tuple[str, ...]
    ) -> dict[str, object]:
        closures.append((candidate_commit, composition_commit, paths))
        return {
            "candidateCommit": candidate_commit,
            "compositionCommit": composition_commit,
            "paths": list(paths),
        }

    monkeypatch.setattr(composer, "verify_source_closure", verify)

    pair = composer.compose_locks(request)

    assert closures == [
        (runtime_commit, runtime_commit, composer._RUNTIME_CLOSURE),
        (COMMIT, COMMIT, composer._PROVISIONER_CLOSURE),
    ]
    assert pair["locks"][0]["composition"]["sourceClosure"]["runtime"] == {
        "candidateCommit": runtime_commit,
        "compositionCommit": runtime_commit,
        "paths": list(composer._RUNTIME_CLOSURE),
    }


def test_lock_validation_accepts_runtime_source_anchor_independent_of_platform_composition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    composer = _module()
    request = _request(composer, tmp_path)
    monkeypatch.setattr(composer.hosted_image_candidate, "verify_candidate", lambda *_a, **_k: None)
    monkeypatch.setattr(
        composer,
        "verify_source_closure",
        lambda _repository, candidate, composition, paths: {
            "candidateCommit": candidate,
            "compositionCommit": composition,
            "paths": list(paths),
        },
    )
    composer.compose_locks(request)
    lock = json.loads(request.output.read_text())["locks"][0]
    runtime_commit = "f" * 40
    lock["components"]["runtime"]["sourceCommit"] = runtime_commit
    lock["composition"]["sourceClosure"]["runtime"].update(
        candidateCommit=runtime_commit,
        compositionCommit=runtime_commit,
    )

    composer.validate_deployment_lock(lock)


def test_composer_binds_runtime_upgrade_compatibility_and_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    composer = _module()
    request = _request(composer, tmp_path)
    consumer_commit = "3" * 40
    trust = tmp_path / "substrate-trust.json"
    trust_digest = _write_json(
        trust,
        {
            "artifact": "exomem-hosted-substrate-runtime-trust",
            "schemaVersion": 1,
            "consumerCommit": consumer_commit,
            "target": {
                **_contract(f"ghcr.io/artexis10/exomem@sha256:{DIGEST}"),
                "runtimeCandidateSha256": request.runtime.sha256,
                "compatibilityDigest": "9" * 64,
            },
            "pinnedSites": composer._SUBSTRATE_RUNTIME_TRUST_SITES,
            "fixtureSha256s": {"agent": "4" * 64, "gateway": "5" * 64},
        },
    )
    evidence = tmp_path / "runtime-upgrade.json"
    digest = _write_json(
        evidence,
        {
            "compatibilityDigest": "9" * 64,
            "migrationMode": "none",
            "substrateConsumerCommit": consumer_commit,
            "substrateTrustSha256": trust_digest,
        },
    )
    request = replace(
        request,
        runtime_upgrade=composer.HashedInput(evidence, digest),
        substrate_trust=composer.HashedInput(trust, trust_digest),
    )
    monkeypatch.setattr(composer.hosted_image_candidate, "verify_candidate", lambda *_a, **_k: None)
    monkeypatch.setattr(
        composer,
        "verify_source_closure",
        lambda _repository, candidate, composition, paths: {
            "candidateCommit": candidate,
            "compositionCommit": composition,
            "paths": list(paths),
        },
    )

    pair = composer.compose_locks(request)

    assert [lock["runtimeUpgrade"] for lock in pair["locks"]] == [
        {
            "compatibilityDigest": "9" * 64,
            "migrationMode": "none",
            "substrateConsumerCommit": consumer_commit,
            "substrateTrustSha256": trust_digest,
        },
        {
            "compatibilityDigest": "9" * 64,
            "migrationMode": "none",
            "substrateConsumerCommit": consumer_commit,
            "substrateTrustSha256": trust_digest,
        },
    ]


def test_composer_accepts_an_authoritatively_empty_legacy_dependency_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    composer = _module()
    request = _request(composer, tmp_path)
    authority = json.loads(request.authoritative_legacy_release_set.path.read_text())
    authority["units"] = []
    catalog = json.loads(request.legacy_catalog.path.read_text())
    catalog["units"] = []
    request = replace(
        request,
        authoritative_legacy_release_set=replace(
            request.authoritative_legacy_release_set,
            sha256=_write_json(request.authoritative_legacy_release_set.path, authority),
        ),
        legacy_catalog=replace(
            request.legacy_catalog,
            sha256=_write_json(request.legacy_catalog.path, catalog),
        ),
        legacy_contracts=(),
    )
    monkeypatch.setattr(composer.hosted_image_candidate, "verify_candidate", lambda *_a, **_k: None)
    monkeypatch.setattr(
        composer,
        "verify_source_closure",
        lambda _repository, candidate, composition, paths: {
            "candidateCommit": candidate,
            "compositionCommit": composition,
            "paths": list(paths),
        },
    )

    pair = composer.compose_locks(request)

    assert [lock["composition"]["legacyCatalog"] for lock in pair["locks"]] == [[], []]


def test_composer_cli_allows_zero_legacy_contract_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    composer = _module()
    captured = []
    monkeypatch.setattr(composer, "compose_locks", lambda request: captured.append(request))
    digest = "a" * 64

    assert (
        composer.main(
            [
                "--repository",
                str(tmp_path),
                "--composition-commit",
                "b" * 40,
                "--runtime-candidate",
                "runtime.json",
                "--runtime-candidate-sha256",
                digest,
                "--runtime-candidate-bundle",
                "runtime.bundle.json",
                "--runtime-bundle",
                "runtime.image.bundle.json",
                "--provisioner-candidate",
                "provisioner.json",
                "--provisioner-candidate-sha256",
                digest,
                "--provisioner-candidate-bundle",
                "provisioner.bundle.json",
                "--provisioner-bundle",
                "provisioner.image.bundle.json",
                "--forward-contract",
                f"forward.json={digest}",
                "--authoritative-legacy-release-set",
                f"authority.json={digest}",
                "--legacy-catalog",
                f"catalog.json={digest}",
                "--rollback",
                f"rollback.json={digest}",
                "--output",
                "pair.json",
            ]
        )
        == 0
    )
    assert len(captured) == 1
    assert captured[0].legacy_contracts == ()


@pytest.mark.parametrize("tamper", ("missing", "target", "commit", "digest", "sites"))
def test_runtime_upgrade_composition_requires_exact_substrate_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    composer = _module()
    request = _request(composer, tmp_path)
    consumer_commit = "3" * 40
    target = {
        **_contract(f"ghcr.io/artexis10/exomem@sha256:{DIGEST}"),
        "runtimeCandidateSha256": request.runtime.sha256,
        "compatibilityDigest": "9" * 64,
    }
    trust_value = {
        "artifact": "exomem-hosted-substrate-runtime-trust",
        "schemaVersion": 1,
        "consumerCommit": consumer_commit,
        "target": target,
        "pinnedSites": composer._SUBSTRATE_RUNTIME_TRUST_SITES,
        "fixtureSha256s": {"agent": "4" * 64, "gateway": "5" * 64},
    }
    if tamper == "target":
        target["schemaDigest"] = "0" * 64
    elif tamper == "commit":
        trust_value["consumerCommit"] = "6" * 40
    elif tamper == "sites":
        trust_value["pinnedSites"] = ["agent-store", "gateway-store", "renamed-site"]
    trust = tmp_path / "substrate-trust.json"
    trust_digest = _write_json(trust, trust_value)
    evidence = tmp_path / "runtime-upgrade.json"
    upgrade_trust_digest = "7" * 64 if tamper == "digest" else trust_digest
    digest = _write_json(
        evidence,
        {
            "compatibilityDigest": "9" * 64,
            "migrationMode": "none",
            "substrateConsumerCommit": consumer_commit,
            "substrateTrustSha256": upgrade_trust_digest,
        },
    )
    request = replace(
        request,
        runtime_upgrade=composer.HashedInput(evidence, digest),
        substrate_trust=(
            None if tamper == "missing" else composer.HashedInput(trust, trust_digest)
        ),
    )
    monkeypatch.setattr(composer.hosted_image_candidate, "verify_candidate", lambda *_a, **_k: None)
    monkeypatch.setattr(
        composer,
        "verify_source_closure",
        lambda _repository, candidate, composition, paths: {
            "candidateCommit": candidate,
            "compositionCommit": composition,
            "paths": list(paths),
        },
    )

    with pytest.raises(composer.CompositionError):
        composer.compose_locks(request)


@pytest.mark.parametrize(
    "failure",
    [
        "valid",
        "missing",
        "unverified",
        "reader",
        "profile",
        "lifecycle",
        "claim_reader",
        "claim_profile",
        "claim_action",
        "claim_signer",
        "claim_substitution",
        "claim_stale",
    ],
)
def test_v3_composition_requires_an_explicit_verified_reader_two_rollback_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    composer = _module()
    request = _request(composer, tmp_path)
    rollback_runtime = tmp_path / "rollback-runtime-candidate.json"
    rollback_runtime_candidate = _candidate(
        "runtime",
        f"ghcr.io/artexis10/exomem@sha256:{'d' * 64}",
        records_compatibility=failure.startswith("claim_") or failure == "valid",
    )
    rollback_runtime_claim = rollback_runtime_candidate.get("recordsCompatibility", {})
    if failure == "claim_reader":
        rollback_runtime_claim["recordsReaderVersion"] = 1
    elif failure == "claim_profile":
        rollback_runtime_claim["profile"] = "hosted-alpha-agent-v2"
    elif failure == "claim_action":
        rollback_runtime_claim["lifecycleActionsEnabled"] = True
    elif failure == "claim_signer":
        rollback_runtime_claim["signerWorkflowDigest"] = "0" * 40
    elif failure == "claim_substitution":
        rollback_runtime_claim["signerWorkflow"] = "Artexis10/exomem/.github/workflows/other.yml"
    elif failure == "claim_stale":
        rollback_runtime_claim["issuedAt"] = "2000-01-01T00:00:00Z"
        rollback_runtime_claim["expiresAt"] = "2000-01-01T01:00:00Z"
    rollback_runtime_sha = _write_json(rollback_runtime, rollback_runtime_candidate)
    rollback_bundle = tmp_path / "rollback-runtime.bundle"
    rollback_candidate_bundle = tmp_path / "rollback-runtime.candidate.bundle"
    rollback_bundle.write_bytes(b"bundle")
    rollback_candidate_bundle.write_bytes(b"bundle")
    compatibility = {
        "minimum_records_reader_version": 2,
        "activeProfile": "hosted-alpha-agent-v2",
        "activeLifecycleActionsEnabled": True,
        "rollbackProfile": "hosted-alpha-agent-v1",
        "rollbackLifecycleActionsEnabled": False,
    }
    if failure == "reader":
        compatibility["minimum_records_reader_version"] = 1
    elif failure == "profile":
        compatibility["rollbackProfile"] = "hosted-alpha-agent-v2"
    elif failure == "lifecycle":
        compatibility["rollbackLifecycleActionsEnabled"] = True
    compatibility_path = tmp_path / "records-compatibility.json"
    compatibility_sha = _write_json(compatibility_path, compatibility)
    forward = json.loads(request.forward_contract.path.read_text())
    forward["agentProfile"] = "hosted-alpha-agent-v2"
    request = replace(
        request,
        forward_contract=replace(
            request.forward_contract,
            sha256=_write_json(request.forward_contract.path, forward),
        ),
        records_compatibility=composer.HashedInput(compatibility_path, compatibility_sha),
        rollback_runtime=composer.CandidateInput(
            rollback_runtime, rollback_runtime_sha, rollback_bundle, rollback_candidate_bundle
        ),
    )
    verified: list[Path] = []
    monkeypatch.setattr(
        composer.hosted_image_candidate,
        "verify_candidate",
        lambda path, **_kwargs: verified.append(path),
    )
    monkeypatch.setattr(
        composer,
        "verify_source_closure",
        lambda _repository, candidate, composition, paths: {
            "candidateCommit": candidate,
            "compositionCommit": composition,
            "paths": list(paths),
        },
    )
    if failure == "missing":
        request = replace(request, rollback_runtime=None)
    if failure == "unverified":
        monkeypatch.setattr(
            composer.hosted_image_candidate,
            "verify_candidate",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                composer.CompositionError("unverified")
            ),
        )

    if failure == "valid":
        pair = composer.compose_locks(request)
        assert pair["schemaVersion"] == 3
        assert pair["locks"][0]["recordsCompatibility"] == {
            **compatibility,
            "rollbackRuntime": {
                "image": f"ghcr.io/artexis10/exomem@sha256:{'d' * 64}",
                "sourceCommit": COMMIT,
                "candidateSha256": rollback_runtime_sha,
                "recordsReaderVersion": 2,
                "readerStatusProof": {
                    "profile": "hosted-alpha-agent-v1",
                    "recordsReaderVersion": 2,
                    "lifecycleActionsEnabled": False,
                    "issuedAt": rollback_runtime_claim["issuedAt"],
                    "expiresAt": rollback_runtime_claim["expiresAt"],
                    "signerWorkflow": "Artexis10/exomem/.github/workflows/release-please.yml",
                    "signerWorkflowDigest": COMMIT,
                },
                "runtimeTarget": rollback_runtime_claim["runtimeTarget"],
            },
        }
        assert [path.name for path in verified] == [
            request.runtime.candidate.name,
            request.provisioner.candidate.name,
            rollback_runtime.name,
        ]
    else:
        with pytest.raises(composer.CompositionError):
            composer.compose_locks(request)
        assert not request.output.exists()


@pytest.mark.parametrize("failure", ["duplicate", "missing", "sha", "extra", "stale"])
def test_composer_rejects_untrusted_catalog_evidence_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    composer = _module()
    request = _request(composer, tmp_path)
    monkeypatch.setattr(
        composer.hosted_image_candidate, "verify_candidate", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        composer,
        "verify_source_closure",
        lambda _repository, candidate, composition, paths: {
            "candidateCommit": candidate,
            "compositionCommit": composition,
            "paths": list(paths),
        },
    )
    catalog = json.loads(request.legacy_catalog.path.read_text())
    if failure == "duplicate":
        catalog["units"].append(catalog["units"][0])
        request = replace(
            request,
            legacy_catalog=replace(
                request.legacy_catalog,
                sha256=_write_json(request.legacy_catalog.path, catalog),
            ),
        )
    elif failure == "missing":
        catalog["units"] = []
        request = replace(
            request,
            legacy_catalog=replace(
                request.legacy_catalog,
                sha256=_write_json(request.legacy_catalog.path, catalog),
            ),
        )
    elif failure == "sha":
        request = replace(request, legacy_catalog=replace(request.legacy_catalog, sha256="0" * 64))
    elif failure == "extra":
        catalog["units"].append({**catalog["units"][0], "releaseVersion": "0.35.0"})
        request = replace(
            request,
            legacy_catalog=replace(
                request.legacy_catalog,
                sha256=_write_json(request.legacy_catalog.path, catalog),
            ),
        )
    else:
        legacy = json.loads(request.legacy_contracts[0].path.read_text())
        legacy["releaseVersion"] = "0.22.0"
        legacy_sha = _write_json(request.legacy_contracts[0].path, legacy)
        catalog["units"][0]["contractSha256"] = legacy_sha
        request = replace(
            request,
            legacy_contracts=(replace(request.legacy_contracts[0], sha256=legacy_sha),),
            legacy_catalog=replace(
                request.legacy_catalog,
                sha256=_write_json(request.legacy_catalog.path, catalog),
            ),
        )

    with pytest.raises(composer.CompositionError):
        composer.compose_locks(request)
    assert not request.output.exists()


def test_composer_requires_the_caller_pinned_candidate_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    composer = _module()
    request = _request(composer, tmp_path)
    request = replace(
        request,
        runtime=replace(request.runtime, sha256="0" * 64),
    )
    monkeypatch.setattr(
        composer.hosted_image_candidate, "verify_candidate", lambda *_args, **_kwargs: None
    )

    with pytest.raises(composer.CompositionError, match="candidate SHA-256"):
        composer.compose_locks(request)
    assert not request.output.exists()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda lock: lock["composition"].update(legacyReleaseSetSha256="0" * 64),
        lambda lock: lock["composition"]["legacyCatalog"][0]["contract"].update(
            gatewayContractDigest="0" * 64
        ),
        lambda lock: lock["composition"]["sourceClosure"]["runtime"].update(
            candidateCommit="0" * 40
        ),
    ],
    ids=["release-set-digest", "embedded-contract", "closure-commit"],
)
def test_lock_validation_rejects_tampered_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation
) -> None:
    composer = _module()
    request = _request(composer, tmp_path)
    monkeypatch.setattr(
        composer.hosted_image_candidate, "verify_candidate", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        composer,
        "verify_source_closure",
        lambda _repository, candidate, composition, paths: {
            "candidateCommit": candidate,
            "compositionCommit": composition,
            "paths": list(paths),
        },
    )
    composer.compose_locks(request)
    lock = deepcopy(json.loads(request.output.read_text())["locks"][0])
    mutation(lock)

    with pytest.raises(composer.CompositionError):
        composer.validate_deployment_lock(lock)


def test_source_closure_rejects_changed_input_but_allows_unrelated_change(tmp_path: Path) -> None:
    composer = _module()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "runtime.py").write_text("one\n")
    (tmp_path / "notes.md").write_text("one\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True)
    base = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    (tmp_path / "notes.md").write_text("two\n")
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-am", "docs", "-q"], check=True)
    head = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()

    composer.verify_source_closure(tmp_path, base, head, ("src/**",))
    (tmp_path / "src" / "runtime.py").write_text("two\n")
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-am", "runtime", "-q"], check=True)
    changed = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    with pytest.raises(composer.CompositionError, match="source closure"):
        composer.verify_source_closure(tmp_path, base, changed, ("src/**",))


def test_atomic_failure_leaves_no_new_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    composer = _module()
    request = _request(composer, tmp_path)
    monkeypatch.setattr(
        composer.hosted_image_candidate, "verify_candidate", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        composer,
        "verify_source_closure",
        lambda _repository, candidate, composition, paths: {
            "candidateCommit": candidate,
            "compositionCommit": composition,
            "paths": list(paths),
        },
    )
    monkeypatch.setattr(
        composer.os, "replace", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk"))
    )

    with pytest.raises(composer.CompositionError, match="write"):
        composer.compose_locks(request)
    assert not request.output.exists()


@pytest.mark.parametrize("change", ["add", "delete", "rename"])
def test_source_closure_rejects_add_delete_and_rename(tmp_path: Path, change: str) -> None:
    composer = _module()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "src").mkdir()
    source = tmp_path / "src" / "runtime.py"
    source.write_text("one\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True)
    base = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    if change == "add":
        (tmp_path / "src" / "new.py").write_text("new\n")
    elif change == "delete":
        source.unlink()
    else:
        source.rename(tmp_path / "src" / "renamed.py")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", change], check=True)
    head = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()

    with pytest.raises(composer.CompositionError, match="source closure"):
        composer.verify_source_closure(tmp_path, base, head, ("src/**",))


@pytest.mark.parametrize("failure", ["shallow", "missing", "non-ancestor", "git-error"])
def test_source_closure_fails_closed_when_git_proof_is_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    composer = _module()

    def fake_git(_repository: Path, arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
        command = arguments[0]
        if failure == "shallow" and command == "rev-parse":
            return subprocess.CompletedProcess(arguments, 0, b"true\n", b"")
        if failure == "missing" and command == "cat-file":
            return subprocess.CompletedProcess(arguments, 1, b"", b"")
        if failure == "non-ancestor" and command == "merge-base":
            return subprocess.CompletedProcess(arguments, 1, b"", b"")
        if failure == "git-error" and command == "diff":
            return subprocess.CompletedProcess(arguments, 128, b"", b"fatal")
        return subprocess.CompletedProcess(
            arguments, 0, b"false\n" if command == "rev-parse" else b"", b""
        )

    monkeypatch.setattr(composer, "_git", fake_git)
    with pytest.raises(composer.CompositionError):
        composer.verify_source_closure(tmp_path, COMMIT, COMMIT, ("src/**",))


@pytest.mark.parametrize("failure", ["duplicate-json", "oversized", "symlink"])
def test_hashed_evidence_rejects_unsafe_bytes(tmp_path: Path, failure: str) -> None:
    composer = _module()
    path = tmp_path / "evidence.json"
    if failure == "duplicate-json":
        path.write_bytes(b'{"a":1,"a":1}')
    elif failure == "oversized":
        path.write_bytes(b"x" * (composer.MAX_EVIDENCE_BYTES + 1))
    else:
        target = tmp_path / "target.json"
        target.write_text("{}")
        try:
            path.symlink_to(target)
        except OSError:
            pytest.skip("symlinks unavailable")
    digest = hashlib.sha256(path.read_bytes()).hexdigest() if failure != "symlink" else "0" * 64
    with pytest.raises(composer.CompositionError):
        composer._load_hashed(composer.HashedInput(path, digest), label="evidence")


def test_atomic_failure_preserves_existing_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    composer = _module()
    output = tmp_path / "lock-pair.json"
    output.write_bytes(b"old pair\n")

    def fail_publish(source: Path, target: Path) -> None:
        raise OSError("disk")

    monkeypatch.setattr(composer.os, "replace", fail_publish)
    with pytest.raises(composer.CompositionError, match="write"):
        composer._write_pair_atomic(output, b"new pair\n")
    assert output.read_bytes() == b"old pair\n"


def test_pair_output_rejects_a_missing_nested_parent_under_an_alias(tmp_path: Path) -> None:
    composer = _module()
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    output = alias / "missing" / "lock-pair.json"

    with pytest.raises(composer.CompositionError, match="directory"):
        composer._write_pair_atomic(output, b"pair\n")
    assert not (target / "missing").exists()


def test_pair_validator_rejects_duplicate_or_divergent_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    composer = _module()
    request = _request(composer, tmp_path)
    monkeypatch.setattr(
        composer.hosted_image_candidate, "verify_candidate", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        composer,
        "verify_source_closure",
        lambda _repository, candidate, composition, paths: {
            "candidateCommit": candidate,
            "compositionCommit": composition,
            "paths": list(paths),
        },
    )
    pair = composer.compose_locks(request)
    duplicate = deepcopy(pair)
    duplicate["locks"][1]["admissionMode"] = "expand"
    with pytest.raises(composer.CompositionError):
        composer.validate_deployment_lock_pair(duplicate)
    divergent = deepcopy(pair)
    divergent["locks"][1]["components"]["runtime"]["candidateSha256"] = "0" * 64
    with pytest.raises(composer.CompositionError):
        composer.validate_deployment_lock_pair(divergent)


def test_authoritative_release_set_rejects_catalog_omission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    composer = _module()
    request = _request(composer, tmp_path)
    authority = json.loads(request.authoritative_legacy_release_set.path.read_text())
    authority["units"].append({**authority["units"][0], "releaseVersion": "0.34.0"})
    request = replace(
        request,
        authoritative_legacy_release_set=replace(
            request.authoritative_legacy_release_set,
            sha256=_write_json(request.authoritative_legacy_release_set.path, authority),
        ),
    )
    monkeypatch.setattr(
        composer.hosted_image_candidate, "verify_candidate", lambda *_args, **_kwargs: None
    )
    with pytest.raises(composer.CompositionError, match="authoritative"):
        composer.compose_locks(request)
    assert not request.output.exists()
