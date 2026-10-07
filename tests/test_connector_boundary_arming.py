"""Durable arming survives missing host configuration and interrupted publication."""

import json

import pytest
from test_connector_boundary import configured_boundary as configured_boundary

from exomem import reserved_paths, state_migration
from exomem.governance import connector_boundary, principal


def test_arming_requires_offline_authority_and_missing_configuration_cannot_disarm(configured_boundary, vault, monkeypatch):
    with pytest.raises(state_migration.StateMigrationOfflineRequired):
        state_migration.arm_connector_boundary_offline(vault, authority=object())
    authority = state_migration.assert_offline_migration_authority(source="isolated test stop window")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    assert connector_boundary.COMPATIBILITY_ID in state_migration.recorded_descriptor_ids(vault)
    state_migration.require_vault_state_ready(vault)
    monkeypatch.delenv(connector_boundary.CONFIG_ENV)
    with pytest.raises(state_migration.StateMigrationOfflineRequired):
        state_migration.require_vault_state_ready(vault)
    assert not connector_boundary.unrestricted(vault, principal.owner_principal())


def test_interrupted_arming_cannot_serve_and_exact_retry_repairs_publication(configured_boundary, vault, monkeypatch):
    authority = state_migration.assert_offline_migration_authority(source="isolated test stop window")
    original = reserved_paths._publish_owner_bytes

    def crash(root, path, descriptor, data):
        if descriptor == connector_boundary.DESCRIPTOR_ID:
            raise OSError("simulated interruption after enrollment")
        return original(root, path, descriptor, data)

    monkeypatch.setattr(reserved_paths, "_publish_owner_bytes", crash)
    with pytest.raises(OSError, match="simulated interruption"):
        state_migration.arm_connector_boundary_offline(vault, authority=authority)
    assert connector_boundary.COMPATIBILITY_ID in state_migration.recorded_descriptor_ids(vault)
    with pytest.raises(state_migration.StateMigrationOfflineRequired):
        state_migration.require_vault_state_ready(vault)
    monkeypatch.setattr(reserved_paths, "_publish_owner_bytes", original)
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    state_migration.require_vault_state_ready(vault)
    requirement = connector_boundary.read_requirement(vault)
    assert set(requirement) == {"version", "protected_scopes", "selectors_sha256", "capture_paths"}
    assert "clients" not in json.dumps(requirement)


def test_arming_preserves_active_selectors_and_detects_same_id_reclassification(configured_boundary, vault):
    from exomem.governance import policy

    authority = state_migration.assert_offline_migration_authority(source="isolated test stop window")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    requirement = connector_boundary.read_requirement(vault)
    compiled = policy.compile_protective_scopes(requirement["protected_scopes"])
    source = policy.load(vault)
    assert policy.protective_scope_documents(compiled, frozenset(compiled.scopes)) == (
        policy.protective_scope_documents(source, frozenset(source.scopes)))
    scope = vault / "Knowledge Base/_Governance/scopes/private.yaml"
    scope.write_text(scope.read_text().replace("private-project", "another-private-project"))
    with pytest.raises(state_migration.StateMigrationOfflineRequired):
        state_migration.require_vault_state_ready(vault)


def test_arming_refuses_capture_namespace_with_private_content(configured_boundary, vault):
    target = vault / "Knowledge Base/Capture/old.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\nproject: private-project\n---\nexisting private content\n")
    authority = state_migration.assert_offline_migration_authority(source="isolated capture stop window")
    with pytest.raises(connector_boundary.BoundaryUnavailable):
        state_migration.arm_connector_boundary_offline(vault, authority=authority)


def test_arming_refuses_a_portable_alias_of_the_capture_namespace(configured_boundary, vault):
    """A hidden alias must not become a collision oracle for later portable capture."""
    target = vault / "Knowledge Base/capture/private.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\nproject: private-project\n---\nprivate alias\n")
    authority = state_migration.assert_offline_migration_authority(source="isolated capture alias stop window")
    with pytest.raises(connector_boundary.BoundaryUnavailable):
        state_migration.arm_connector_boundary_offline(vault, authority=authority)
    assert connector_boundary.COMPATIBILITY_ID not in state_migration.recorded_descriptor_ids(vault)
    assert connector_boundary.read_requirement(vault) is None


def test_maintenance_command_arms_the_configured_boundary(configured_boundary, vault, capsys):
    from exomem.__main__ import _simple_maintain_main

    result = _simple_maintain_main(["--vault", str(vault), "--migrate-state", "--offline",
                                    "--arm-connector-boundary", "--json"])
    assert result == 0
    assert json.loads(capsys.readouterr().out)["success"]
    assert connector_boundary.read_requirement(vault) is not None
    state_migration.require_vault_state_ready(vault)
