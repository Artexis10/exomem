"""Rendered Helm contract for stopping the old hosted actors during Cloud rollout."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
CHART = ROOT / "infra/helm/platform"
HELM = shutil.which("helm")

LEGACY_DEPLOYMENTS = {
    "exomem-gateway": 2,
    "exomem-provisioner-api": 2,
    "exomem-provisioner-worker": 1,
    "exomem-volume-worker": 1,
}
LEGACY_CRONJOBS = {
    "exomem-capacity-receipt-collector",
    "exomem-hosted-scheduler-exomem-access-delivery",
    "exomem-hosted-scheduler-exomem-reconcile",
    "exomem-hosted-scheduler-exomem-export-gc",
    "exomem-durability-actions",
    "exomem-export-gc",
    "exomem-durability-backup",
    "exomem-database-backup",
    "exomem-deletion-dispatcher",
}


def _render(*overrides: str) -> tuple[subprocess.CompletedProcess[str], list[dict[str, Any]]]:
    result = subprocess.run(
        [
            HELM, "template", "legacy-pause-test", str(CHART),
            "--namespace", "exomem-platform",
            "--values", str(CHART / "values.validation.yaml"),
            "--set", "gateway.enabled=true",
            "--set", "gateway.replicas=2",
            "--set", "provisioner.replicas=2",
            "--set-string", "gateway.image=ghcr.io/substrate-systems/substrate-gateway@sha256:" + "a" * 64,
            "--set-string", "gateway.originHostname=legacy.example.test",
            "--set-string", "gateway.databaseEgressCidrs[0]=10.0.1.5/32",
            *overrides,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    documents = [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]
    return result, documents


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
@pytest.mark.parametrize("upgrade", [False, True])
@pytest.mark.parametrize(
    ("paused", "cellctl", "cloud_gateway"),
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (False, False, True),
        (False, True, True),
        (True, True, True),
    ],
)
def test_legacy_actors_follow_explicit_or_cloud_pause(
    paused: bool, cellctl: bool, cloud_gateway: bool, upgrade: bool,
) -> None:
    overrides = [
        "--set", f"cellctl.enabled={str(cellctl).lower()}",
        "--set", f"cloudGateway.enabled={str(cloud_gateway).lower()}",
    ]
    if paused:
        overrides.extend(("--set", "legacyHosted.paused=true"))
    if upgrade:
        overrides.append("--is-upgrade")
    result, documents = _render(*overrides)
    assert result.returncode == 0, result.stderr

    deployments = {
        doc["metadata"]["name"]: doc
        for doc in documents if doc.get("kind") == "Deployment"
    }
    cronjobs = {
        doc["metadata"]["name"]: doc
        for doc in documents if doc.get("kind") == "CronJob"
    }
    effective_pause = paused or cellctl or cloud_gateway
    migration_jobs = [
        doc for doc in documents
        if doc.get("kind") == "Job"
        and doc["metadata"]["name"] == "exomem-provisioner-database-migration"
    ]
    assert len(migration_jobs) == (0 if effective_pause else 1)
    if migration_jobs:
        assert migration_jobs[0]["spec"]["template"]["spec"]["containers"][0]["command"] == [
            "exomem-provisioner-database-validate" if upgrade else "exomem-provisioner-database-migrate"
        ]
    for name, configured_replicas in LEGACY_DEPLOYMENTS.items():
        assert deployments[name]["spec"]["replicas"] == (0 if effective_pause else configured_replicas), name
    assert set(cronjobs) == LEGACY_CRONJOBS
    for name, cronjob in cronjobs.items():
        serves_cloud = name in {
            "exomem-hosted-scheduler-exomem-reconcile",
            "exomem-hosted-scheduler-exomem-access-delivery",
        }
        expected_suspend = effective_pause and not (
            serves_cloud and (cellctl or cloud_gateway)
        )
        assert cronjob["spec"].get("suspend", False) is expected_suspend, name

    # Keep support workloads alive while the legacy actors are stopped.
    for name in ("exomem-cloudflared", "exomem-hosted-scheduler-collector", "exomem-hosted-scheduler-alerts"):
        assert deployments[name]["spec"]["replicas"] > 0, name


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_legacy_pause_value_must_be_boolean() -> None:
    result, _ = _render("--set-string", "legacyHosted.paused=yes")
    assert result.returncode != 0
    assert "/legacyHosted/paused" in result.stderr


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_disabled_legacy_gateway_stays_absent_when_paused() -> None:
    result, documents = _render("--set", "gateway.enabled=false", "--set", "legacyHosted.paused=true")
    assert result.returncode == 0, result.stderr
    deployments = {
        doc["metadata"]["name"]: doc
        for doc in documents if doc.get("kind") == "Deployment"
    }
    assert "exomem-gateway" not in deployments
    for name in LEGACY_DEPLOYMENTS.keys() - {"exomem-gateway"}:
        assert deployments[name]["spec"]["replicas"] == 0, name
