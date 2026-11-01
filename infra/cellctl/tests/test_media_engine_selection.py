"""cloud-multimodal-processing D7: an engine's switch renders into the selected cells and the fleet default."""

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
    monkeypatch.setenv("CELLCTL_CELL_DATA_BACKUP_BUCKET_NAME", "backup-bucket")
    monkeypatch.setenv("CELLCTL_CELL_DATA_STAGING_BUCKET_NAME", "staging-bucket")
    monkeypatch.setenv("CELLCTL_CELL_DATA_ENDPOINT", "https://s3.cell-data.example")
    monkeypatch.setenv("CELLCTL_MEDIA_ENGINE_CELL_IDS", json.dumps({"documents": [CANARY], "ocr": [CANARY]}))
    selected = main.build_cluster_config()
    unselected = dataclasses.replace(selected, media_engine_cell_ids={})

    # A canary restarts its own cell; every other cell keeps the pod it runs.
    assert _digest(OTHER, selected) == _digest(OTHER, unselected)
    assert _digest(CANARY, selected) != _digest(CANARY, unselected)


async def _reconcile(cell_db, config: reconcile.ClusterConfig, *cell_ids: str) -> FakeClusterGateway:
    for cell_id in cell_ids:
        await _seed_cell(cell_db, cell_id, cell_id)
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = FakeClusterGateway()
    try:
        await reconcile.reconcile_once(connection, cluster, FakeB2(), FakeHetznerVolumeProvider(),
                                       _secrets_config(), config, now=datetime(2026, 1, 1, tzinfo=UTC))
    finally:
        await connection.close()
    return cluster


def _engines(cluster: FakeClusterGateway, cell_id: str) -> str | None:
    pod = cluster.applied[("exo-cell-" + cell_id, "StatefulSet", "cell")]["spec"]["template"]["spec"]
    env = {entry["name"]: entry.get("value") for entry in pod["containers"][0]["env"]}
    return env.get("EXOMEM_MEDIA_ENGINES")


def _digest(cell_id: str, config: reconcile.ClusterConfig) -> str:
    row = CellRow(cell_id=cell_id, tenant_id=tenant_uuid(cell_id), storage_gib=10,
                  rollout_priority=1, desired_state="running", desired_image=None, generation=1)
    return reconcile._compute_render_digest(row, config, _secrets_config())


async def test_reconcile_renders_the_engine_switch_into_selected_cells_only(cell_db) -> None:
    config = dataclasses.replace(
        _cluster_config(), media_engine_cell_ids={"documents": (CANARY, OTHER), "ocr": (CANARY,)}
    )
    cluster = await _reconcile(cell_db, config, CANARY, OTHER)

    assert _engines(cluster, CANARY) == "documents,ocr"
    assert _engines(cluster, OTHER) == "documents"


async def test_a_cell_in_no_list_runs_the_fleet_default(cell_db) -> None:
    config = dataclasses.replace(_cluster_config(), media_engine_fleet_default=("ocr", "documents"))
    cluster = await _reconcile(cell_db, config, CANARY, OTHER)

    # Sorted, so the same set renders the same value whatever order the chart lists it in.
    assert _engines(cluster, CANARY) == "documents,ocr"
    assert _engines(cluster, OTHER) == "documents,ocr"


async def test_a_cells_own_entries_add_to_the_fleet_default(cell_db) -> None:
    config = dataclasses.replace(
        _cluster_config(),
        media_engine_fleet_default=("documents",),
        media_engine_cell_ids={"ocr": (CANARY,), "documents": (CANARY,)},
    )
    cluster = await _reconcile(cell_db, config, CANARY, OTHER)

    assert _engines(cluster, CANARY) == "documents,ocr"
    assert _engines(cluster, OTHER) == "documents"


async def test_an_empty_fleet_default_renders_no_switch_into_a_cell(cell_db, monkeypatch) -> None:
    monkeypatch.setenv("CELLCTL_B2_BUCKET_NAME", "bucket")
    monkeypatch.setenv("CELLCTL_B2_ENDPOINT", "https://s3.example")
    monkeypatch.setenv("CELLCTL_CELL_DATA_BACKUP_BUCKET_NAME", "backup-bucket")
    monkeypatch.setenv("CELLCTL_CELL_DATA_STAGING_BUCKET_NAME", "staging-bucket")
    monkeypatch.setenv("CELLCTL_CELL_DATA_ENDPOINT", "https://s3.cell-data.example")
    monkeypatch.setenv("CELLCTL_MEDIA_ENGINE_FLEET_DEFAULT", "[]")
    config = dataclasses.replace(main.build_cluster_config(), media_engine_cell_ids={"ocr": (CANARY,)})
    assert config.media_engine_fleet_default == ()

    cluster = await _reconcile(cell_db, config, CANARY, OTHER)

    # No variable at all, as before the default existed, so no other cell's pod changes.
    assert _engines(cluster, OTHER) is None
    assert _engines(cluster, CANARY) == "ocr"


def test_a_default_that_names_what_a_cell_already_runs_restarts_no_cell(monkeypatch) -> None:
    monkeypatch.setenv("CELLCTL_B2_BUCKET_NAME", "bucket")
    monkeypatch.setenv("CELLCTL_B2_ENDPOINT", "https://s3.example")
    monkeypatch.setenv("CELLCTL_CELL_DATA_BACKUP_BUCKET_NAME", "backup-bucket")
    monkeypatch.setenv("CELLCTL_CELL_DATA_STAGING_BUCKET_NAME", "staging-bucket")
    monkeypatch.setenv("CELLCTL_CELL_DATA_ENDPOINT", "https://s3.cell-data.example")
    per_cell = {"documents": (CANARY, OTHER), "ocr": (CANARY, OTHER)}
    rolled_out = dataclasses.replace(main.build_cluster_config(), media_engine_cell_ids=per_cell)
    with_default = dataclasses.replace(rolled_out, media_engine_fleet_default=("ocr", "documents"))

    assert _digest(CANARY, with_default) == _digest(CANARY, rolled_out)
    assert _digest(OTHER, with_default) == _digest(OTHER, rolled_out)

    # The precondition matters: a cell the per-cell list misses does change.
    partial = dataclasses.replace(rolled_out, media_engine_cell_ids={"documents": (CANARY,), "ocr": (CANARY,)})
    partial_with_default = dataclasses.replace(partial, media_engine_fleet_default=("ocr", "documents"))
    assert _digest(OTHER, partial_with_default) != _digest(OTHER, partial)
