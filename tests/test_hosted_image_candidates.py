"""Contract tests for independent hosted OCI image candidate evidence."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
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
    workflow_path = ".github/workflows/release-please.yml"
    discovery_tag = f"{COMMIT}-hosted"
    storage: dict[str, str] = {
        "kind": "github-release",
        "subject": f"{repository}@sha256:{DIGEST}",
        "uri": f"https://github.com/Artexis10/exomem/releases/download/v0.35.1/candidate-{DIGEST}.json",
    }
    if kind == "provisioner":
        repository = "ghcr.io/artexis10/exomem-provisioner"
        source_ref = checkout_ref = "refs/heads/main"
        release = None
        workflow_path = ".github/workflows/publish-hosted-provisioner.yml"
        discovery_tag = COMMIT
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
            "id": "12345",
            "url": "https://github.com/Artexis10/exomem/attestations/12345",
            "bundleSha256": "",
        },
        "storage": storage,
    }


def _write_bundle(path: Path, data: bytes = b"bundle") -> str:
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


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


def test_verify_uses_exact_gh_policy_and_requires_exact_statement_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    record = _record()
    record["attestation"]["bundleSha256"] = _write_bundle(bundle)  # type: ignore[index]
    candidate_path = tmp_path / "candidate.json"
    candidate.record_candidate(record, bundle, candidate_path)
    captured: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = json.dumps(
            [
                {
                    "statement": {
                        "subject": [
                            {
                                "name": "ghcr.io/artexis10/exomem",
                                "digest": {"sha256": DIGEST},
                            }
                        ]
                    }
                }
            ]
        )
        stderr = ""

    def fake_run(argv: list[str], **_: object) -> Result:
        captured.append(argv)
        return Result()

    monkeypatch.setattr(candidate.subprocess, "run", fake_run)
    candidate.verify_candidate(candidate_path, bundle=bundle)

    assert captured == [
        [
            "gh",
            "attestation",
            "verify",
            f"ghcr.io/artexis10/exomem@sha256:{DIGEST}",
            "--repo",
            "Artexis10/exomem",
            "--signer-workflow",
            ".github/workflows/release-please.yml",
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
        ]
    ]


def test_verify_rejects_subject_drift_and_supports_oci_bundle_retrieval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    record = _record()
    record["attestation"]["bundleSha256"] = _write_bundle(bundle)  # type: ignore[index]
    candidate_path = tmp_path / "candidate.json"
    candidate.record_candidate(record, bundle, candidate_path)

    class Result:
        returncode = 0
        stdout = json.dumps([{"statement": {"subject": []}}])
        stderr = ""

    seen: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> Result:
        seen.append(argv)
        return Result()

    monkeypatch.setattr(candidate.subprocess, "run", fake_run)
    with pytest.raises(candidate.CandidateError, match="subject"):
        candidate.verify_candidate(candidate_path, bundle_from_oci=True)
    assert "--bundle" not in seen[0]
