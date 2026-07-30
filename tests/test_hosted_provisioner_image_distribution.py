from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVISIONER = ROOT / "infra/provisioner"
MIGRATION_ROOT = "/opt/exomem/provisioner-migrations"
ATTEST_ACTION = "actions/attest@508db95dd578ae2727ebd6217d5ba78e4fbda05d"


def _load_verifier():
    path = ROOT / "infra/scripts/verify_provisioner_image.py"
    spec = importlib.util.spec_from_file_location("migration_image_verifier", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_provisioner_distribution_exposes_three_database_commands() -> None:
    project = tomllib.loads((PROVISIONER / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["scripts"] | {
        "exomem-provisioner-database-bootstrap": (
            "exomem_provisioner.database_bootstrap:run_bootstrap"
        ),
        "exomem-provisioner-database-migrate": (
            "exomem_provisioner.database_bootstrap:run_migrate"
        ),
        "exomem-provisioner-database-validate": (
            "exomem_provisioner.database_bootstrap:run_validate"
        ),
    } == project["project"]["scripts"]


def test_provisioner_image_packages_migrations_at_fixed_read_only_path() -> None:
    dockerfile = (PROVISIONER / "Dockerfile").read_text(encoding="utf-8")

    assert (
        f"COPY infra/provisioner/alembic.ini {MIGRATION_ROOT}/alembic.ini" in dockerfile
    )
    assert f"COPY infra/provisioner/alembic {MIGRATION_ROOT}/alembic" in dockerfile
    assert f"chown -R 0:0 {MIGRATION_ROOT}" in dockerfile
    assert f"chmod -R a-w {MIGRATION_ROOT}" in dockerfile


def test_docker_context_exposes_only_canonical_provisioner_build_inputs() -> None:
    rules = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert {rule for rule in rules if rule.startswith("!infra/provisioner/")} == {
        "!infra/provisioner/",
        "!infra/provisioner/README.md",
        "!infra/provisioner/alembic.ini",
        "!infra/provisioner/alembic/",
        "!infra/provisioner/alembic/**",
        "!infra/provisioner/pyproject.toml",
        "!infra/provisioner/src/",
        "!infra/provisioner/src/**",
        "!infra/provisioner/uv.lock",
    }
    assert "infra/provisioner/alembic/**/__pycache__/" in rules
    assert "infra/provisioner/alembic/**/*.pyc" in rules
    assert "!infra/scripts/" not in rules
    assert "!infra/contracts/" not in rules
    assert "!.github/" not in rules


def test_image_verifier_requires_packaged_migrations_and_database_commands() -> None:
    module = _load_verifier()

    assert {
        "exomem-provisioner-database-bootstrap",
        "exomem-provisioner-database-migrate",
        "exomem-provisioner-database-validate",
    } <= set(module._ENTRYPOINTS)
    assert module._MIGRATION_ROOT == MIGRATION_ROOT
    assert "DATABASE_REVISION" in module._PROBE
    assert "is_symlink" in module._PROBE
    assert "rglob" in module._PROBE
    assert "sha256" in module._PROBE
    assert "expected_files" in module._PROBE
    assert module._EXPECTED_MIGRATION_FILES == {
        str(path.relative_to(PROVISIONER)): __import__("hashlib").sha256(
            path.read_bytes()
        ).hexdigest()
        for path in [
            PROVISIONER / "alembic.ini",
            *sorted((PROVISIONER / "alembic").rglob("*")),
        ]
        if path.is_file() and "__pycache__" not in path.parts
    }


def test_provisioner_workflow_is_an_independent_digest_bound_candidate_producer() -> None:
    text = (ROOT / ".github/workflows/publish-hosted-provisioner.yml").read_text(
        encoding="utf-8"
    )

    assert 'test "$GITHUB_REF" = "refs/heads/main"' in text
    assert "attestations: write" in text
    assert text.count(ATTEST_ACTION) == 2
    assert "subject-name: ghcr.io/artexis10/exomem-provisioner" in text
    assert "subject-digest: ${{ steps.build.outputs.digest }}" in text
    assert "subject-path: ${{ steps.candidate.outputs.candidate }}" in text
    assert "push-to-registry: true" in text
    assert text.count("create-storage-record: false") == 2
    assert "infra/scripts/hosted_image_candidate.py record" in text
    assert text.count("infra/scripts/hosted_image_candidate.py verify") >= 2
    assert text.count("--candidate-bundle") >= 2
    assert "--attestation-id" not in text
    assert "--attestation-url" not in text
    assert '--source-repository "$GITHUB_REPOSITORY"' in text
    assert (
        '--producer-workflow '
        '"$GITHUB_REPOSITORY/.github/workflows/publish-hosted-provisioner.yml"'
        in text
    )
    assert "--storage-kind oci-referrer" in text
    assert "--bundle-from-oci" in text
    assert "application/vnd.exomem.hosted-image-candidate.v1+json" in text
    assert "application/vnd.dev.sigstore.bundle.v0.3+json" in text
    assert "oras attach" in text
    assert "--template '{{.digest}}'" in text
    assert "oras pull" in text
    assert text.count("cmp ") >= 2
    assert "steps.build.outputs.digest" in text
    assert 'test "$observed_revision" = "$GITHUB_SHA"' in text


def test_provisioner_workflow_has_only_owned_image_inputs() -> None:
    text = (ROOT / ".github/workflows/publish-hosted-provisioner.yml").read_text(
        encoding="utf-8"
    )

    assert '"infra/provisioner/**"' in text
    assert '"infra/helm/cell/**"' in text
    assert '"infra/scripts/hosted_image_candidate.py"' in text
    assert '"infra/tool-versions.env"' in text
    assert "exomem-hosted-release-v1.json" not in text
    assert "exomem-hosted-runtime-k3s-gate-v1.json" not in text
    assert "substrate-gateway-contract-selection-v1.json" not in text
    assert "verify_hosted_release.py" not in text
    assert "Prove the published runtime" not in text


def test_oras_used_for_candidate_attachment_is_version_and_hash_pinned() -> None:
    versions = (ROOT / "infra/tool-versions.env").read_text(encoding="utf-8")
    workflow = (
        ROOT / ".github/workflows/publish-hosted-provisioner.yml"
    ).read_text(encoding="utf-8")

    assert "ORAS_VERSION=1.3.3" in versions
    assert (
        "ORAS_LINUX_AMD64_SHA256="
        "9ce999f8d2de03fc03968b29d743077a58783e545e5eaa53917ca177352d0e59"
        in versions
    )
    assert "source infra/tool-versions.env" in workflow
    assert "oras_${ORAS_VERSION}_linux_amd64.tar.gz" in workflow
    assert "sha256sum --check" in workflow
