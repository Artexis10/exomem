"""cloud-multimodal-processing D7: an engine's switch renders into the selected cells only."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime

import asyncpg

from cellctl import main, reconcile
from cellctl.state import CellRow
from cellctl.storage.fake_b2 import FakeB2
from cellctl.storage.fake_hetzner import FakeHetznerVolumeProvider

from .test_reconcile import (
    FakeClusterGateway,
    _cluster_config,
    _secrets_config,
    _seed_cell,
    tenant_uuid,
)

CANARY = "aaaaaaaaaaaaaaaa"
OTHER = "bbbbbbbbbbbbbbbb"


def test_only_a_selected_cell_changes_its_render_digest(monkeypatch) -> None:
    monkeypatch.setenv("CELLCTL_B2_BUCKET_NAME", "bucket")
    monkeypatch.setenv("CELLCTL_B2_ENDPOINT", "https://s3.example")
    monkeypatch.setenv("CELLCTL_MEDIA_ENGINE_CELL_IDS", json.dumps({"documents": [CANARY], "ocr": [CANARY]}))
    selected = main.build_cluster_config()
    unselected = dataclasses.replace(selected, media_engine_cell_ids={})

    def digest(cell_id: str, config: reconcile.ClusterConfig) -> str:
        row = CellRow(cell_id=cell_id, tenant_id=tenant_uuid(cell_id), storage_gib=10,
                      rollout_priority=1, desired_state="running", desired_image=None, generation=1)
        return reconcile._compute_render_digest(row, config, _secrets_config())

    # A canary restarts its own cell; every other cell keeps the pod it runs.
    assert digest(OTHER, selected) == digest(OTHER, unselected)
    assert digest(CANARY, selected) != digest(CANARY, unselected)


async def test_reconcile_renders_the_engine_switch_into_selected_cells_only(cell_db) -> None:
    for cell_id in (CANARY, OTHER):
        await _seed_cell(cell_db, cell_id, cell_id)
    config = dataclasses.replace(
        _cluster_config(), media_engine_cell_ids={"documents": (CANARY, OTHER), "ocr": (CANARY,)}
    )
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = FakeClusterGateway()
    try:
        await reconcile.reconcile_once(connection, cluster, FakeB2(), FakeHetznerVolumeProvider(),
                                       _secrets_config(), config, now=datetime(2026, 1, 1, tzinfo=UTC))
    finally:
        await connection.close()

    def engines(cell_id: str) -> str | None:
        pod = cluster.applied[("exo-cell-" + cell_id, "StatefulSet", "cell")]["spec"]["template"]["spec"]
        env = {entry["name"]: entry.get("value") for entry in pod["containers"][0]["env"]}
        return env.get("EXOMEM_MEDIA_ENGINES")

    assert engines(CANARY) == "documents,ocr"
    assert engines(OTHER) == "documents"
