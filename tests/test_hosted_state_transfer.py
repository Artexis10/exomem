"""Prevent a shared-host handover from mutating resources or losing recovery state."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "infra/scripts/inspect_terraform_plan.py"
spec = importlib.util.spec_from_file_location("state_transfer_inspector", SCRIPT)
assert spec is not None and spec.loader is not None
inspector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inspector)


def _manifest(tmp_path: Path, *, corrupt_secret: bool = False, duplicate_owner: bool = False) -> dict:
    key = {
        "mode": "managed", "type": "b2_application_key", "name": "control_db_pgbackrest",
        "provider": 'provider["registry.terraform.io/backblaze/b2"]',
        "instances": [{"schema_version": 0,
                       "attributes": {"id": "backup-key-id", "application_key": "retained-private-value"},
                       "sensitive_attributes": [[{"type": "get_attr", "value": "application_key"}]],
                       "private": "opaque-provider-state", "dependencies": ["b2_bucket.control_db_pgbackrest"]}],
    }
    neighbour = {"mode": "managed", "type": "b2_bucket", "name": "other_product",
                 "provider": key["provider"], "instances": [{"attributes": {"id": "other-bucket-id"}}]}
    source = {"version": 4, "terraform_version": "1.15.8", "serial": 6,
              "lineage": "source-lineage", "outputs": {}, "resources": [key, neighbour]}
    moved = copy.deepcopy(key)
    moved["instances"][0].pop("dependencies")
    if corrupt_secret:
        moved["instances"][0]["attributes"]["application_key"] = "lost-private-value"
    target = {**source, "serial": 1, "lineage": "target-lineage", "resources": [moved]}
    after = {**source, "serial": 7, "resources": [neighbour, key] if duplicate_owner else [neighbour]}
    manifest = {"schema_version": 1, "source_workspace": "product-durability",
                "target_workspace": "shared-durability",
                "resources": [{"address": "b2_application_key.control_db_pgbackrest", "provider_id": "backup-key-id"}],
                "snapshots": {"target_before": None}}
    for name, value in (("source_before", source), ("source_after", after), ("target_after", target)):
        path = tmp_path / (name + ".json")
        path.write_text(json.dumps(value))
        manifest["snapshots"][name] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return manifest


def test_native_move_preserves_backup_credentials_and_unrelated_resources(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    assert inspector.verify_transfer(manifest) == 1


def test_transfer_rejects_lost_backup_credentials_without_echoing_them(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, corrupt_secret=True)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    result = subprocess.run([sys.executable, str(SCRIPT), "--state-transfer", str(path)], capture_output=True, text=True)
    assert result.returncode == 2
    assert "resource contents" in result.stderr
    assert "retained-private-value" not in result.stdout + result.stderr
    assert "lost-private-value" not in result.stdout + result.stderr


def test_transfer_rejects_two_active_owners(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="source resources"):
        inspector.verify_transfer(_manifest(tmp_path, duplicate_owner=True))


def test_transfer_rejects_a_second_owner_hidden_under_an_indexed_alias(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    for name in ("source_before", "source_after"):
        entry = manifest["snapshots"][name]
        path = Path(entry["path"])
        state = json.loads(path.read_bytes())
        alias = copy.deepcopy(json.loads(Path(manifest["snapshots"]["target_after"]["path"]).read_bytes())["resources"][0])
        alias["name"] = "other_management_name"
        alias["instances"][0]["index_key"] = "backup"
        state["resources"].append(alias)
        path.write_text(json.dumps(state))
        entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="physical ownership"):
        inspector.verify_transfer(manifest)


def test_transfer_rejects_a_stale_reviewed_snapshot(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    Path(manifest["snapshots"]["source_before"]["path"]).write_text("{}")
    with pytest.raises(ValueError, match="reviewed hash"):
        inspector.verify_transfer(manifest)


def test_transfer_rejects_a_different_provider_resource(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    manifest["resources"][0]["provider_id"] = "another-backup-key"
    with pytest.raises(ValueError, match="resource manifest"):
        inspector.verify_transfer(manifest)


def test_state_only_plan_rejects_the_network_update_that_can_power_off_the_host() -> None:
    plan = {"resource_changes": [{"address": "hcloud_server.control", "mode": "managed",
                                  "change": {"actions": ["update"]}}]}
    assert inspector.inspect(plan, set(), state_only=True)


def test_state_only_plan_rejects_external_effects_hidden_outside_resource_changes() -> None:
    plan = {"resource_changes": [], "configuration": {"root_module": {
        "module_calls": {"nested": {"module": {"resources": [{"provisioners": [{"type": "local-exec"}]}]}}}}}}
    assert inspector.inspect(plan, set(), state_only=True)


def test_state_only_plan_allows_retiring_sensitive_outputs_without_provider_changes() -> None:
    plan = {"resource_changes": [{"address": "b2_bucket.other_product", "change": {"actions": ["no-op"]}}],
            "output_changes": {"control_db_pgbackrest_application_key": {
                "actions": ["delete"], "after": None, "after_sensitive": False}}}
    assert inspector.inspect(plan, set(), state_only=True) == []
