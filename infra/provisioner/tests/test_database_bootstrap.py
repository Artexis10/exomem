from __future__ import annotations

import importlib
from pathlib import Path

import pytest


def _bootstrap_module():
    return importlib.import_module("exomem_provisioner.database_bootstrap")


def test_bootstrap_lock_key_is_deterministic_and_database_schema_specific() -> None:
    module = _bootstrap_module()

    first = module.database_lock_key("hosted_control", "exomem_provisioner")

    assert first == module.database_lock_key("hosted_control", "exomem_provisioner")
    assert first != module.database_lock_key("hosted_control_shadow", "exomem_provisioner")
    assert first != module.database_lock_key("hosted_control", "exomem_provisioner_shadow")
    assert -(2**63) <= first < 2**63


@pytest.mark.parametrize(
    "overrides",
    [
        {"can_login": False},
        {"is_superuser": True},
        {"can_create_database": True},
        {"can_create_role": True},
        {"can_replicate": True},
        {"can_bypass_rls": True},
        {"member_of": ("privileged_parent",)},
    ],
)
def test_existing_runtime_role_with_unsafe_attributes_fails_closed(
    overrides: dict[str, object],
) -> None:
    module = _bootstrap_module()
    values: dict[str, object] = {
        "name": "exomem_provisioner_runtime",
        "can_login": True,
        "is_superuser": False,
        "can_create_database": False,
        "can_create_role": False,
        "can_replicate": False,
        "can_bypass_rls": False,
        "member_of": (),
    }
    values.update(overrides)

    with pytest.raises(module.DatabaseBootstrapError, match="runtime role is unsafe"):
        module.validate_runtime_role(
            module.RuntimeRoleState(**values),
            expected_role="exomem_provisioner_runtime",
            admin_role="bootstrap_admin",
            database_owner="bootstrap_admin",
            expected_schema="exomem_provisioner",
            owned_schemas=("exomem_provisioner",),
        )


@pytest.mark.parametrize(
    ("admin_role", "database_owner", "owned_schemas"),
    [
        ("exomem_provisioner_runtime", "bootstrap_admin", ("exomem_provisioner",)),
        ("bootstrap_admin", "exomem_provisioner_runtime", ("exomem_provisioner",)),
        ("bootstrap_admin", "bootstrap_admin", ()),
        (
            "bootstrap_admin",
            "bootstrap_admin",
            ("exomem_provisioner", "unrelated_schema"),
        ),
    ],
)
def test_runtime_identity_or_schema_ownership_mismatch_fails_closed(
    admin_role: str,
    database_owner: str,
    owned_schemas: tuple[str, ...],
) -> None:
    module = _bootstrap_module()
    state = module.RuntimeRoleState(
        name="exomem_provisioner_runtime",
        can_login=True,
        is_superuser=False,
        can_create_database=False,
        can_create_role=False,
        can_replicate=False,
        can_bypass_rls=False,
        member_of=(),
    )

    with pytest.raises(module.DatabaseBootstrapError):
        module.validate_runtime_role(
            state,
            expected_role="exomem_provisioner_runtime",
            admin_role=admin_role,
            database_owner=database_owner,
            expected_schema="exomem_provisioner",
            owned_schemas=owned_schemas,
        )


def test_bootstrap_configuration_requires_distinct_roles_on_the_same_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _bootstrap_module()
    monkeypatch.setenv(
        "EXOMEM_PROVISIONER_DATABASE_ADMIN_URL",
        "postgresql+asyncpg://operator:admin-secret@database.invalid/control",
    )
    monkeypatch.setenv(
        "EXOMEM_PROVISIONER_DATABASE_URL",
        "postgresql+asyncpg://runtime:runtime-secret@database.invalid/control",
    )
    monkeypatch.setenv("EXOMEM_PROVISIONER_DATABASE_SCHEMA", "provisioner_data")
    monkeypatch.setenv("EXOMEM_PROVISIONER_DATABASE_ROLE", "runtime")

    configuration = module.load_configuration(require_admin=True)

    assert configuration.database == "control"
    assert configuration.admin_role == "operator"
    assert configuration.runtime_role == "runtime"
    assert configuration.schema == "provisioner_data"
    assert configuration.lock_timeout_seconds == 60
    assert "admin-secret" not in repr(configuration)
    assert "runtime-secret" not in repr(configuration)


@pytest.mark.parametrize(
    ("admin_url", "runtime_url", "role"),
    [
        (
            "postgresql+asyncpg://runtime:a@database.invalid/control",
            "postgresql+asyncpg://runtime:r@database.invalid/control",
            "runtime",
        ),
        (
            "postgresql+asyncpg://operator:a@database.invalid/other",
            "postgresql+asyncpg://runtime:r@database.invalid/control",
            "runtime",
        ),
        (
            "postgresql+asyncpg://operator:a@database.invalid/control",
            "postgresql+asyncpg://not_runtime:r@database.invalid/control",
            "runtime",
        ),
    ],
)
def test_bootstrap_configuration_rejects_identity_or_database_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    admin_url: str,
    runtime_url: str,
    role: str,
) -> None:
    module = _bootstrap_module()
    monkeypatch.setenv("EXOMEM_PROVISIONER_DATABASE_ADMIN_URL", admin_url)
    monkeypatch.setenv("EXOMEM_PROVISIONER_DATABASE_URL", runtime_url)
    monkeypatch.setenv("EXOMEM_PROVISIONER_DATABASE_SCHEMA", "provisioner_data")
    monkeypatch.setenv("EXOMEM_PROVISIONER_DATABASE_ROLE", role)

    with pytest.raises(module.DatabaseBootstrapError):
        module.load_configuration(require_admin=True)


def test_packaged_revision_state_accepts_empty_known_or_exact_and_rejects_other() -> None:
    module = _bootstrap_module()
    known = frozenset({"0001", "0002", "0003"})

    assert module.validate_revision_state((), known=known, head="0003", exact=False) is None
    assert module.validate_revision_state(("0001",), known=known, head="0003", exact=False) is None
    assert module.validate_revision_state(("0003",), known=known, head="0003", exact=True) is None
    with pytest.raises(module.DatabaseBootstrapError):
        module.validate_revision_state(("0001",), known=known, head="0003", exact=True)
    with pytest.raises(module.DatabaseBootstrapError):
        module.validate_revision_state(("future",), known=known, head="0003", exact=False)
    with pytest.raises(module.DatabaseBootstrapError):
        module.validate_revision_state(("0001", "0002"), known=known, head="0003", exact=False)


def test_packaged_migrations_must_have_one_head_matching_runtime_revision(
    tmp_path: Path,
) -> None:
    module = _bootstrap_module()
    root = tmp_path / "migrations"
    versions = root / "alembic/versions"
    versions.mkdir(parents=True)
    (root / "alembic.ini").write_text(
        "[alembic]\nscript_location = %(here)s/alembic\n",
        encoding="utf-8",
    )
    (root / "alembic/script.py.mako").write_text("", encoding="utf-8")
    (root / "alembic/env.py").write_text("", encoding="utf-8")
    (versions / "0001.py").write_text(
        "revision = '9999_wrong_head'\ndown_revision = None\n",
        encoding="utf-8",
    )

    with pytest.raises(module.DatabaseBootstrapError, match="packaged migration head"):
        module.load_packaged_migrations(root)
