"""Reviewer-only artifact transport configuration and render isolation."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import asyncpg
import pytest

from cellctl import main, reconcile
from cellctl.manifests import ResourceSettings, render_network_policies, render_statefulset
from cellctl.state import CellRow
from cellctl.storage.fake_b2 import FakeB2
from cellctl.storage.fake_hetzner import FakeHetznerVolumeProvider

from .test_manifests import _spec
from .test_reconcile import (
    FakeClusterGateway,
    _cluster_config,
    _secrets_config,
    _seed_cell,
    tenant_uuid,
)

BROKER_URL = "http://10.43.0.25:8767"
CELL_ID = "aaaaaaaaaaaaaaaa"


def test_artifact_transport_settings_load_without_discovery(monkeypatch) -> None:
    monkeypatch.setenv("CELLCTL_B2_BUCKET_NAME", "bucket")
    monkeypatch.setenv("CELLCTL_B2_ENDPOINT", "https://s3.example")
    monkeypatch.setenv("CELLCTL_ARTIFACT_BROKER_URL", BROKER_URL)
    monkeypatch.setenv("CELLCTL_ARTIFACT_BROKER_CELL_IDS", '["aaaaaaaaaaaaaaaa"]')
    config = main.build_cluster_config()
    assert config.artifact_broker_url == BROKER_URL
    assert config.artifact_broker_cell_ids == (CELL_ID,)


@pytest.mark.parametrize("endpoint", [
    "http://broker.exomem-cloud.svc:8767", "https://10.43.0.25:8767",
    "http://10.43.0.25:80", "http://8.8.8.8:8767", "http://127.0.0.1:8767",
    "http://169.254.169.254:8767", "http://100.64.0.1:8767", "http://[::1]:8767",
    "http://user@10.43.0.25:8767", "http://10.43.0.25:8767/fetch",
    "http://10.43.0.25:8767?token=x", "http://10.43.0.25:8767#fragment",
])
def test_artifact_transport_refuses_nonliteral_or_unconfined_endpoints(endpoint: str) -> None:
    with pytest.raises(ValueError, match="artifact broker"):
        dataclasses.replace(_cluster_config(), artifact_broker_url=endpoint)


@pytest.mark.parametrize("cell_ids", [
    ("bad",), ("A" * 16,), ("1" * 16,), (CELL_ID, CELL_ID), "aaaaaaaaaaaaaaaa",
])
def test_artifact_transport_refuses_invalid_activation_lists(cell_ids) -> None:
    with pytest.raises(ValueError, match="artifact broker"):
        dataclasses.replace(_cluster_config(), artifact_broker_url=BROKER_URL,
                            artifact_broker_cell_ids=cell_ids)


def test_artifact_transport_requires_endpoint_before_cell_activation() -> None:
    with pytest.raises(ValueError, match="artifact broker"):
        dataclasses.replace(_cluster_config(), artifact_broker_cell_ids=(CELL_ID,))


def test_selected_runtime_gets_only_the_broker_endpoint_and_network_edge() -> None:
    spec = _spec(artifact_broker_url=BROKER_URL)
    runtime = next(p for p in render_network_policies(spec) if p["metadata"]["name"] == "runtime-ingress")
    assert runtime["spec"]["egress"] == [{
        "to": [{
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "exomem-cloud"}},
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "exomem-artifact-broker"}},
        }],
        "ports": [{"protocol": "TCP", "port": 8767}],
    }]
    pod = render_statefulset(spec)["spec"]["template"]["spec"]
    env = {entry["name"]: entry.get("value") for entry in pod["containers"][0]["env"]}
    assert env["EXOMEM_CLOUD_ARTIFACT_BROKER_URL"] == BROKER_URL
    assert not any("ARTIFACT" in entry["name"] for entry in pod["initContainers"][0]["env"])


def test_unselected_cells_keep_the_legacy_digest_when_transport_is_configured() -> None:
    row = CellRow(cell_id=CELL_ID, tenant_id=tenant_uuid("a"), storage_gib=10,
                  rollout_priority=1, desired_state="running", desired_image=None, generation=1)
    # Production's live chart values still set the resources this digest was captured with.
    captured = dataclasses.replace(_cluster_config(), resources=ResourceSettings(cpu_request="250m", memory_request="1Gi"))
    baseline = reconcile._compute_render_digest(row, captured, _secrets_config())
    # This already-converged cell's digest predates optional transport inputs.
    assert baseline == "1987383291acb5cf023ae2f9e7f8af665ef2050121ec61a0cf4eac45f6ec8de8"
    configured = dataclasses.replace(captured, artifact_broker_url=BROKER_URL,
                                    artifact_broker_cell_ids=("bbbbbbbbbbbbbbbb",))
    assert reconcile._compute_render_digest(row, configured, _secrets_config()) == baseline
    selected = dataclasses.replace(configured, artifact_broker_cell_ids=(CELL_ID,))
    assert reconcile._compute_render_digest(row, selected, _secrets_config()) != baseline
    moved = dataclasses.replace(selected, artifact_broker_url="http://10.43.0.26:8767")
    assert reconcile._compute_render_digest(row, moved, _secrets_config()) != reconcile._compute_render_digest(row, selected, _secrets_config())


async def test_reconcile_adds_transport_only_to_the_selected_cell(cell_db) -> None:
    for cell_id in (CELL_ID, "bbbbbbbbbbbbbbbb"):
        await _seed_cell(cell_db, cell_id, cell_id)
    config = dataclasses.replace(_cluster_config(), artifact_broker_url=BROKER_URL,
                                 artifact_broker_cell_ids=(CELL_ID,))
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = FakeClusterGateway()
    try:
        await reconcile.reconcile_once(connection, cluster, FakeB2(), FakeHetznerVolumeProvider(),
                                       _secrets_config(), config, now=datetime(2026, 1, 1, tzinfo=UTC))
        for cell_id in (CELL_ID, "bbbbbbbbbbbbbbbb"):
            namespace = "exo-cell-" + cell_id
            pod = cluster.applied[(namespace, "StatefulSet", "cell")]["spec"]["template"]["spec"]
            env = {entry["name"]: entry.get("value") for entry in pod["containers"][0]["env"]}
            policy = cluster.applied[(namespace, "NetworkPolicy", "runtime-ingress")]["spec"]
            if cell_id == CELL_ID:
                assert env["EXOMEM_CLOUD_ARTIFACT_BROKER_URL"] == BROKER_URL
                assert policy["egress"][0]["ports"] == [{"protocol": "TCP", "port": 8767}]
            else:
                assert "EXOMEM_CLOUD_ARTIFACT_BROKER_URL" not in env
                assert policy["egress"] == []
    finally:
        await connection.close()
