from __future__ import annotations

import copy
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "infra/contracts/exomem-hosted-fleet-inventory-v1.schema.json"
SCRIPT = ROOT / "infra/scripts/hosted_fleet_inventory.py"


def _module():
    assert SCRIPT.is_file(), "the fleet inventory reconciler must be committed"
    spec = importlib.util.spec_from_file_location("hosted_fleet_inventory", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _runtime(release: str, marker: str) -> dict[str, str]:
    return {
        "releaseVersion": release,
        "runtimeImage": f"ghcr.io/artexis10/exomem@sha256:{marker * 64}",
        "protocolVersion": "1",
        "agentProfile": "hosted-alpha-agent-v1",
        "gatewayContractDigest": marker * 64,
        "commandFingerprint": marker * 64,
        "schemaDigest": marker * 64,
        "compatibilityDigest": marker * 64,
    }


def _control_runtime(runtime: dict[str, str]) -> dict[str, str]:
    """Substrate owns contract identity, not the deployed OCI image."""

    return {key: value for key, value in runtime.items() if key != "runtimeImage"}


def _kubernetes_runtime(runtime: dict[str, str]) -> dict[str, str]:
    """Kubernetes owns deployed bytes, not promotion compatibility."""

    return {key: value for key, value in runtime.items() if key != "compatibilityDigest"}


def _empty_sources() -> dict[str, dict[str, object]]:
    return {
        "substrate": {
            "artifact": "exomem-hosted-substrate-fleet-observation",
            "schemaVersion": 1,
            "observedAt": "2026-08-21T12:00:00Z",
            "routableCells": [],
            "tenantBindings": [],
            "assignments": [],
            "unfinishedOperations": [],
            "capacityClaims": [],
            "capacityActiveCellCount": 0,
            "reviewerAuthorities": [],
            "reviewerTenants": [],
        },
        "provisioner": {
            "artifact": "exomem-hosted-provisioner-fleet-observation",
            "schemaVersion": 1,
            "observedAt": "2026-08-21T12:00:00Z",
            "desiredCells": [],
            "unfinishedOperations": [],
        },
        "kubernetes": {
            "artifact": "exomem-hosted-kubernetes-fleet-observation",
            "schemaVersion": 1,
            "observedAt": "2026-08-21T12:00:00Z",
            "namespaces": [],
            "helmReleases": [],
            "workloads": [],
            "volumes": [],
        },
    }


def _add_live_cell(
    sources: dict[str, dict[str, object]],
    *,
    cell_id: str,
    runtime: dict[str, str],
    reviewer: bool = False,
) -> None:
    substrate = sources["substrate"]
    substrate["routableCells"].append({"cellId": cell_id, "runtime": _control_runtime(runtime)})
    substrate["tenantBindings"].append({"cellId": cell_id, "status": "active"})
    substrate["capacityClaims"].append({"cellId": cell_id})
    substrate["capacityActiveCellCount"] += 1
    if reviewer:
        substrate["reviewerAuthorities"].append({"cellId": cell_id})
        substrate["reviewerTenants"].append({"cellId": cell_id})

    sources["provisioner"]["desiredCells"].append(
        {"cellId": cell_id, "runtime": dict(runtime), "state": "ready"}
    )
    sources["kubernetes"]["namespaces"].append({"cellId": cell_id})
    sources["kubernetes"]["helmReleases"].append(
        {
            "cellId": cell_id,
            "runtime": _kubernetes_runtime(runtime),
            "driver": "configmap",
            "status": "deployed",
        }
    )
    sources["kubernetes"]["workloads"].append(
        {"cellId": cell_id, "runtimeImage": runtime["runtimeImage"], "ready": True}
    )
    sources["kubernetes"]["volumes"].append({"cellId": cell_id, "status": "bound"})


def test_inventory_schema_is_committed_strict_and_closed() -> None:
    assert SCHEMA.is_file(), "the normalized inventory schema must be committed"
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False


def test_empty_fleet_requires_three_authorities_to_agree() -> None:
    module = _module()
    target = _runtime("0.57.2", "a")

    inventory = module.reconcile_inventory(_empty_sources(), target=target)

    assert inventory["status"] == "empty"
    assert inventory["counts"] == {
        "cells": 0,
        "ordinaryCells": 0,
        "reviewerCells": 0,
        "targetCells": 0,
        "legacyCells": 0,
        "terminalCells": 0,
        "inconsistentCells": 0,
        "activeAssignments": 0,
        "unfinishedOperations": 0,
        "capacityClaims": 0,
    }
    assert inventory["cells"] == []
    assert inventory["issues"] == []
    assert module.zero_fleet_noop(inventory) is True
    Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8"))).validate(inventory)


def test_inventory_projects_the_verified_ten_field_upgrade_target() -> None:
    module = _module()
    runtime = _runtime("0.57.2", "a")
    verified_target = {
        **runtime,
        "sourceCommit": "b" * 40,
        "runtimeCandidateSha256": "c" * 64,
    }

    assert module.runtime_from_upgrade_target(verified_target) == runtime

    verified_target["unexpected"] = "drift"
    with pytest.raises(module.InventoryError, match="target fields"):
        module.runtime_from_upgrade_target(verified_target)


@pytest.mark.parametrize(
    ("reviewer", "classification", "ordinary", "reviewers"),
    [(False, "target", 1, 0), (True, "reviewer", 0, 1)],
)
def test_reconciles_ordinary_and_reviewer_cells(
    reviewer: bool, classification: str, ordinary: int, reviewers: int
) -> None:
    module = _module()
    target = _runtime("0.57.2", "a")
    sources = _empty_sources()
    _add_live_cell(sources, cell_id="cell_1809ce5c", runtime=target, reviewer=reviewer)

    inventory = module.reconcile_inventory(sources, target=target)

    assert inventory["status"] == "consistent"
    assert inventory["cells"][0]["classification"] == classification
    assert inventory["counts"]["ordinaryCells"] == ordinary
    assert inventory["counts"]["reviewerCells"] == reviewers
    assert inventory["counts"]["targetCells"] == 1
    assert inventory["counts"]["legacyCells"] == 0
    assert module.zero_fleet_noop(inventory) is False


def test_substrate_cannot_assert_or_be_required_to_know_the_runtime_image() -> None:
    module = _module()
    target = _runtime("0.57.2", "a")
    sources = _empty_sources()
    _add_live_cell(sources, cell_id="cell_1809ce5c", runtime=target)

    control_runtime = sources["substrate"]["routableCells"][0]["runtime"]
    assert "runtimeImage" not in control_runtime
    inventory = module.reconcile_inventory(sources, target=target)

    assert inventory["status"] == "consistent"
    assert inventory["cells"][0]["runtime"] == target


def test_substrate_contract_drift_still_disagrees_with_image_authorities() -> None:
    module = _module()
    target = _runtime("0.57.2", "a")
    sources = _empty_sources()
    _add_live_cell(sources, cell_id="cell_1809ce5c", runtime=target)
    sources["substrate"]["routableCells"][0]["runtime"]["schemaDigest"] = "b" * 64

    inventory = module.reconcile_inventory(sources, target=target)

    assert inventory["status"] == "inconsistent"
    assert "runtime_identity_divergence" in inventory["issues"]


def test_mixed_and_multiple_legacy_releases_are_retained_deterministically() -> None:
    module = _module()
    target = _runtime("0.57.2", "a")
    legacy_one = _runtime("0.54.1", "b")
    legacy_two = _runtime("0.50.0", "c")
    sources = _empty_sources()
    _add_live_cell(sources, cell_id="cell_target", runtime=target)
    _add_live_cell(sources, cell_id="cell_legacy_b", runtime=legacy_one)
    _add_live_cell(sources, cell_id="cell_legacy_a", runtime=legacy_two)

    inventory = module.reconcile_inventory(sources, target=target)

    assert inventory["status"] == "consistent"
    assert [cell["cellId"] for cell in inventory["cells"]] == [
        "cell_legacy_a",
        "cell_legacy_b",
        "cell_target",
    ]
    assert inventory["counts"]["targetCells"] == 1
    assert inventory["counts"]["legacyCells"] == 2
    assert inventory["legacyRuntimes"] == [legacy_two, legacy_one]


@pytest.mark.parametrize(
    ("mutation", "issue"),
    [
        (
            lambda sources, target: sources["provisioner"]["desiredCells"][0].update(
                {"runtime": _kubernetes_runtime(target)}
            ),
            "runtime_identity_divergence",
        ),
        (
            lambda sources, _target: sources["substrate"]["tenantBindings"][0].update(
                {"status": "destroyed"}
            ),
            "destroyed_cell_ghost",
        ),
        (
            lambda sources, _target: sources["substrate"].update({"tenantBindings": []}),
            "missing_active_binding",
        ),
        (
            lambda sources, _target: sources["substrate"]["routableCells"].append(
                copy.deepcopy(sources["substrate"]["routableCells"][0])
            ),
            "duplicate_routable_cell",
        ),
        (
            lambda sources, _target: sources["substrate"].update({"capacityActiveCellCount": 2}),
            "stale_capacity_count",
        ),
    ],
)
def test_reconciliation_classifies_divergence_ghosts_gaps_and_duplicates(
    mutation, issue: str
) -> None:
    module = _module()
    target = _runtime("0.57.2", "a")
    legacy = _runtime("0.54.1", "b")
    sources = _empty_sources()
    _add_live_cell(sources, cell_id="cell_1809ce5c", runtime=legacy)

    mutation(sources, target)
    inventory = module.reconcile_inventory(sources, target=target)

    assert inventory["status"] == "inconsistent"
    assert issue in inventory["issues"]
    assert inventory["counts"]["inconsistentCells"] >= 1 or issue == "stale_capacity_count"
    with pytest.raises(module.InventoryError, match="inconsistent"):
        module.require_inventory_gate(inventory, action="expand")


def test_contract_gate_requires_zero_legacy_and_no_unfinished_authority() -> None:
    module = _module()
    target = _runtime("0.57.2", "a")
    legacy = _runtime("0.54.1", "b")
    sources = _empty_sources()
    _add_live_cell(sources, cell_id="cell_1809ce5c", runtime=legacy)
    inventory = module.reconcile_inventory(sources, target=target)

    with pytest.raises(module.InventoryError, match="legacy"):
        module.require_inventory_gate(inventory, action="contract")

    target_sources = _empty_sources()
    _add_live_cell(target_sources, cell_id="cell_1809ce5c", runtime=target)
    target_sources["substrate"]["assignments"].append(
        {
            "assignmentId": "assignment_1",
            "cellId": "cell_1809ce5c",
            "status": "active",
            "targetRuntime": _control_runtime(target),
        }
    )
    target_sources["substrate"]["unfinishedOperations"].append(
        {
            "operationId": "operation_1",
            "cellId": "cell_1809ce5c",
            "kind": "rollforward",
            "status": "running",
            "targetRuntime": _control_runtime(target),
        }
    )
    pending = module.reconcile_inventory(target_sources, target=target)
    assert pending["status"] == "consistent"
    with pytest.raises(module.InventoryError, match="unfinished"):
        module.require_inventory_gate(pending, action="contract")

    clean_sources = _empty_sources()
    _add_live_cell(clean_sources, cell_id="cell_1809ce5c", runtime=target)
    clean = module.reconcile_inventory(clean_sources, target=target)
    assert module.require_inventory_gate(clean, action="contract") == clean


def test_inventory_digest_is_canonical_and_source_order_independent() -> None:
    module = _module()
    target = _runtime("0.57.2", "a")
    sources = _empty_sources()
    _add_live_cell(sources, cell_id="cell_b", runtime=target)
    _add_live_cell(sources, cell_id="cell_a", runtime=target)
    reversed_sources = copy.deepcopy(sources)
    for source in reversed_sources.values():
        for value in source.values():
            if isinstance(value, list):
                value.reverse()

    first = module.reconcile_inventory(sources, target=target)
    second = module.reconcile_inventory(reversed_sources, target=target)

    assert first == second
    assert module.inventory_sha256(first) == module.inventory_sha256(second)


class _HttpResponse:
    def __init__(self, payload: object, *, status: int = 200) -> None:
        self.status = status
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, maximum: int) -> bytes:
        return self._body[:maximum]


def test_substrate_collector_uses_private_token_and_bounded_https(
    tmp_path: Path,
) -> None:
    module = _module()
    observation = _empty_sources()["substrate"]
    token = tmp_path / "operator-token"
    token.write_text("not-a-real-token\n", encoding="utf-8")
    token.chmod(0o600)
    seen: dict[str, object] = {}

    def opener(request, *, timeout: float):
        seen["url"] = request.full_url
        seen["authorization"] = request.get_header("Authorization")
        seen["timeout"] = timeout
        return _HttpResponse({"success": True, "observation": observation})

    collected = module.collect_substrate(
        "https://substratesystems.io/api/exomem/admin/fleet",
        token_file=token,
        timeout_seconds=7,
        opener=opener,
    )

    assert collected == observation
    assert seen == {
        "url": "https://substratesystems.io/api/exomem/admin/fleet",
        "authorization": "Bearer not-a-real-token",
        "timeout": 7,
    }


def test_provisioner_collector_runs_only_the_read_only_command_with_a_timeout() -> None:
    module = _module()
    observation = _empty_sources()["provisioner"]
    calls: list[tuple[list[str], float]] = []

    def runner(command, *, capture_output, timeout, check):
        assert capture_output is True and check is False
        calls.append((command, timeout))
        return subprocess.CompletedProcess(command, 0, json.dumps(observation).encode(), b"")

    collected = module.collect_provisioner(timeout_seconds=9, runner=runner)

    assert collected == observation
    assert calls == [
        (
            [
                "kubectl",
                "-n",
                "exomem-platform",
                "exec",
                "deployment/exomem-provisioner-api",
                "--",
                "exomem-provisioner-fleet-observe",
            ],
            9,
        )
    ]


def _legacy_provisioner_deployment() -> dict[str, object]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "exomem-provisioner-api", "namespace": "exomem-platform"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "api",
                            "env": [
                                {
                                    "name": "EXOMEM_PROVISIONER_BEARER",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": "exomem-provisioner-auth",
                                            "key": "credential",
                                        }
                                    },
                                },
                                {
                                    "name": "EXOMEM_PROVISIONER_DATABASE_URL",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": "exomem-provisioner-database",
                                            "key": "url",
                                        }
                                    },
                                },
                                {
                                    "name": "EXOMEM_PROVISIONER_ENVELOPE_KEY",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": "exomem-provisioner-wrapping-key",
                                            "key": "key-material",
                                        }
                                    },
                                },
                                {
                                    "name": "EXOMEM_PROVISIONER_DATABASE_SCHEMA",
                                    "value": "exomem_provisioner",
                                },
                                {
                                    "name": "EXOMEM_PROVISIONER_DATABASE_ROLE",
                                    "value": "exomem_provisioner_runtime",
                                },
                                {
                                    "name": "EXOMEM_PROVISIONER_DEPLOYMENT_LOCK_PATH",
                                    "value": (
                                        "/etc/exomem/deployment-lock/"
                                        "exomem-hosted-deployment-lock-v2.json"
                                    ),
                                },
                                {
                                    "name": "EXOMEM_PROVIDER_RECOVERY_SIGNING_KEY",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": "exomem-provider-recovery-signer",
                                            "key": "private-key",
                                        }
                                    },
                                },
                            ],
                            "volumeMounts": [
                                {
                                    "name": "deployment-lock",
                                    "mountPath": "/etc/exomem/deployment-lock",
                                    "readOnly": True,
                                }
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "deployment-lock",
                            "configMap": {
                                "name": "exomem-hosted-deployment-lock-v2-0123456789abcdef",
                                "defaultMode": 420,
                                "items": [
                                    {
                                        "key": "exomem-hosted-deployment-lock-v2.json",
                                        "path": "exomem-hosted-deployment-lock-v2.json",
                                    }
                                ],
                            },
                        }
                    ],
                }
            }
        },
    }


def test_first_upgrade_observer_uses_a_digest_pinned_minimum_authority_job() -> None:
    module = _module()
    observation = _empty_sources()["provisioner"]
    image = f"ghcr.io/artexis10/exomem-provisioner@sha256:{'a' * 64}"
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if command[-3:] == ["deployment/exomem-provisioner-api", "-o", "json"]:
            output = json.dumps(_legacy_provisioner_deployment()).encode()
        elif command[-5:] == ["create", "-f", "-", "-o", "json"]:
            manifest = json.loads(kwargs["input"])
            pod = manifest["spec"]["template"]["spec"]
            container = pod["containers"][0]
            assert manifest["metadata"]["generateName"] == "exomem-fleet-observer-"
            assert manifest["metadata"]["labels"] == {
                "app.kubernetes.io/name": "exomem-fleet-observer",
                "app.kubernetes.io/part-of": "exomem-hosted",
            }
            assert pod["automountServiceAccountToken"] is False
            assert pod["restartPolicy"] == "Never"
            assert container["image"] == image
            assert container["command"] == ["exomem-provisioner-fleet-observe"]
            assert {item["name"] for item in container["env"]} == {
                "EXOMEM_PROVISIONER_BEARER",
                "EXOMEM_PROVISIONER_DATABASE_URL",
                "EXOMEM_PROVISIONER_ENVELOPE_KEY",
                "EXOMEM_PROVISIONER_DATABASE_SCHEMA",
                "EXOMEM_PROVISIONER_DATABASE_ROLE",
                "EXOMEM_PROVISIONER_TRUSTED_PROXY_IPS",
                "EXOMEM_PROVISIONER_DEPLOYMENT_LOCK_PATH",
                "HOME",
            }
            assert (
                next(
                    item for item in container["env"] if item["name"] == "EXOMEM_PROVISIONER_BEARER"
                )["value"]
                == "observer-has-no-api-authority-0000"
            )
            assert all(
                item["name"] != "EXOMEM_PROVIDER_RECOVERY_SIGNING_KEY" for item in container["env"]
            )
            assert pod["volumes"] == [
                {
                    "name": "temporary",
                    "emptyDir": {"sizeLimit": "16Mi"},
                },
                _legacy_provisioner_deployment()["spec"]["template"]["spec"]["volumes"][0],
            ]
            output = json.dumps(
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {
                        "name": "exomem-fleet-observer-abc12",
                        "namespace": "exomem-platform",
                        "labels": manifest["metadata"]["labels"],
                    },
                }
            ).encode()
        elif "wait" in command:
            output = b""
        elif "logs" in command:
            output = json.dumps(observation).encode()
        elif "delete" in command:
            output = b""
        else:  # pragma: no cover - makes an unexpected effect obvious
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, output, b"")

    collected = module.collect_provisioner(
        timeout_seconds=9,
        bootstrap_image=image,
        runner=runner,
    )

    assert collected == observation
    assert [command for command, _kwargs in calls] == [
        [
            "kubectl",
            "-n",
            "exomem-platform",
            "get",
            "deployment/exomem-provisioner-api",
            "-o",
            "json",
        ],
        ["kubectl", "-n", "exomem-platform", "create", "-f", "-", "-o", "json"],
        [
            "kubectl",
            "-n",
            "exomem-platform",
            "wait",
            "--for=condition=complete",
            "--timeout=9s",
            "job/exomem-fleet-observer-abc12",
        ],
        [
            "kubectl",
            "-n",
            "exomem-platform",
            "logs",
            "job/exomem-fleet-observer-abc12",
        ],
        [
            "kubectl",
            "-n",
            "exomem-platform",
            "delete",
            "job/exomem-fleet-observer-abc12",
            "--ignore-not-found=true",
            "--wait=true",
            "--timeout=9s",
        ],
    ]


def test_first_upgrade_observer_rejects_mutable_or_foreign_images() -> None:
    module = _module()

    for image in (
        "ghcr.io/artexis10/exomem-provisioner:latest",
        f"ghcr.io/other/exomem-provisioner@sha256:{'a' * 64}",
    ):
        with pytest.raises(module.InventoryError, match="bootstrap image is invalid"):
            module.collect_provisioner(bootstrap_image=image)


def test_first_upgrade_observer_cleanup_failure_fails_closed() -> None:
    module = _module()
    image = f"ghcr.io/artexis10/exomem-provisioner@sha256:{'a' * 64}"

    def runner(command, **kwargs):
        if command[-3:] == ["deployment/exomem-provisioner-api", "-o", "json"]:
            output = json.dumps(_legacy_provisioner_deployment()).encode()
            return subprocess.CompletedProcess(command, 0, output, b"")
        if command[-5:] == ["create", "-f", "-", "-o", "json"]:
            output = json.dumps(
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {
                        "name": "exomem-fleet-observer-abc12",
                        "namespace": "exomem-platform",
                        "labels": {
                            "app.kubernetes.io/name": "exomem-fleet-observer",
                            "app.kubernetes.io/part-of": "exomem-hosted",
                        },
                    },
                }
            ).encode()
            return subprocess.CompletedProcess(command, 0, output, b"")
        if "wait" in command:
            return subprocess.CompletedProcess(command, 0, b"", b"")
        if "logs" in command:
            return subprocess.CompletedProcess(
                command, 0, json.dumps(_empty_sources()["provisioner"]).encode(), b""
            )
        assert "delete" in command
        return subprocess.CompletedProcess(command, 1, b"", b"cleanup detail must stay private")

    with pytest.raises(module.InventoryError, match="provisioner collector failed"):
        module.collect_provisioner(bootstrap_image=image, runner=runner)


def _kubernetes_documents(runtime: dict[str, str]) -> dict[str, object]:
    cell_id = "cell_1809ce5c"
    namespace = "exo-a"
    return {
        "namespaces": {
            "items": [
                {
                    "metadata": {
                        "name": namespace,
                        "annotations": {
                            "exomem.io/cell-id": cell_id,
                            "exomem.io/expected-release": runtime["releaseVersion"],
                        },
                    }
                }
            ]
        },
        "workloads": {
            "items": [
                {
                    "metadata": {
                        "name": namespace,
                        "namespace": namespace,
                        "annotations": {"exomem.io/cell-id": cell_id},
                    },
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [
                                    {
                                        "name": "exomem",
                                        "image": runtime["runtimeImage"],
                                        "env": [
                                            {
                                                "name": "EXOMEM_HOSTED_PROTOCOL_VERSION",
                                                "value": runtime["protocolVersion"],
                                            }
                                        ],
                                    }
                                ]
                            }
                        }
                    },
                    "status": {"readyReplicas": 1, "replicas": 1},
                }
            ]
        },
        "volumes": {
            "items": [
                {
                    "metadata": {
                        "name": f"{namespace}-data",
                        "namespace": namespace,
                        "annotations": {"exomem.io/cell-id": cell_id},
                    },
                    "status": {"phase": "Bound"},
                }
            ]
        },
        "helm": {
            "items": [
                {
                    "metadata": {
                        "name": "sh.helm.release.v1.exo-a.v1",
                        "namespace": namespace,
                        "labels": {
                            "owner": "helm",
                            "name": namespace,
                            "status": "deployed",
                        },
                    }
                }
            ]
        },
    }


def test_kubernetes_collector_reconciles_configmap_helm_state_without_secrets() -> None:
    module = _module()
    target = _runtime("0.57.2", "a")
    documents = _kubernetes_documents(target)
    calls: list[tuple[str, ...]] = []

    def runner(command, *, capture_output, timeout, check):
        assert capture_output is True and timeout == 8 and check is False
        calls.append(tuple(command))
        if "namespaces" in command:
            key = "namespaces"
        elif "statefulsets" in command:
            key = "workloads"
        elif "persistentvolumeclaims" in command:
            key = "volumes"
        else:
            key = "helm"
        return subprocess.CompletedProcess(command, 0, json.dumps(documents[key]).encode(), b"")

    collected = module.collect_kubernetes(
        runtime_catalog=[target],
        observed_at="2026-08-21T12:00:00Z",
        timeout_seconds=8,
        runner=runner,
    )

    assert collected == _empty_sources()["kubernetes"] | {
        "namespaces": [{"cellId": "cell_1809ce5c"}],
        "helmReleases": [
            {
                "cellId": "cell_1809ce5c",
                "runtime": target,
                "driver": "configmap",
                "status": "deployed",
            }
        ],
        "workloads": [
            {
                "cellId": "cell_1809ce5c",
                "runtimeImage": target["runtimeImage"],
                "ready": True,
            }
        ],
        "volumes": [{"cellId": "cell_1809ce5c", "status": "bound"}],
    }
    assert len(calls) == 4
    assert all("get" in command and "-o" in command for command in calls)
    assert calls[1].count("statefulsets") == 1


def test_execution_facts_are_derived_from_the_exact_gated_inventory() -> None:
    module = _module()
    target = _runtime("0.57.2", "a")
    legacy = _runtime("0.54.1", "b")
    sources = _empty_sources()
    _add_live_cell(sources, cell_id="cell_legacy", runtime=legacy)
    _add_live_cell(sources, cell_id="cell_target", runtime=target)

    inventory = module.reconcile_inventory(sources, target=target)
    facts = module.execution_inventory_facts(inventory)
    digest = module.inventory_sha256(inventory)

    assert facts == {
        "inventoryStatus": "consistent",
        "inventorySha256": digest,
        "cellCount": 2,
        "legacyCellCount": 1,
        "inventoryEvidenceSha256": digest,
        "cells": [
            {
                "cellId": "cell_legacy",
                "class": "legacy",
                "releaseVersion": "0.54.1",
                "assignmentId": None,
                "operationId": None,
                "status": "pending",
                "beforeVaultSha256": None,
                "afterVaultSha256": None,
                "evidenceSha256": None,
            },
            {
                "cellId": "cell_target",
                "class": "target",
                "releaseVersion": "0.57.2",
                "assignmentId": None,
                "operationId": None,
                "status": "no_op",
                "beforeVaultSha256": None,
                "afterVaultSha256": None,
                "evidenceSha256": None,
            },
        ],
    }


def test_execution_facts_retain_terminal_evidence_but_exclude_destroyed_cells() -> None:
    module = _module()
    target = _runtime("0.57.2", "a")
    sources = _empty_sources()
    sources["substrate"]["tenantBindings"].append(
        {"cellId": "cell_destroyed", "status": "destroyed"}
    )

    inventory = module.reconcile_inventory(sources, target=target)
    facts = module.execution_inventory_facts(inventory)

    assert inventory["counts"]["terminalCells"] == 1
    assert inventory["counts"]["cells"] == 0
    assert inventory["cells"][0]["classification"] == "terminal"
    assert inventory["cells"][0]["runtime"] is None
    assert facts["cellCount"] == 0
    assert facts["cells"] == []


def test_operator_cli_collects_three_authorities_and_writes_private_phase_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _module()
    target = _runtime("0.57.2", "a")
    sources = _empty_sources()
    _add_live_cell(sources, cell_id="cell_target", runtime=target)
    token = tmp_path / "operator-token"
    token.write_text("not-a-real-token", encoding="utf-8")
    token.chmod(0o600)
    target_path = tmp_path / "target.json"
    target_path.write_text(
        json.dumps(
            {
                **target,
                "sourceCommit": "b" * 40,
                "runtimeCandidateSha256": "c" * 64,
            }
        ),
        encoding="utf-8",
    )
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps({"runtimes": [target]}), encoding="utf-8")
    inventory_path = tmp_path / "inventory.json"
    facts_path = tmp_path / "facts.json"
    observer_image = f"ghcr.io/artexis10/exomem-provisioner@sha256:{'f' * 64}"
    provisioner_arguments: dict[str, object] = {}

    monkeypatch.setattr(module, "collect_substrate", lambda *_args, **_kwargs: sources["substrate"])

    def collect_provisioner(**kwargs):
        provisioner_arguments.update(kwargs)
        return sources["provisioner"]

    monkeypatch.setattr(module, "collect_provisioner", collect_provisioner)
    monkeypatch.setattr(module, "collect_kubernetes", lambda **_kwargs: sources["kubernetes"])

    assert (
        module.main(
            [
                "collect",
                "--substrate-endpoint",
                "https://substratesystems.io/api/exomem/admin/fleet",
                "--substrate-token-file",
                os.fspath(token),
                "--runtime-catalog",
                os.fspath(catalog_path),
                "--target",
                os.fspath(target_path),
                "--observed-at",
                "2026-08-21T12:00:00Z",
                "--inventory-output",
                os.fspath(inventory_path),
                "--facts-output",
                os.fspath(facts_path),
                "--provisioner-bootstrap-image",
                observer_image,
            ]
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    facts = json.loads(facts_path.read_text(encoding="utf-8"))
    assert summary == {
        "cellCount": 1,
        "inventorySha256": module.inventory_sha256(inventory),
        "legacyCellCount": 0,
        "status": "consistent",
    }
    assert facts == module.execution_inventory_facts(inventory)
    assert provisioner_arguments == {
        "timeout_seconds": 15,
        "bootstrap_image": observer_image,
    }
    assert stat.S_IMODE(inventory_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(facts_path.stat().st_mode) == 0o600


def test_collector_failures_never_echo_tokens_paths_or_remote_output(tmp_path: Path) -> None:
    module = _module()
    token = tmp_path / "operator-token-with-sensitive-path"
    token.write_text("secret-bearer", encoding="utf-8")
    token.chmod(0o600)

    def failing_opener(_request, *, timeout):
        del timeout
        raise RuntimeError("secret-bearer note title must never escape")

    with pytest.raises(module.InventoryError) as captured:
        module.collect_substrate(
            "https://substratesystems.io/api/exomem/admin/fleet",
            token_file=token,
            opener=failing_opener,
        )
    message = str(captured.value)
    assert message == "substrate collector failed"
    assert "secret" not in message and os.fspath(token) not in message
