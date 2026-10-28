"""The node media-slot lease in cellctl: which cells get the URL and the egress rule."""

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
SLOT_URL = "http://10.43.0.26:8768"
CELL_ID = "aaaaaaaaaaaaaaaa"
OTHER_CELL_ID = "bbbbbbbbbbbbbbbb"


def _egress(spec) -> list[dict]:
    runtime = next(p for p in render_network_policies(spec) if p["metadata"]["name"] == "runtime-ingress")
    return runtime["spec"]["egress"]


def _runtime_env(spec) -> dict[str, str | None]:
    pod = render_statefulset(spec)["spec"]["template"]["spec"]
    return {entry["name"]: entry.get("value") for entry in pod["containers"][0]["env"]}


def _slot_rule(port: int, pod: str) -> dict:
    return {
        "to": [{
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "exomem-cloud"}},
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": pod}},
        }],
        "ports": [{"protocol": "TCP", "port": port}],
    }


def test_a_selected_cell_gains_exactly_one_egress_rule_and_the_url() -> None:
    spec = _spec(media_slot_url=SLOT_URL)
    assert _egress(spec) == [_slot_rule(8768, "exomem-media-slot-lease")]
    assert _runtime_env(spec)["EXOMEM_CLOUD_MEDIA_SLOT_URL"] == SLOT_URL
    pod = render_statefulset(spec)["spec"]["template"]["spec"]
    assert not any("MEDIA_SLOT" in entry["name"] for entry in pod["initContainers"][0]["env"])


def test_a_cell_with_the_broker_and_the_lease_gets_the_broker_rule_first() -> None:
    spec = _spec(artifact_broker_url=BROKER_URL, media_slot_url=SLOT_URL)
    assert _egress(spec) == [
        _slot_rule(8767, "exomem-artifact-broker"),
        _slot_rule(8768, "exomem-media-slot-lease"),
    ]


def test_a_cell_without_the_lease_renders_as_before() -> None:
    assert _egress(_spec()) == []
    assert "EXOMEM_CLOUD_MEDIA_SLOT_URL" not in _runtime_env(_spec())
    assert _egress(_spec(artifact_broker_url=BROKER_URL)) == [_slot_rule(8767, "exomem-artifact-broker")]


def _digest(cell_id: str, config: reconcile.ClusterConfig) -> str:
    row = CellRow(cell_id=cell_id, tenant_id=tenant_uuid(cell_id), storage_gib=10,
                  rollout_priority=1, desired_state="running", desired_image=None, generation=1)
    return reconcile._compute_render_digest(row, config, _secrets_config())


def test_only_a_selected_cell_sees_its_digest_move() -> None:
    # Production's live chart values still set the resources this digest was captured with.
    captured = dataclasses.replace(_cluster_config(), resources=ResourceSettings(cpu_request="250m", memory_request="1Gi"))
    baseline = _digest(CELL_ID, captured)
    canary = dataclasses.replace(captured, media_slot_url=SLOT_URL, media_slot_cell_ids=(OTHER_CELL_ID,))
    assert _digest(CELL_ID, canary) == baseline  # not selected: no restart
    assert _digest(OTHER_CELL_ID, canary) != _digest(OTHER_CELL_ID, captured)
    fleet = dataclasses.replace(canary, media_slot_all_cells=True)
    assert _digest(CELL_ID, fleet) != baseline  # the fleet switch reaches a cell created later


def _slot_environment(monkeypatch) -> None:
    monkeypatch.setenv("CELLCTL_B2_BUCKET_NAME", "bucket")
    monkeypatch.setenv("CELLCTL_B2_ENDPOINT", "https://s3.example")
    monkeypatch.setenv("CELLCTL_MEDIA_SLOT_URL", SLOT_URL)


def test_the_selection_loads_from_the_environment(monkeypatch) -> None:
    _slot_environment(monkeypatch)
    monkeypatch.setenv("CELLCTL_MEDIA_SLOT_CELL_IDS", f'["{CELL_ID}"]')
    config = main.build_cluster_config()
    assert config.media_slot_for_cell(CELL_ID) == SLOT_URL
    assert config.media_slot_for_cell(OTHER_CELL_ID) == ""
    monkeypatch.setenv("CELLCTL_MEDIA_SLOT_ALL_CELLS", "true")
    assert main.build_cluster_config().media_slot_for_cell(OTHER_CELL_ID) == SLOT_URL


def test_nothing_is_selected_when_the_environment_omits_the_lease(monkeypatch) -> None:
    monkeypatch.setenv("CELLCTL_B2_BUCKET_NAME", "bucket")
    monkeypatch.setenv("CELLCTL_B2_ENDPOINT", "https://s3.example")
    assert main.build_cluster_config().media_slot_for_cell(CELL_ID) == ""


@pytest.mark.parametrize("endpoint", [
    "http://10.43.0.26:8767", "http://media-slot.exomem-cloud.svc:8768", "https://10.43.0.26:8768",
    "http://8.8.8.8:8768", "http://10.43.0.26:8768/path",
])
def test_the_lease_refuses_an_endpoint_that_is_not_a_literal_private_address_on_its_port(endpoint: str) -> None:
    with pytest.raises(ValueError, match="media slot"):
        dataclasses.replace(_cluster_config(), media_slot_url=endpoint)


def test_a_selection_needs_the_endpoint_and_valid_unique_cell_ids() -> None:
    with pytest.raises(ValueError, match="media slot"):
        dataclasses.replace(_cluster_config(), media_slot_cell_ids=(CELL_ID,))
    with pytest.raises(ValueError, match="media slot"):
        dataclasses.replace(_cluster_config(), media_slot_all_cells=True)
    with pytest.raises(ValueError, match="media slot"):
        dataclasses.replace(_cluster_config(), media_slot_url=SLOT_URL, media_slot_cell_ids=(CELL_ID, CELL_ID))


async def test_reconcile_adds_the_lease_only_to_the_selected_cell(cell_db) -> None:
    for cell_id in (CELL_ID, OTHER_CELL_ID):
        await _seed_cell(cell_db, cell_id, cell_id)
    config = dataclasses.replace(_cluster_config(), media_slot_url=SLOT_URL, media_slot_cell_ids=(CELL_ID,))
    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = FakeClusterGateway()
    try:
        await reconcile.reconcile_once(connection, cluster, FakeB2(), FakeHetznerVolumeProvider(),
                                       _secrets_config(), config, now=datetime(2026, 1, 1, tzinfo=UTC))
        for cell_id in (CELL_ID, OTHER_CELL_ID):
            namespace = "exo-cell-" + cell_id
            pod = cluster.applied[(namespace, "StatefulSet", "cell")]["spec"]["template"]["spec"]
            env = {entry["name"]: entry.get("value") for entry in pod["containers"][0]["env"]}
            policy = cluster.applied[(namespace, "NetworkPolicy", "runtime-ingress")]["spec"]
            if cell_id == CELL_ID:
                assert env["EXOMEM_CLOUD_MEDIA_SLOT_URL"] == SLOT_URL
                assert [rule["ports"] for rule in policy["egress"]] == [[{"protocol": "TCP", "port": 8768}]]
            else:
                assert "EXOMEM_CLOUD_MEDIA_SLOT_URL" not in env
                assert policy["egress"] == []
    finally:
        await connection.close()
