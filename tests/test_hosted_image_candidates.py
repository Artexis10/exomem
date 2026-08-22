"""Contract tests for independent hosted OCI image candidate evidence."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "infra" / "scripts" / "hosted_image_candidate.py"
)
SPEC = importlib.util.spec_from_file_location("hosted_image_candidate", SCRIPT)
assert SPEC and SPEC.loader
candidate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(candidate)


COMMIT = "a" * 40
DIGEST = "b" * 64
WORKFLOW_DIGEST = "c" * 40


def _record(kind: str = "runtime") -> dict[str, object]:
    repository = "ghcr.io/artexis10/exomem"
    source_ref = "refs/heads/main"
    checkout_ref = "refs/tags/v0.35.1"
    release: object = {"tag": "v0.35.1", "version": "0.35.1"}
    workflow_path = "Artexis10/exomem/.github/workflows/release-please.yml"
    discovery_tag = f"{repository}:{COMMIT}-hosted"
    storage: dict[str, str] = {
        "kind": "github-release",
        "subject": f"{repository}@sha256:{DIGEST}",
        "uri": f"https://github.com/Artexis10/exomem/releases/download/v0.35.1/candidate-{DIGEST}.json",
    }
    if kind == "provisioner":
        repository = "ghcr.io/artexis10/exomem-provisioner"
        source_ref = checkout_ref = "refs/heads/main"
        release = None
        workflow_path = "Artexis10/exomem/.github/workflows/publish-hosted-provisioner.yml"
        discovery_tag = f"{repository}:{COMMIT}"
        storage = {
            "kind": "oci-referrer",
            "subject": f"{repository}@sha256:{DIGEST}",
            "uri": f"oci://{repository}@sha256:{DIGEST}",
        }
    return {
        "schemaVersion": 1,
        "kind": kind,
        "image": {
            "repository": repository,
            "digest": f"sha256:{DIGEST}",
            "reference": f"{repository}@sha256:{DIGEST}",
            "discoveryTag": discovery_tag,
        },
        "source": {
            "repository": "Artexis10/exomem",
            "checkoutRef": checkout_ref,
            "commit": COMMIT,
        },
        "release": release,
        "workflow": {
            "producerRepository": "Artexis10/exomem",
            "signerWorkflow": workflow_path,
            "signerWorkflowDigest": WORKFLOW_DIGEST,
            "oidcSourceRef": source_ref,
            "oidcSourceCommit": COMMIT,
            "event": "push",
            "runId": "123456",
            "runAttempt": "1",
        },
        "attestation": {
            "predicateType": "https://slsa.dev/provenance/v1",
            "subjectName": repository,
            "subjectDigest": f"sha256:{DIGEST}",
            "bundleSha256": "",
        },
        "storage": storage,
    }


def _write_bundle(path: Path, data: bytes = b"bundle") -> str:
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _statement(name: str, sha256: str) -> str:
    return json.dumps(
        [{"verificationResult": {"statement": {"subject": [{"name": name, "digest": {"sha256": sha256}}]}}}]
    )


@pytest.mark.parametrize("kind", ["runtime", "provisioner"])
def test_record_round_trip_is_canonical_and_digest_authoritative(tmp_path: Path, kind: str) -> None:
    bundle = tmp_path / "evidence.json"
    record = _record(kind)
    record["attestation"]["bundleSha256"] = _write_bundle(bundle)  # type: ignore[index]
    output = tmp_path / "candidate.json"

    candidate.record_candidate(record, bundle, output)

    assert output.read_bytes().endswith(b"\n")
    if os.name != "nt":
        assert output.stat().st_mode & 0o777 == 0o600
    parsed = candidate.load_candidate(output)
    assert parsed == record
    assert list(json.loads(output.read_text(encoding="utf-8"))) == sorted(parsed)
    assert parsed["image"]["reference"] == (  # type: ignore[index]
        f"{parsed['image']['repository']}@{parsed['image']['digest']}"  # type: ignore[index]
    )


def test_runtime_rejects_release_and_source_identity_drift(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    record = _record()
    record["attestation"]["bundleSha256"] = _write_bundle(bundle)  # type: ignore[index]
    record["source"]["checkoutRef"] = "refs/tags/v0.35.0"  # type: ignore[index]

    with pytest.raises(candidate.CandidateError, match="release"):
        candidate.record_candidate(record, bundle, tmp_path / "candidate.json")


@pytest.mark.parametrize(
    ("path_value", "message"),
    [
        ("image", "unknown"),
        ("image.reference", "immutable"),
        ("workflow.oidcSourceRef", "source ref"),
    ],
)
def test_record_rejects_closed_schema_mutable_image_and_unapproved_ref(
    tmp_path: Path, path_value: str, message: str
) -> None:
    bundle = tmp_path / "bundle"
    record = _record()
    record["attestation"]["bundleSha256"] = _write_bundle(bundle)  # type: ignore[index]
    if path_value == "image":
        record["unknown"] = True
    elif path_value == "image.reference":
        record["image"]["reference"] = "ghcr.io/artexis10/exomem:hosted"  # type: ignore[index]
    else:
        record["workflow"]["oidcSourceRef"] = "refs/heads/feature"  # type: ignore[index]

    with pytest.raises(candidate.CandidateError, match=message):
        candidate.record_candidate(record, bundle, tmp_path / "candidate.json")


def test_load_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "candidate.json"
    path.write_text('{"schemaVersion": 1, "schemaVersion": 1}', encoding="utf-8")

    with pytest.raises(candidate.CandidateError, match="duplicate"):
        candidate.load_candidate(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "12345"),
        ("url", "https://github.com/Artexis10/exomem/attestations/12345"),
    ],
)
def test_candidate_schema_and_record_cli_exclude_attestation_id_and_url(field: str, value: str) -> None:
    assert set(_record()["attestation"]) == {"predicateType", "subjectName", "subjectDigest", "bundleSha256"}  # type: ignore[arg-type]
    record = _record()
    record["attestation"][field] = value  # type: ignore[index]
    with pytest.raises(candidate.CandidateError, match="unknown"):
        candidate.validate_candidate(record)
    parser = candidate._parser()
    commands = next(action for action in parser._subparsers._group_actions if action.dest == "command")  # type: ignore[union-attr]
    record_parser = commands.choices["record"]
    destinations = {action.dest for action in record_parser._actions}
    assert "attestation_id" not in destinations
    assert "attestation_url" not in destinations


def test_record_rejects_symlink_oversize_and_hash_drift(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    record = _record()
    record["attestation"]["bundleSha256"] = _write_bundle(bundle)  # type: ignore[index]
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(bundle)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(candidate.CandidateError, match="symlink"):
        candidate.record_candidate(record, linked, tmp_path / "candidate.json")

    oversized = tmp_path / "oversized"
    oversized.write_bytes(b"x" * (candidate.MAX_BUNDLE_BYTES + 1))
    with pytest.raises(candidate.CandidateError, match="maximum"):
        candidate.record_candidate(record, oversized, tmp_path / "candidate.json")

    with pytest.raises(candidate.CandidateError, match="hash"):
        candidate.record_candidate(record, bundle, tmp_path / "candidate.json", bundle_sha256="0" * 64)


def test_record_rejects_cross_subject_storage(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    record = _record()
    record["attestation"]["bundleSha256"] = _write_bundle(bundle)  # type: ignore[index]
    record["storage"]["subject"] = "ghcr.io/artexis10/exomem@sha256:" + "d" * 64  # type: ignore[index]

    with pytest.raises(candidate.CandidateError, match="storage"):
        candidate.record_candidate(record, bundle, tmp_path / "candidate.json")


def test_record_rejects_recorded_bundle_hash_and_malformed_source_commit(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    record = _record()
    record["attestation"]["bundleSha256"] = "0" * 64  # type: ignore[index]
    _write_bundle(bundle)

    with pytest.raises(candidate.CandidateError, match="bundle hash"):
        candidate.record_candidate(record, bundle, tmp_path / "candidate.json")

    record["attestation"]["bundleSha256"] = _write_bundle(bundle)  # type: ignore[index]
    record["source"]["commit"] = "not-a-commit"  # type: ignore[index]
    with pytest.raises(candidate.CandidateError, match="40-character"):
        candidate.record_candidate(record, bundle, tmp_path / "candidate.json")


def test_verify_uses_exact_gh_policy_for_image_and_candidate_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "image-bundle"
    candidate_bundle = tmp_path / "candidate-bundle"
    record = _record()
    record["attestation"]["bundleSha256"] = _write_bundle(bundle)  # type: ignore[index]
    candidate_path = tmp_path / "candidate.json"
    candidate.record_candidate(record, bundle, candidate_path)
    _write_bundle(candidate_bundle)
    captured: list[list[str]] = []

    class Result:
        returncode = 0
        stderr = ""

    def fake_run(argv: list[str], **_: object) -> Result:
        captured.append(argv)
        result = Result()
        if argv[3].startswith("oci://"):
            result.stdout = _statement("ghcr.io/artexis10/exomem", DIGEST)
        else:
            result.stdout = _statement(candidate_path.name, hashlib.sha256(candidate_path.read_bytes()).hexdigest())
        return result

    monkeypatch.setattr(candidate.subprocess, "run", fake_run)
    candidate.verify_candidate(candidate_path, bundle=bundle, candidate_bundle=candidate_bundle)

    assert captured == [
        [
            "gh",
            "attestation",
            "verify",
            f"oci://ghcr.io/artexis10/exomem@sha256:{DIGEST}",
            "--repo",
            "Artexis10/exomem",
            "--signer-workflow",
            "Artexis10/exomem/.github/workflows/release-please.yml",
            "--signer-digest",
            WORKFLOW_DIGEST,
            "--source-digest",
            COMMIT,
            "--source-ref",
            "refs/heads/main",
            "--predicate-type",
            "https://slsa.dev/provenance/v1",
            "--deny-self-hosted-runners",
            "--bundle",
            os.fspath(bundle),
            "--format",
            "json",
        ],
        [
            "gh",
            "attestation",
            "verify",
            os.fspath(candidate_path),
            "--repo",
            "Artexis10/exomem",
            "--signer-workflow",
            "Artexis10/exomem/.github/workflows/release-please.yml",
            "--signer-digest",
            WORKFLOW_DIGEST,
            "--source-digest",
            COMMIT,
            "--source-ref",
            "refs/heads/main",
            "--predicate-type",
            "https://slsa.dev/provenance/v1",
            "--deny-self-hosted-runners",
            "--bundle",
            os.fspath(candidate_bundle),
            "--format",
            "json",
        ],
    ]


def test_verify_rejects_image_subject_drift_and_supports_oci_bundle_retrieval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    candidate_bundle = tmp_path / "candidate-bundle"
    record = _record()
    record["attestation"]["bundleSha256"] = _write_bundle(bundle)  # type: ignore[index]
    candidate_path = tmp_path / "candidate.json"
    candidate.record_candidate(record, bundle, candidate_path)
    _write_bundle(candidate_bundle)

    class Result:
        returncode = 0
        stdout = json.dumps([{"verificationResult": {"statement": {"subject": []}}}])
        stderr = ""

    seen: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> Result:
        seen.append(argv)
        return Result()

    monkeypatch.setattr(candidate.subprocess, "run", fake_run)
    with pytest.raises(candidate.CandidateError, match="subject"):
        candidate.verify_candidate(candidate_path, bundle_from_oci=True, candidate_bundle=candidate_bundle)
    assert "--bundle" not in seen[0]
    assert "--bundle-from-oci" in seen[0]


@pytest.mark.parametrize("mismatch", ["name", "digest"])
def test_verify_rejects_wrong_candidate_subject_or_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    bundle = tmp_path / "bundle"
    candidate_bundle = tmp_path / "candidate-bundle"
    record = _record()
    record["attestation"]["bundleSha256"] = _write_bundle(bundle)  # type: ignore[index]
    candidate_path = tmp_path / "candidate.json"
    candidate.record_candidate(record, bundle, candidate_path)
    _write_bundle(candidate_bundle)

    class Result:
        returncode = 0
        stderr = ""

    def fake_run(argv: list[str], **_: object) -> Result:
        result = Result()
        if argv[3].startswith("oci://"):
            result.stdout = _statement("ghcr.io/artexis10/exomem", DIGEST)
        else:
            name = "wrong-name" if mismatch == "name" else candidate_path.name
            digest = "0" * 64 if mismatch == "digest" else hashlib.sha256(candidate_path.read_bytes()).hexdigest()
            result.stdout = _statement(name, digest)
        return result

    monkeypatch.setattr(candidate.subprocess, "run", fake_run)
    with pytest.raises(candidate.CandidateError, match="candidate"):
        candidate.verify_candidate(candidate_path, bundle=bundle, candidate_bundle=candidate_bundle)


@pytest.mark.parametrize("mismatch_target", [None, "image", "candidate"])
def test_verify_requires_every_gh_result_to_match_its_expected_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch_target: str | None
) -> None:
    bundle = tmp_path / "bundle"
    candidate_bundle = tmp_path / "candidate-bundle"
    record = _record()
    record["attestation"]["bundleSha256"] = _write_bundle(bundle)  # type: ignore[index]
    candidate_path = tmp_path / "candidate.json"
    candidate.record_candidate(record, bundle, candidate_path)
    _write_bundle(candidate_bundle)
    candidate_sha256 = hashlib.sha256(candidate_path.read_bytes()).hexdigest()

    class Result:
        returncode = 0
        stderr = ""

    def statements(name: str, digest: str, target: str) -> str:
        results = [json.loads(_statement(name, digest))[0], json.loads(_statement(name, digest))[0]]
        if mismatch_target == target:
            results[-1]["verificationResult"]["statement"]["subject"][0]["digest"] = {"sha256": "0" * 64}
        return json.dumps(results)

    def fake_run(argv: list[str], **_: object) -> Result:
        result = Result()
        if argv[3].startswith("oci://"):
            result.stdout = statements("ghcr.io/artexis10/exomem", DIGEST, "image")
        else:
            result.stdout = statements(candidate_path.name, candidate_sha256, "candidate")
        return result

    monkeypatch.setattr(candidate.subprocess, "run", fake_run)
    if mismatch_target is not None:
        with pytest.raises(candidate.CandidateError, match="candidate"):
            candidate.verify_candidate(candidate_path, bundle=bundle, candidate_bundle=candidate_bundle)
    else:
        candidate.verify_candidate(candidate_path, bundle=bundle, candidate_bundle=candidate_bundle)


@pytest.mark.parametrize("field", ["release", "tag", "storage", "metadata"])
def test_verify_rejects_tampered_candidate_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    bundle = tmp_path / "bundle"
    candidate_bundle = tmp_path / "candidate-bundle"
    record = _record()
    record["attestation"]["bundleSha256"] = _write_bundle(bundle)  # type: ignore[index]
    candidate_path = tmp_path / "candidate.json"
    candidate.record_candidate(record, bundle, candidate_path)
    original_digest = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
    _write_bundle(candidate_bundle)
    tampered = json.loads(candidate_path.read_text(encoding="utf-8"))
    if field == "release":
        tampered["release"] = {"tag": "v0.35.2", "version": "0.35.2"}
        tampered["source"]["checkoutRef"] = "refs/tags/v0.35.2"
        tampered["storage"]["uri"] = tampered["storage"]["uri"].replace("v0.35.1", "v0.35.2")
    elif field == "tag":
        tampered_commit = "d" * 40
        tampered["source"]["commit"] = tampered_commit
        tampered["workflow"]["oidcSourceCommit"] = tampered_commit
        tampered["image"]["discoveryTag"] = f"ghcr.io/artexis10/exomem:{tampered_commit}-hosted"
    elif field == "storage":
        tampered["storage"]["uri"] = tampered["storage"]["uri"].replace("candidate-", "replacement-")
    else:
        tampered["workflow"]["runId"] = "999999"
    candidate_path.write_text(json.dumps(tampered, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")

    class Result:
        returncode = 0
        stderr = ""

    def fake_run(argv: list[str], **_: object) -> Result:
        result = Result()
        if argv[3].startswith("oci://"):
            result.stdout = _statement("ghcr.io/artexis10/exomem", DIGEST)
        else:
            result.stdout = _statement(candidate_path.name, original_digest)
        return result

    monkeypatch.setattr(candidate.subprocess, "run", fake_run)
    with pytest.raises(candidate.CandidateError, match="candidate"):
        candidate.verify_candidate(candidate_path, bundle=bundle, candidate_bundle=candidate_bundle)


def test_verify_requires_candidate_bundle_and_rejects_unsafe_candidate_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    record = _record()
    record["attestation"]["bundleSha256"] = _write_bundle(bundle)  # type: ignore[index]
    candidate_path = tmp_path / "candidate.json"
    candidate.record_candidate(record, bundle, candidate_path)
    seen: list[list[str]] = []
    monkeypatch.setattr(candidate.subprocess, "run", lambda argv, **_kwargs: seen.append(argv))

    with pytest.raises(candidate.CandidateError, match="candidate bundle"):
        candidate.verify_candidate(candidate_path, bundle=bundle)

    missing = tmp_path / "missing-candidate-bundle"
    with pytest.raises(candidate.CandidateError, match="cannot inspect candidate bundle"):
        candidate.verify_candidate(candidate_path, bundle=bundle, candidate_bundle=missing)

    oversized = tmp_path / "oversized-candidate-bundle"
    oversized.write_bytes(b"x" * (candidate.MAX_BUNDLE_BYTES + 1))
    with pytest.raises(candidate.CandidateError, match="candidate bundle.*maximum"):
        candidate.verify_candidate(candidate_path, bundle=bundle, candidate_bundle=oversized)

    linked = tmp_path / "linked-candidate-bundle"
    try:
        linked.symlink_to(bundle)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(candidate.CandidateError, match="candidate bundle.*symlink"):
        candidate.verify_candidate(candidate_path, bundle=bundle, candidate_bundle=linked)
    assert seen == []


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired(["gh"], 1), OSError("missing gh")])
def test_verify_fails_cleanly_when_gh_cannot_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    bundle = tmp_path / "bundle"
    candidate_bundle = tmp_path / "candidate-bundle"
    record = _record()
    record["attestation"]["bundleSha256"] = _write_bundle(bundle)  # type: ignore[index]
    candidate_path = tmp_path / "candidate.json"
    candidate.record_candidate(record, bundle, candidate_path)
    _write_bundle(candidate_bundle)
    monkeypatch.setattr(candidate.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(failure))

    with pytest.raises(candidate.CandidateError, match="unable to complete"):
        candidate.verify_candidate(candidate_path, bundle=bundle, candidate_bundle=candidate_bundle)


def test_runtime_storage_uri_must_be_canonical_release_asset(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    record = _record()
    record["attestation"]["bundleSha256"] = _write_bundle(bundle)  # type: ignore[index]
    record["storage"]["uri"] = (  # type: ignore[index]
        "https://github.com/Artexis10/exomem/releases/download/v0.35.1/%2e%2e/candidate.json"
    )

    with pytest.raises(candidate.CandidateError, match="storage"):
        candidate.record_candidate(record, bundle, tmp_path / "candidate.json")


def _runtime_target_fixture_files(tmp_path: Path) -> tuple[Path, Path]:
    agent = tmp_path / "agent-contract-fixture.json"
    agent.write_text(
        json.dumps(
            {
                "sourceCommit": COMMIT,
                "sourceRelease": "0.35.1",
                "compatibility": {
                    "profile": "hosted-alpha-agent-v1",
                    "command_surface_sha256": "c" * 64,
                    "schema_contract_sha256": "d" * 64,
                    "compatibility_sha256": "e" * 64,
                },
            }
        )
    )
    gateway = tmp_path / "gateway-contract-0-35-1.json"
    gateway.write_text(
        json.dumps(
            {
                "exomem_release": "0.35.1",
                "protocol_version": "1",
                "digest": {"algorithm": "sha256", "value": "f" * 64},
            }
        )
    )
    return agent, gateway


def _successful_verification(monkeypatch: pytest.MonkeyPatch, candidate_path: Path) -> None:
    class Result:
        returncode = 0
        stderr = ""

    def fake_run(argv: list[str], **_: object) -> Result:
        result = Result()
        if argv[3].startswith("oci://"):
            result.stdout = _statement("ghcr.io/artexis10/exomem", DIGEST)
        else:
            result.stdout = _statement(
                candidate_path.name,
                hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
            )
        return result

    monkeypatch.setattr(candidate.subprocess, "run", fake_run)


def test_verify_cli_emits_one_exact_runtime_target_after_both_attestations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "image-bundle"
    candidate_bundle = tmp_path / "candidate-bundle"
    record = _record()
    record["attestation"]["bundleSha256"] = _write_bundle(bundle)  # type: ignore[index]
    candidate_path = tmp_path / "candidate.json"
    candidate.record_candidate(record, bundle, candidate_path)
    _write_bundle(candidate_bundle)
    agent, gateway = _runtime_target_fixture_files(tmp_path)
    output = tmp_path / "target.json"
    _successful_verification(monkeypatch, candidate_path)

    assert (
        candidate.main(
            [
                "verify",
                "--candidate",
                str(candidate_path),
                "--candidate-bundle",
                str(candidate_bundle),
                "--bundle",
                str(bundle),
                "--agent-contract-fixture",
                str(agent),
                "--gateway-contract-fixture",
                str(gateway),
                "--runtime-target-output",
                str(output),
            ]
        )
        == 0
    )
    assert json.loads(output.read_text()) == {
        "releaseVersion": "0.35.1",
        "sourceCommit": COMMIT,
        "runtimeImage": f"ghcr.io/artexis10/exomem@sha256:{DIGEST}",
        "runtimeCandidateSha256": hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
        "protocolVersion": "1",
        "agentProfile": "hosted-alpha-agent-v1",
        "gatewayContractDigest": "f" * 64,
        "commandFingerprint": "c" * 64,
        "schemaDigest": "d" * 64,
        "compatibilityDigest": "e" * 64,
    }
    assert output.stat().st_mode & 0o777 == 0o600


def test_verify_cli_refuses_partial_or_mismatched_runtime_target_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "image-bundle"
    candidate_bundle = tmp_path / "candidate-bundle"
    record = _record()
    record["attestation"]["bundleSha256"] = _write_bundle(bundle)  # type: ignore[index]
    candidate_path = tmp_path / "candidate.json"
    candidate.record_candidate(record, bundle, candidate_path)
    _write_bundle(candidate_bundle)
    agent, gateway = _runtime_target_fixture_files(tmp_path)
    agent_value = json.loads(agent.read_text())
    agent_value["sourceCommit"] = "0" * 40
    agent.write_text(json.dumps(agent_value))
    output = tmp_path / "target.json"
    _successful_verification(monkeypatch, candidate_path)
    base = [
        "verify",
        "--candidate",
        str(candidate_path),
        "--candidate-bundle",
        str(candidate_bundle),
        "--bundle",
        str(bundle),
    ]

    assert candidate.main([*base, "--runtime-target-output", str(output)]) == 2
    assert not output.exists()
    assert (
        candidate.main(
            [
                *base,
                "--agent-contract-fixture",
                str(agent),
                "--gateway-contract-fixture",
                str(gateway),
                "--runtime-target-output",
                str(output),
            ]
        )
        == 2
    )
    assert not output.exists()
