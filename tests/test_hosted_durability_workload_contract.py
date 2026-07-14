from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _document(name: str) -> dict[str, object]:
    return json.loads((ROOT / "infra" / "contracts" / name).read_text(encoding="utf-8"))


def test_durability_workload_commands_and_privileges_are_disjoint() -> None:
    contract = _document("durability-workloads-v1.json")
    workloads = contract["workloads"]

    assert contract["schemaVersion"] == 1
    assert {tuple(value["command"]) for value in workloads.values()} == {
        ("exomem-export-gc",),
        ("exomem-durability-backup-worker",),
        ("exomem-database-backup-worker",),
        ("exomem-durability-actions",),
        ("exomem-deletion-worker",),
        ("exomem-volume-worker",),
    }
    assert workloads["deletion"]["claimActions"] == ["discard", "destroy"]
    assert workloads["deletion"]["kind"] == "CronJob"
    assert workloads["deletion"]["schedule"] == "* * * * *"
    assert workloads["deletion"]["concurrencyPolicy"] == "Forbid"
    assert workloads["deletion"]["maxOperations"] == 1
    assert workloads["deliveryGc"]["automountServiceAccountToken"] is False
    assert workloads["databaseBackup"]["automountServiceAccountToken"] is False
    assert workloads["deletion"]["privateKey"] is None
    assert workloads["deliveryGc"]["privateKey"] is None
    assert workloads["databaseBackup"]["privateKey"].endswith("/private-key")
    signer = "exomem-provider-recovery-signer/private-key"
    assert workloads["vaultBackup"]["privateKey"] == signer
    assert workloads["databaseBackup"]["privateKey"] == signer
    assert workloads["durabilityActions"]["privateKey"] == signer
    assert workloads["volumeLifecycle"]["privateKey"] == signer
    assert workloads["volumeLifecycle"]["publicVerifier"] is False
    assert workloads["vaultBackup"]["publicVerifier"] is False
    assert workloads["databaseBackup"]["publicVerifier"] is False
    assert workloads["durabilityActions"]["publicVerifier"] is False
    assert workloads["deletion"]["publicVerifier"] is True
    assert workloads["deliveryGc"]["publicVerifier"] is False
    assert not any(
        "database-backup" in permission
        for permission in workloads["deletion"]["providerPermissions"]
    )
    assert "databaseBackupDeleteKeyId" not in contract["secretBindings"]
    assert "databaseBackupDeleteKey" not in contract["secretBindings"]
    assert all(
        workload["concurrencyPolicy"] == "Forbid"
        for workload in workloads.values()
        if workload["kind"] == "CronJob"
    )


def test_every_workload_secret_binding_exists_in_the_handoff_matrix() -> None:
    contract = _document("durability-workloads-v1.json")
    matrix = _document("secret-destinations-v1.json")
    destinations = {
        f"{destination['kubernetes_secret']}/{destination['key']}"
        for secret in matrix["secrets"].values()
        for destination in secret["destinations"].values()
        if destination["kind"] == "sops_k8s_secret"
    }

    assert set(contract["secretBindings"].values()) <= destinations
    for workload in contract["workloads"].values():
        private_key = workload["privateKey"]
        if private_key is not None:
            assert private_key in destinations


def test_no_hosted_durability_identity_can_bypass_governance() -> None:
    contract = _document("durability-workloads-v1.json")
    workloads = contract["workloads"]

    bypass_consumers = {
        name
        for name, workload in workloads.items()
        if any("bypass-governance" in value for value in workload["providerPermissions"])
    }
    assert bypass_consumers == set()
    assert "bypassGovernance" not in (
        ROOT / "infra" / "terraform" / "durability" / "storage.tf"
    ).read_text(encoding="utf-8")
