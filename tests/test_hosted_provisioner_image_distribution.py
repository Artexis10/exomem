from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVISIONER = ROOT / "infra/provisioner"
MIGRATION_ROOT = "/opt/exomem/provisioner-migrations"


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
