"""Armed archives preserve selectors and force older readers to refuse."""

import json
import os
import subprocess
import zipfile

import pytest
from test_connector_boundary import configured_boundary as configured_boundary
from test_hosted_portability import _context

from exomem import hosted_portability as portability
from exomem import state_migration
from exomem.governance import connector_boundary, principal


def test_armed_export_carries_protection_and_actual_v1_reader_refuses(configured_boundary, vault, tmp_path):
    _, authenticate = configured_boundary
    authority = state_migration.assert_offline_migration_authority(source="isolated export stop window")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    with principal.request_scope(authenticate("limited")):
        with pytest.raises(portability.PortabilityError, match="EXPORT_UNAVAILABLE"):
            portability.export_quiesced_vault(vault, tmp_path / "refused", context=_context())
    with principal.request_scope(principal.owner_principal(surface="cli")):
        exported = portability.export_quiesced_vault(vault, tmp_path / "exports", context=_context())
    assert exported.manifest["schema_version"] == 2
    assert exported.manifest["connector_boundary"] == connector_boundary.read_requirement(vault)
    with zipfile.ZipFile(exported.archive_path) as archive:
        assert json.loads(archive.read(connector_boundary.requirement_relative_path())) == exported.manifest["connector_boundary"]
        assert not any("_Governance/" in name for name in archive.namelist())
    older = os.environ.get("EXOMEM_TEST_OLDER_READER_PYTHON")
    assert older, "the real pre-capability runtime must be supplied"
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    result = subprocess.run([older, "-c", "from exomem.hosted_portability import verify_export_archive; import sys; verify_export_archive(sys.argv[1])", str(exported.archive_path)],
                            env=environment, capture_output=True, text=True)
    assert result.returncode != 0
    assert "UNSUPPORTED_MANIFEST_VERSION" in result.stderr


def test_restore_installs_protection_without_source_clients_and_waits_for_destination_config(configured_boundary, vault, tmp_path, monkeypatch):
    from test_connector_boundary import PRIVATE, PUBLIC, SCOPE

    from exomem.governance import egress, policy

    config, _ = configured_boundary
    authority = state_migration.assert_offline_migration_authority(source="isolated restore stop window")
    from exomem import state_paths

    review = state_paths.vault_state_dir(vault) / ".review-state.json"
    review.write_text('{"reviewed": []}\n')
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    with principal.request_scope(principal.owner_principal(surface="cli")):
        exported = portability.export_quiesced_vault(vault, tmp_path / "exports", context=_context())
    prepared = portability.prepare_restore(exported.archive_path, tmp_path / "staging",
        context=_context(lifecycle_state="restore-staging"))
    monkeypatch.delenv(connector_boundary.CONFIG_ENV)
    destination = tmp_path / "destination"
    with pytest.raises(portability.PortabilityError, match="PUBLICATION_FAILED"):
        portability.publish_prepared_restore(prepared, destination)
    assert destination.is_dir() and not prepared.staging_root.exists()
    monkeypatch.setenv(connector_boundary.CONFIG_ENV, str(config))
    restored = portability.publish_prepared_restore(prepared, destination, rebuild_derived=lambda root: None)
    assert (state_paths.vault_state_dir(destination) / ".review-state.json").read_bytes() == review.read_bytes()
    assert restored.live_root == destination
    assert restored.derived_state == "ready"
    assert connector_boundary.COMPATIBILITY_ID in state_migration.recorded_descriptor_ids(destination)
    assert SCOPE in policy.load(destination).scopes
    monkeypatch.delenv(connector_boundary.CONFIG_ENV)
    with pytest.raises(state_migration.StateMigrationOfflineRequired):
        state_migration.require_vault_state_ready(destination)
    destination_config = tmp_path / "destination-config.json"
    data = json.loads(config.read_text())
    data["clients"] = []
    destination_config.write_text(json.dumps(data))
    monkeypatch.setenv(connector_boundary.CONFIG_ENV, str(destination_config))
    state_migration.require_vault_state_ready(destination)
    who = principal.owner_principal(surface="rest")
    assert egress.quick_page_visible(destination, PUBLIC, principal=who)
    assert not egress.quick_page_visible(destination, PRIVATE, principal=who)


def test_protective_scope_restore_refuses_existing_selector_conflict(configured_boundary, vault):
    from exomem.governance import tool

    authority = state_migration.assert_offline_migration_authority(source="isolated restore stop window")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    requirement = connector_boundary.read_requirement(vault)
    scope = vault / "Knowledge Base/_Governance/scopes/private.yaml"
    scope.write_text(scope.read_text().replace("private-project", "different-project"))
    before = scope.read_bytes()
    with pytest.raises(tool.GovernanceError) as error:
        tool.restore_protective_scopes(vault, requirement["protected_scopes"], authority=authority)
    assert error.value.code == "GOVERNANCE_SCOPE_CONFLICT"
    assert scope.read_bytes() == before


def test_actual_older_runtime_accepts_unarmed_root_and_refuses_after_enrollment(configured_boundary, vault):
    older = os.environ.get("EXOMEM_TEST_OLDER_READER_PYTHON")
    assert older, "the real pre-capability runtime must be supplied"
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop(connector_boundary.CONFIG_ENV, None)
    command = [older, "-c", "from exomem.state_migration import require_vault_state_ready; from pathlib import Path; import sys; require_vault_state_ready(Path(sys.argv[1]))", str(vault)]
    before = subprocess.run(command, env=environment, capture_output=True, text=True)
    assert before.returncode == 0, before.stderr
    authority = state_migration.assert_offline_migration_authority(source="isolated downgrade stop window")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    after = subprocess.run(command, env=environment, capture_output=True, text=True)
    assert after.returncode != 0
    assert "StateMigrationManifestError" in after.stderr


@pytest.mark.parametrize("damage", ["selectors", "missing-artifact", "version-downgrade"])
def test_inconsistent_armed_archives_refuse_before_restore_staging(configured_boundary, vault, tmp_path, damage):
    """Partial edits to portable protection must never produce an unprotected restore."""
    authority = state_migration.assert_offline_migration_authority(source="isolated archive stop window")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    with principal.request_scope(principal.owner_principal(surface="cli")):
        exported = portability.export_quiesced_vault(vault, tmp_path / "exports", context=_context())
    with zipfile.ZipFile(exported.archive_path) as archive:
        entries = {entry.filename: (entry, archive.read(entry)) for entry in archive.infolist()}
    manifest = json.loads(entries[portability.MANIFEST_NAME][1])
    if damage == "selectors":
        manifest["connector_boundary"]["selectors_sha256"] = "0" * 64
    elif damage == "missing-artifact":
        entries.pop(connector_boundary.requirement_relative_path())
    else:
        manifest["schema_version"] = 1
        manifest.pop("connector_boundary")
    manifest.pop("overall_digest")
    manifest["overall_digest"] = {"algorithm": "sha256", "value": portability._manifest_digest(manifest)}
    entries[portability.MANIFEST_NAME] = (entries[portability.MANIFEST_NAME][0], json.dumps(manifest).encode())
    damaged = tmp_path / "damaged.zip"
    with zipfile.ZipFile(damaged, "w") as archive:
        for entry, content in entries.values():
            archive.writestr(entry, content)
    staging = tmp_path / "refused-staging"
    with pytest.raises(portability.PortabilityError):
        portability.prepare_restore(damaged, staging, context=_context(lifecycle_state="restore-staging"))
    assert not staging.exists()


@pytest.mark.parametrize("boundary", ["before_proposal", "after_proposal", "after_intent", "after_marker", "after_target_write", "after_terminal", "after_install"])
def test_interrupted_protection_restore_resumes_original_proposal(configured_boundary, vault, tmp_path, monkeypatch, boundary):
    """An interrupted publication retains its inode and original destination evidence."""
    from test_connector_boundary import PRIVATE, SCOPE

    from exomem.governance import policy, receipts, store, tool

    authority = state_migration.assert_offline_migration_authority(source="isolated restore crash proof")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    with principal.request_scope(principal.owner_principal(surface="cli")):
        exported = portability.export_quiesced_vault(vault, tmp_path / "exports", context=_context())
    prepared = portability.prepare_restore(exported.archive_path, tmp_path / "staging", context=_context(lifecycle_state="restore-staging"))
    destination = tmp_path / "destination"
    protected = (vault / PRIVATE).read_bytes()
    proposal = tool._proposal
    commit = tool._commit
    install = state_migration.restore_connector_boundary_offline

    def interrupted_proposal(*args, **kwargs):
        if boundary == "before_proposal":
            raise OSError("process stopped before proposal insertion")
        proposal(*args, **kwargs)
        raise OSError("process stopped after proposal insertion")

    def interrupted_commit(*args, **kwargs):
        return commit(*args, **kwargs, crash_at=boundary)

    def interrupted_install(*args, **kwargs):
        install(*args, **kwargs)
        raise OSError("process stopped after protection installation")

    if boundary in {"before_proposal", "after_proposal"}:
        monkeypatch.setattr(tool, "_proposal", interrupted_proposal)
    elif boundary == "after_install":
        monkeypatch.setattr(state_migration, "restore_connector_boundary_offline", interrupted_install)
    else:
        monkeypatch.setattr(tool, "_commit", interrupted_commit)
    with pytest.raises(portability.PortabilityError, match="PUBLICATION_FAILED"):
        portability.publish_prepared_restore(prepared, destination)
    prior_events = receipts.event_records(destination)
    connection = store.open_readonly_connection(destination)
    prior_ids = connection.execute("SELECT proposal_id FROM governance_proposals").fetchall()
    connection.close()
    monkeypatch.setattr(tool, "_proposal", proposal)
    monkeypatch.setattr(tool, "_commit", commit)
    monkeypatch.setattr(state_migration, "restore_connector_boundary_offline", install)
    result = portability.publish_prepared_restore(prepared, destination, rebuild_derived=lambda root: None)
    assert result.derived_state == "ready"
    assert (destination / PRIVATE).read_bytes() == protected
    assert SCOPE in policy.load(destination).scopes
    assert receipts.event_records(destination)[:len(prior_events)] == prior_events
    assert receipts.verify_chain(destination)["valid"]
    connection = store.open_readonly_connection(destination)
    proposal_ids = connection.execute("SELECT proposal_id FROM governance_proposals").fetchall()
    connection.close()
    assert len(proposal_ids) == 1
    assert not prior_ids or proposal_ids == prior_ids


def test_hosted_restore_recovers_its_protected_publication(configured_boundary, vault, tmp_path, monkeypatch):
    from test_connector_boundary import PRIVATE, SCOPE
    from test_hosted_restore_candidate import _bootstrap, _request, _require_bound_state_ready

    from exomem import hosted_restore
    from exomem import init as init_module
    from exomem.governance import policy, receipts
    from exomem.hosted_runtime import HostedBindingV2

    with principal.request_scope(principal.owner_principal(surface="cli")):
        init_module.init_vault(vault, force=True)
    authority = state_migration.assert_offline_migration_authority(source="isolated hosted restore")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    with principal.request_scope(principal.owner_principal(surface="cli")):
        exported = portability.export_quiesced_vault(vault, tmp_path / "exports",
            context=_context(cell_id="source-cell", vault_id="logical-vault"))
    request = _request(tmp_path, exported)
    def crash(point):
        if point == "protection_restored":
            raise hosted_restore.HostedRestoreCrash(point)
    with pytest.raises(hosted_restore.HostedRestoreCrash):
        hosted_restore.restore_candidate(request, bootstrap_security=_bootstrap, crash_hook=crash)
    destination = tmp_path / "target-vault"
    events = receipts.event_records(destination)
    restored = hosted_restore.restore_candidate(request, bootstrap_security=_bootstrap)
    assert restored.status == "ready"
    assert receipts.event_records(destination) == events
    assert (destination / PRIVATE).read_bytes() == (vault / PRIVATE).read_bytes()
    for record in exported.manifest["files"]:
        assert (destination / record["path"]).read_bytes() == (vault / record["path"]).read_bytes()
    assert SCOPE in policy.load(destination).scopes
    binding = HostedBindingV2(cell_id="target-cell", vault_id="logical-vault", vault_root=destination,
        state_root=tmp_path / "target-state", log_root=tmp_path / "target-log", runtime_uid=os.getuid(), runtime_gid=os.getgid())
    assert _require_bound_state_ready(binding).dual_state is False


@pytest.mark.parametrize("failure", ["process_death", "protection_failure"])
def test_restore_preserves_published_tree_for_exact_crash_recovery(configured_boundary, vault, tmp_path, monkeypatch, failure):
    """A killed process or failed protection step retains the published inode."""
    from dataclasses import asdict
    from pathlib import Path

    from test_connector_boundary import PRIVATE

    authority = state_migration.assert_offline_migration_authority(source="isolated publication crash")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    with principal.request_scope(principal.owner_principal(surface="cli")):
        exported = portability.export_quiesced_vault(vault, tmp_path / "exports", context=_context())
    prepared = portability.prepare_restore(exported.archive_path, tmp_path / "staging", context=_context(lifecycle_state="restore-staging"))
    destination = tmp_path / "destination"
    if failure == "process_death":
        import sys

        script = '''import json, os, sys
from pathlib import Path
from exomem import hosted_portability as p
assert Path(p.__file__).resolve().is_relative_to(Path(sys.argv[5]).resolve())
a = p.verify_export_archive(sys.argv[1])
r = p.PreparedRestore(Path(sys.argv[2]), a.archive_path, a.archive_sha256, a.manifest, p.PortabilityContext(**json.loads(sys.argv[4])))
def killed_publish(staging, live):
    os.replace(staging, live)
    os._exit(73)
p.publish_prepared_restore(r, sys.argv[3], publish=killed_publish)
'''
        child = subprocess.run([sys.executable, "-c", script, str(exported.archive_path), str(prepared.staging_root),
            str(destination), json.dumps(asdict(prepared.context)), str(Path(portability.__file__).parents[1])], capture_output=True, text=True)
        assert child.returncode == 73, child.stderr
    else:
        install = state_migration.restore_connector_boundary_offline
        def stop_after_install(*args, **kwargs):
            install(*args, **kwargs)
            raise OSError("publication interrupted")
        monkeypatch.setattr(state_migration, "restore_connector_boundary_offline", stop_after_install)
        with pytest.raises(portability.PortabilityError, match="PUBLICATION_FAILED"):
            portability.publish_prepared_restore(prepared, destination)
        monkeypatch.setattr(state_migration, "restore_connector_boundary_offline", install)
    assert destination.is_dir()
    assert not prepared.staging_root.exists()
    before = (destination / PRIVATE).read_bytes()
    restored = portability.publish_prepared_restore(prepared, destination)
    assert restored.state == "published"
    assert (destination / PRIVATE).read_bytes() == before


@pytest.mark.parametrize("damage", ["scope", "registry", "unrelated", "receipt", "missing_head", "foreign_chain", "operation", "archive", "destination", "placement"])
def test_restore_retry_refuses_unbound_residue_without_discarding_it(configured_boundary, vault, tmp_path, monkeypatch, damage):
    """A matching directory alone never authorizes altered policy, evidence or request bytes."""
    from dataclasses import replace

    from test_connector_boundary import SCOPE

    from exomem import init as init_module
    from exomem.governance import policy, receipts, store

    with principal.request_scope(principal.owner_principal(surface="cli")):
        init_module.init_vault(vault, force=True)
    authority = state_migration.assert_offline_migration_authority(source="isolated restore tamper proof")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    with principal.request_scope(principal.owner_principal(surface="cli")):
        exported = portability.export_quiesced_vault(vault, tmp_path / "exports", context=_context())
    prepared = portability.prepare_restore(exported.archive_path, tmp_path / "staging", context=_context(lifecycle_state="restore-staging"))
    destination = tmp_path / "destination"
    install = state_migration.restore_connector_boundary_offline
    def interrupted(*args, **kwargs):
        install(*args, **kwargs)
        raise OSError("publication interrupted")
    monkeypatch.setattr(state_migration, "restore_connector_boundary_offline", interrupted)
    with pytest.raises(portability.PortabilityError):
        portability.publish_prepared_restore(prepared, destination)
    monkeypatch.setattr(state_migration, "restore_connector_boundary_offline", install)
    retained = destination
    governance = policy.governance_root(destination)
    if damage == "scope":
        target = governance / f"scopes/{SCOPE}.yaml"
        target.write_text(target.read_text().replace("private-project", "changed-project"))
    elif damage == "registry":
        target = destination / "Knowledge Base/_Schema/context-roles.yaml"
        target.write_bytes(target.read_bytes() + b"\n# changed after publication\n")
    elif damage == "unrelated":
        (governance / "unrelated.txt").write_text("unowned")
    elif damage == "receipt":
        target = next((governance / "events").glob("*/*.jsonl"))
        target.write_bytes(target.read_bytes() + b'{}\n')
    elif damage == "missing_head":
        connection = store.open_connection(destination)
        connection.execute("DELETE FROM receipts_head")
        connection.commit()
        connection.close()
    elif damage == "foreign_chain":
        import shutil

        foreign = tmp_path / "foreign"
        foreign.mkdir()
        with monkeypatch.context() as foreign_environment:
            foreign_environment.delenv(connector_boundary.CONFIG_ENV)
            state_migration.migrate_vault_state_offline(foreign, authority=authority)
            event = receipts.begin_event(foreign, operation="governance_policy_commit", prior="a" * 64, target="b" * 64)
            receipts.commit_event(foreign, event["event_id"], outcome="committed")
        assert receipts.verify_chain(foreign)["valid"]
        for instance in (policy.governance_root(foreign) / "events").iterdir():
            shutil.copytree(instance, governance / "events" / instance.name)
    elif damage == "destination":
        destination = tmp_path / "different-destination"
    elif damage == "placement":
        monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "different-state"))
    elif damage == "operation":
        prepared = replace(prepared, context=replace(prepared.context, operation_id="different-operation"))
    else:
        prepared.source_archive.write_bytes(b"changed archive")
    before = {path.relative_to(retained): path.read_bytes() for path in retained.rglob("*") if path.is_file()}
    with pytest.raises((portability.PortabilityError, ValueError)):
        portability.publish_prepared_restore(prepared, destination)
    assert retained.is_dir() and not prepared.staging_root.exists()
    assert {path.relative_to(retained): path.read_bytes() for path in retained.rglob("*") if path.is_file()} == before


def test_armed_rebuild_failure_retains_changed_bytes_protection_and_portable_state(configured_boundary, vault, tmp_path):
    from test_connector_boundary import PUBLIC

    from exomem import state_paths
    from exomem.governance import policy

    authority = state_migration.assert_offline_migration_authority(source="isolated rebuild integrity proof")
    review = state_paths.vault_state_dir(vault) / ".review-state.json"
    review.write_text('{"reviewed": []}\n')
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    with principal.request_scope(principal.owner_principal(surface="cli")):
        exported = portability.export_quiesced_vault(vault, tmp_path / "exports", context=_context())
    prepared = portability.prepare_restore(exported.archive_path, tmp_path / "staging", context=_context(lifecycle_state="restore-staging"))
    destination = tmp_path / "destination"
    evidence = {}

    def corrupt_rebuild(root):
        (root / PUBLIC).write_text("changed by rebuild\n")
        evidence.update({path.relative_to(root): path.read_bytes() for path in policy.governance_root(root).rglob("*") if path.is_file()})

    with pytest.raises(portability.PortabilityError, match="CANONICAL_INTEGRITY_VIOLATION"):
        portability.publish_prepared_restore(prepared, destination, rebuild_derived=corrupt_rebuild)
    assert (destination / PUBLIC).read_text() == "changed by rebuild\n"
    assert evidence and all((destination / relative).read_bytes() == value for relative, value in evidence.items())
    assert (state_paths.vault_state_dir(destination) / ".review-state.json").read_bytes() == review.read_bytes()
    assert not (destination / "Knowledge Base/.review-state.json").exists()


@pytest.mark.parametrize("tamper", [False, True])
def test_protected_restore_resumes_existing_physical_migration(configured_boundary, vault, tmp_path, monkeypatch, tamper):
    from exomem import state_paths

    authority = state_migration.assert_offline_migration_authority(source="isolated physical migration retry")
    review = state_paths.vault_state_dir(vault) / ".review-state.json"
    review.write_text('{"reviewed": []}\n')
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    with principal.request_scope(principal.owner_principal(surface="cli")):
        exported = portability.export_quiesced_vault(vault, tmp_path / "exports", context=_context())
    prepared = portability.prepare_restore(exported.archive_path, tmp_path / "staging", context=_context(lifecycle_state="restore-staging"))
    destination = tmp_path / "destination"
    crash_point = state_migration._crash_point

    def interrupted(point):
        if point == "after-source-delete":
            raise OSError("stopped after verified portable member relocation")

    monkeypatch.setattr(state_migration, "_crash_point", interrupted)
    with pytest.raises(portability.PortabilityError, match="PUBLICATION_FAILED"):
        portability.publish_prepared_restore(prepared, destination)
    monkeypatch.setattr(state_migration, "_crash_point", crash_point)
    portable = state_paths.vault_state_dir(destination) / ".review-state.json"
    assert portable.read_bytes() == review.read_bytes()
    assert not (destination / "Knowledge Base/.review-state.json").exists()
    if tamper:
        portable.write_text("changed after relocation\n")
        with pytest.raises(portability.PortabilityError):
            portability.publish_prepared_restore(prepared, destination)
        assert portable.read_text() == "changed after relocation\n"
    else:
        restored = portability.publish_prepared_restore(prepared, destination)
        assert restored.state == "published"
        assert portable.read_bytes() == review.read_bytes()
        state_migration.require_vault_state_ready(destination)
