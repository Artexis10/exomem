"""add-face-search D9a: the operator's face search enablement reaches the named cell and no other."""

from __future__ import annotations

import dataclasses
import json

import pytest

from cellctl import main, reconcile
from cellctl.manifests import check_model_env, render_statefulset
from cellctl.state import CellRow

from .test_manifests import _spec
from .test_reconcile import _secrets_config, tenant_uuid

ENABLED = "aaaaaaaaaaaaaaaa"
OTHER = "bbbbbbbbbbbbbbbb"


def _digest(cell_id: str, config: reconcile.ClusterConfig) -> str:
    row = CellRow(cell_id=cell_id, tenant_id=tenant_uuid(cell_id), storage_gib=10,
                  rollout_priority=1, desired_state="running", desired_image=None, generation=1)
    return reconcile._compute_render_digest(row, config, _secrets_config())


def _faces_env(cell_id: str, config: reconcile.ClusterConfig) -> str | None:
    spec = _spec(cell_id=cell_id, faces_hosted=config.faces_hosted_for_cell(cell_id))
    env = render_statefulset(spec)["spec"]["template"]["spec"]["containers"][0]["env"]
    return {entry["name"]: entry.get("value") for entry in env}.get("EXOMEM_FACES_HOSTED")


def test_only_the_cell_the_operator_names_runs_faces(monkeypatch) -> None:
    monkeypatch.setenv("CELLCTL_B2_BUCKET_NAME", "bucket")
    monkeypatch.setenv("CELLCTL_B2_ENDPOINT", "https://s3.example")
    monkeypatch.setenv("CELLCTL_CELL_DATA_BACKUP_BUCKET_NAME", "backup-bucket")
    monkeypatch.setenv("CELLCTL_CELL_DATA_STAGING_BUCKET_NAME", "staging-bucket")
    monkeypatch.setenv("CELLCTL_CELL_DATA_ENDPOINT", "https://s3.cell-data.example")
    monkeypatch.setenv("CELLCTL_FACES_HOSTED_CELL_IDS", json.dumps([ENABLED]))
    selected = main.build_cluster_config()
    unselected = dataclasses.replace(selected, faces_hosted_cell_ids=())

    # The runtime's face_store.deployment_refusal reads exactly "1"; every other cell refuses faces.
    assert _faces_env(ENABLED, selected) == "1"
    assert _faces_env(OTHER, selected) is None
    assert _digest(ENABLED, selected) != _digest(ENABLED, unselected)
    assert _digest(OTHER, selected) == _digest(OTHER, unselected)


def test_a_cluster_wide_model_env_cannot_enable_faces_in_every_cell() -> None:
    with pytest.raises(ValueError, match="EXOMEM_FACES_HOSTED"):
        check_model_env({"EXOMEM_FACES_HOSTED": "1"})
