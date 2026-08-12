from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra/scripts/hosted_image_candidate.py"
COMPOSER_SCRIPT = ROOT / "infra/scripts/hosted_composition_lock.py"
COMMIT = "a" * 40
DIGEST = "b" * 64


def _module():
    spec = importlib.util.spec_from_file_location("hosted_image_candidate_cli", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _composer():
    spec = importlib.util.spec_from_file_location("hosted_composition_lock_cli", COMPOSER_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _argv(bundle: Path, output: Path, *extra: str, component: str = "runtime") -> list[str]:
    image = (
        f"ghcr.io/artexis10/exomem@sha256:{DIGEST}"
        if component == "runtime"
        else f"ghcr.io/artexis10/exomem-provisioner@sha256:{DIGEST}"
    )
    source_ref = "refs/tags/v0.35.1" if component == "runtime" else "refs/heads/main"
    workflow = (
        "Artexis10/exomem/.github/workflows/release-please.yml"
        if component == "runtime"
        else "Artexis10/exomem/.github/workflows/publish-hosted-provisioner.yml"
    )
    storage = (
        "https://github.com/Artexis10/exomem/releases/download/v0.35.1/candidate.json"
        if component == "runtime"
        else f"oci://{image}"
    )
    argv = [
        "record", "--component", component, "--source-repository", "Artexis10/exomem",
        "--source-ref", source_ref, "--source-commit", COMMIT,
        "--image", image, "--discovery-tag", f"{image.split('@')[0]}:{COMMIT}" + ("-hosted" if component == "runtime" else ""),
        "--producer-repository", "Artexis10/exomem", "--producer-workflow", workflow,
        "--producer-workflow-commit", COMMIT, "--producer-oidc-source-ref", source_ref,
        "--producer-oidc-source-commit", COMMIT, "--producer-event", "workflow_dispatch" if component == "runtime" else "push",
        "--run-id", "1", "--run-attempt", "1", "--bundle", str(bundle),
        "--storage-kind", "github-release" if component == "runtime" else "oci-referrer",
        "--storage-uri", storage, "--output", str(output),
    ]
    if component == "runtime":
        argv.extend(("--release", "0.35.1"))
    return [*argv, *extra]


def _claim_args(*, issued_at: str, expires_at: str, profile: str = "hosted-alpha-agent-v1",
                reader_version: str = "2", actions_enabled: str = "false") -> tuple[str, ...]:
    return (
        "--records-profile", profile,
        "--records-reader-version", reader_version,
        "--lifecycle-actions-enabled", actions_enabled,
        "--records-issued-at", issued_at,
        "--records-expires-at", expires_at,
        "--records-runtime-target-json", json.dumps(
            {
                "releaseVersion": "0.35.1",
                "protocolVersion": "1",
                "agentProfile": profile,
                "gatewayContractDigest": "c" * 64,
                "commandFingerprint": "d" * 64,
                "schemaDigest": "e" * 64,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def test_record_cli_emits_a_signed_reader_two_claim_without_changing_schema_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = _module()
    bundle = tmp_path / "bundle"
    bundle.write_bytes(b"bundle")
    legacy = tmp_path / "legacy.json"
    v2 = tmp_path / "v2.json"
    issued_at = datetime.now(UTC).replace(microsecond=0)
    expires_at = issued_at + timedelta(hours=1)
    issued = issued_at.isoformat().replace("+00:00", "Z")
    expires = expires_at.isoformat().replace("+00:00", "Z")

    assert candidate.main(_argv(bundle, legacy)) == 0
    assert candidate.main(_argv(bundle, v2, *_claim_args(issued_at=issued, expires_at=expires))) == 0

    assert candidate.load_candidate(legacy)["schemaVersion"] == 1
    emitted = candidate.load_candidate(v2)
    assert emitted["schemaVersion"] == 2
    assert emitted["recordsCompatibility"] == {
        "profile": "hosted-alpha-agent-v1", "recordsReaderVersion": 2,
        "lifecycleActionsEnabled": False, "issuedAt": issued, "expiresAt": expires,
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
    assert json.loads(legacy.read_text())["schemaVersion"] == 1

    composer = _composer()
    image_bundle = tmp_path / "image.bundle"
    candidate_bundle = tmp_path / "candidate.bundle"
    image_bundle.write_bytes(b"bundle")
    candidate_bundle.write_bytes(b"bundle")
    monkeypatch.setattr(composer.hosted_image_candidate, "verify_candidate", lambda *_args, **_kwargs: None)
    composed, candidate_sha = composer._candidate(
        composer.CandidateInput(
            v2,
            hashlib.sha256(v2.read_bytes()).hexdigest(),
            image_bundle,
            candidate_bundle,
        ),
        kind="runtime",
    )
    assert candidate_sha == hashlib.sha256(v2.read_bytes()).hexdigest()
    assert composed["recordsCompatibility"] == emitted["recordsCompatibility"]


@pytest.mark.parametrize(
    ("extra", "component"),
    [
        (("--records-profile", "hosted-alpha-agent-v1"), "runtime"),
        (_claim_args(issued_at="2026-08-12T11:00:00Z", expires_at="2026-08-12T12:00:00Z", profile="wrong"), "runtime"),
        (_claim_args(issued_at="2026-08-12T11:00:00Z", expires_at="2026-08-12T12:00:00Z", reader_version="1"), "runtime"),
        (_claim_args(issued_at="2026-08-12T11:00:00Z", expires_at="2026-08-12T12:00:00Z", actions_enabled="true"), "runtime"),
        (_claim_args(issued_at="2000-01-01T00:00:00Z", expires_at="2000-01-01T01:00:00Z"), "runtime"),
        (_claim_args(issued_at="2026-08-12T11:00:00Z", expires_at="2026-08-13T11:00:01Z"), "runtime"),
        (_claim_args(issued_at="2026-08-12T11:00:00+00:00", expires_at="2026-08-12T12:00:00Z"), "runtime"),
        (
            (*_claim_args(issued_at="2026-08-12T11:00:00Z", expires_at="2026-08-12T12:00:00Z"),
             "--producer-workflow", "Artexis10/exomem/.github/workflows/other.yml"),
            "runtime",
        ),
        (_claim_args(issued_at="2026-08-12T11:00:00Z", expires_at="2026-08-12T12:00:00Z"), "provisioner"),
    ],
)
def test_record_cli_refuses_partial_or_invalid_reader_claims(
    tmp_path: Path, extra: tuple[str, ...], component: str
) -> None:
    candidate = _module()
    bundle = tmp_path / "bundle"
    bundle.write_bytes(b"bundle")
    argv = _argv(bundle, tmp_path / "candidate.json", *extra, component=component)
    assert candidate.main(argv) == 2
