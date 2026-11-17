"""erase-captured-source: an owner's erasure enablement reaches that cell and no other."""

from __future__ import annotations

import dataclasses
import json

from cellctl import main, reconcile
from cellctl.manifests import render_statefulset
from cellctl.state import CellRow

from .test_manifests import _spec
from .test_reconcile import _secrets_config, tenant_uuid

ENABLED = "aaaaaaaaaaaaaaaa"
OTHER = "bbbbbbbbbbbbbbbb"


def _digest(cell_id: str, config: reconcile.ClusterConfig) -> str:
    row = CellRow(cell_id=cell_id, tenant_id=tenant_uuid(cell_id), storage_gib=10,
                  rollout_priority=1, desired_state="running", desired_image=None, generation=1)
    return reconcile._compute_render_digest(row, config, _secrets_config())


def _policy_env(cell_id: str, config: reconcile.ClusterConfig) -> str | None:
    spec = _spec(cell_id=cell_id, source_erasure_policy=config.source_erasure_policy_for_cell(cell_id))
    env = render_statefulset(spec)["spec"]["template"]["spec"]["containers"][0]["env"]
    return {entry["name"]: entry.get("value") for entry in env}.get("EXOMEM_SOURCE_ERASURE_POLICY")


def test_only_the_cell_whose_owner_enabled_erasure_receives_the_policy(monkeypatch) -> None:
    monkeypatch.setenv("CELLCTL_B2_BUCKET_NAME", "bucket")
    monkeypatch.setenv("CELLCTL_B2_ENDPOINT", "https://s3.example")
    monkeypatch.setenv("CELLCTL_CELL_DATA_BACKUP_BUCKET_NAME", "backup-bucket")
    monkeypatch.setenv("CELLCTL_CELL_DATA_STAGING_BUCKET_NAME", "staging-bucket")
    monkeypatch.setenv("CELLCTL_CELL_DATA_ENDPOINT", "https://s3.cell-data.example")
    monkeypatch.setenv("CELLCTL_SOURCE_ERASURE_CELL_IDS", json.dumps({"records-b": [ENABLED], "records-a": [ENABLED]}))
    selected = main.build_cluster_config()
    unselected = dataclasses.replace(selected, source_erasure_cell_ids={})

    # The runtime reads this exact object; any other cell keeps erasure off and its running pod.
    assert json.loads(_policy_env(ENABLED, selected)) == {"enabled_systems": ["records-a", "records-b"]}
    assert _policy_env(OTHER, selected) is None
    assert _digest(ENABLED, selected) != _digest(ENABLED, unselected)
    assert _digest(OTHER, selected) == _digest(OTHER, unselected)
