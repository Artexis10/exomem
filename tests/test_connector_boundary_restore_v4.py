"""The full restore adapter retains destination v4 authority and exact continuity."""

import json
import os
import shutil
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest
import test_governance_schema_migration_plan as migration_tests
from test_connector_boundary import PRIVATE, SCOPE
from test_connector_boundary import configured_boundary as configured_boundary
from test_governance_schema_migration_plan import _stage_reviewed_migration
from test_hosted_portability import _context

from exomem import hosted_portability as portability
from exomem import init as init_module
from exomem import state_migration
from exomem.governance import (
    authorization_custody,
    policy,
    principal,
    receipts,
    schema_migration,
    store,
    tool,
)


@pytest.mark.parametrize(("boundary", "damage"), [
    ("v4_after_policy_receipt_intent", None), ("v4_after_registry_ack", None),
    ("v4_after_policy_receipt_terminal", None), ("v4_after_mirror_effect", None),
    ("v4_after_policy_receipt_terminal", "later_scope"),
    ("v4_after_policy_receipt_terminal", "missing_custody"),
    ("v4_after_policy_receipt_terminal", "changed_custody"),
    ("v4_after_policy_receipt_terminal", "substituted_inode"),
    ("v4_after_policy_receipt_terminal", "competing_tree"),
])
def test_v4_full_restore_resumes_same_proposal(configured_boundary, vault, tmp_path, monkeypatch, boundary, damage):
    config, _ = configured_boundary
    authority = state_migration.assert_offline_migration_authority(source="isolated enrolled full restore")
    added_scope = "01ARZ3NDEKTSV4RRFFQ69G5FAB"
    with principal.request_scope(principal.owner_principal(surface="cli")):
        init_module.init_vault(vault, force=True)
    (policy.governance_root(vault) / "scopes/second.yaml").write_text(
        f"governance_version: 1\nid: {added_scope}\ntags: [private-label]\n")
    configuration = json.loads(config.read_text())
    configuration["default_denied_scope_ids"].append(added_scope)
    config.write_text(json.dumps(configuration))
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    with principal.request_scope(principal.owner_principal(surface="cli")):
        exported = portability.export_quiesced_vault(vault, tmp_path / "exports", context=_context())
    prepared = portability.prepare_restore(exported.archive_path, tmp_path / "staging", context=_context(lifecycle_state="restore-staging"))
    destination = tmp_path / "destination"
    restore_scopes = tool.restore_protective_scopes
    commit = tool._commit
    with ExitStack() as maintenance_environment:
        def enroll_then_restore(root, documents, *, authority, recovery):
            maintenance_environment.enter_context(contextmanager(migration_tests._offline_state.__wrapped__)(tmp_path, monkeypatch))
            mirror = policy.governance_root(root) / f"scopes/{SCOPE}.yaml"
            mirror.parent.mkdir(parents=True, exist_ok=True)
            mirror.write_text(tool.restore_scope_documents({SCOPE: documents[SCOPE]})[f"scopes/{SCOPE}.yaml"])
            store.open_connection(root).close()
            now = int(time.time()) - 10
            plan = _stage_reviewed_migration(root, now=now)
            schema_migration.commit_forward_migration(root, expected_plan_digest=plan.plan_digest, now=now + 2)
            monkeypatch.setattr(tool, "restore_protective_scopes", restore_scopes)
            return restore_scopes(root, documents, authority=authority, recovery=recovery)

        def interrupted_commit(*args, **kwargs):
            return commit(*args, **kwargs, crash_at=boundary)

        monkeypatch.setattr(tool, "restore_protective_scopes", enroll_then_restore)
        monkeypatch.setattr(tool, "_commit", interrupted_commit)
        with pytest.raises(portability.PortabilityError, match="PUBLICATION_FAILED") as failure:
            portability.publish_prepared_restore(prepared, destination)
        assert destination.is_dir() and not prepared.staging_root.exists()
        assert store.authorization_session_schema_version_if_readable(destination) == 4, repr(failure.value.__context__)
        monkeypatch.setattr(tool, "_commit", commit)
        prior_events = receipts.event_records(destination)
        if damage is not None:
            if damage == "later_scope":
                mirror = policy.governance_root(destination) / f"scopes/{added_scope}.yaml"
                mirror.write_text(f"governance_version: 1\nid: {added_scope}\npaths: ['Knowledge Base/Other/**']\n")
            elif damage == "missing_custody":
                control = Path(os.environ[authorization_custody.CONTROL_FILE_ENV])
                control.rename(control.with_suffix(".retained"))
            elif damage == "changed_custody":
                control = Path(os.environ[authorization_custody.CONTROL_FILE_ENV])
                control.write_bytes(control.read_bytes() + b"changed")
            elif damage == "substituted_inode":
                retained = tmp_path / "retained-inode"
                destination.rename(retained)
                shutil.copytree(retained, destination)
            else:
                prepared.staging_root.mkdir()
                (prepared.staging_root / "unowned.txt").write_text("competing tree")
            before = {path.relative_to(destination): path.read_bytes() for path in destination.rglob("*") if path.is_file()}
            with pytest.raises(portability.PortabilityError):
                portability.publish_prepared_restore(prepared, destination)
            assert {path.relative_to(destination): path.read_bytes() for path in destination.rglob("*") if path.is_file()} == before
            return
        restored = portability.publish_prepared_restore(prepared, destination)
        assert restored.state == "published"
        assert store.authorization_session_schema_version_if_readable(destination) == 4
        assert added_scope in policy.load(destination).scopes
        assert (destination / PRIVATE).read_bytes() == (vault / PRIVATE).read_bytes()
        for record in prepared.manifest["files"]:
            assert (destination / record["path"]).read_bytes() == (vault / record["path"]).read_bytes()
        assert receipts.event_records(destination)[:len(prior_events)] == prior_events
        assert receipts.verify_chain(destination)["valid"]
        connection = store.open_active_governance_read_connection(destination)
        rows = connection.execute("SELECT status, proposal_json, membership_manifest FROM governance_proposals").fetchall()
        connection.close()
        assert len(rows) == 1 and rows[0][0] == "spent"
        assert json.loads(rows[0][1])["proposal_evidence"] == "restore-continuity/v1"
        assert json.loads(rows[0][2])["schema"] == "restore-continuity/v1"
