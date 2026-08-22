from __future__ import annotations

import copy
import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra" / "scripts" / "hosted_runtime_upgrade_orchestrator.py"


def _module():
    assert SCRIPT.is_file(), "the reusable runtime-upgrade orchestrator must be committed"
    spec = importlib.util.spec_from_file_location("hosted_runtime_upgrade_orchestrator", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _target() -> dict[str, str]:
    return {
        "releaseVersion": "0.57.2",
        "sourceCommit": "a" * 40,
        "runtimeImage": f"ghcr.io/artexis10/exomem@sha256:{'a' * 64}",
        "runtimeCandidateSha256": "b" * 64,
        "protocolVersion": "1",
        "agentProfile": "hosted-alpha-agent-v1",
        "gatewayContractDigest": "c" * 64,
        "commandFingerprint": "d" * 64,
        "schemaDigest": "e" * 64,
        "compatibilityDigest": "f" * 64,
    }


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


def _inventory(*legacy: tuple[str, dict[str, str]]) -> dict[str, object]:
    target = {
        key: value
        for key, value in _target().items()
        if key not in {"sourceCommit", "runtimeCandidateSha256"}
    }
    cells = []
    for cell_id, runtime in legacy:
        cells.append(
            {
                "cellId": cell_id,
                "classification": "legacy",
                "runtime": runtime,
                "surfaces": {
                    "bindingStatus": "active",
                    "routable": True,
                    "capacityClaim": True,
                    "desiredState": True,
                    "namespace": True,
                    "helmRelease": True,
                    "workload": True,
                    "volume": True,
                    "reviewerAuthority": False,
                    "reviewerPurpose": False,
                    "assignmentIds": [],
                    "unfinishedOperationIds": [],
                },
                "issues": [],
            }
        )
    return {
        "artifact": "exomem-hosted-fleet-inventory",
        "schemaVersion": 1,
        "target": target,
        "status": "empty" if not cells else "consistent",
        "observedAt": "2026-08-21T12:00:00Z",
        "sourceSha256s": {
            "substrate": "1" * 64,
            "provisioner": "2" * 64,
            "kubernetes": "3" * 64,
        },
        "counts": {
            "cells": len(cells),
            "ordinaryCells": len(cells),
            "reviewerCells": 0,
            "targetCells": 0,
            "legacyCells": len(cells),
            "terminalCells": 0,
            "inconsistentCells": 0,
            "activeAssignments": 0,
            "unfinishedOperations": 0,
            "capacityClaims": len(cells),
        },
        "legacyRuntimes": sorted(
            [runtime for _, runtime in legacy], key=lambda value: value["releaseVersion"]
        ),
        "cells": sorted(cells, key=lambda value: value["cellId"]),
        "issues": [],
    }


def _descriptor(runtime: dict[str, str], marker: str) -> dict[str, object]:
    return {
        "runtime": runtime,
        "sourceCommit": marker * 40,
        "contractSha256": marker * 64,
    }


def test_substrate_trust_proof_binds_every_reviewed_consumer_site() -> None:
    module = _module()
    execution = module.upgrade.new_execution(
        execution_id="runtime-0-57-2-20260821t120000z",
        target=_target(),
        created_at="2026-08-21T12:00:00Z",
    )
    report = {
        "artifact": "exomem-hosted-substrate-runtime-trust",
        "schemaVersion": 1,
        "consumerCommit": "4" * 40,
        "target": _target(),
        "pinnedSites": sorted(module.composer._SUBSTRATE_RUNTIME_TRUST_SITES),
        "fixtureSha256s": {"agent": "5" * 64, "gateway": "6" * 64},
    }

    facts = module.prove_substrate_trust(execution, report)

    assert facts == {
        "substrateConsumerCommit": "4" * 40,
        "releaseEvidenceSha256": hashlib.sha256(module.canonical(report)).hexdigest(),
    }
    report["pinnedSites"].pop()
    with pytest.raises(module.OrchestrationError, match="pinned sites"):
        module.prove_substrate_trust(execution, report)


def _inventoried_execution(module, inventory: dict[str, object], pair: dict[str, object]):
    execution = module.upgrade.new_execution(
        execution_id="runtime-0-57-2-20260821t120000z",
        target=_target(),
        created_at="2026-08-21T12:00:00Z",
    )
    execution = module.upgrade.advance_execution(
        execution,
        expected_sha256=module.upgrade.canonical_sha256(execution),
        next_phase="trusted",
        updated_at="2026-08-21T12:01:00Z",
        facts={"substrateConsumerCommit": "4" * 40, "releaseEvidenceSha256": "5" * 64},
    )
    pair_bytes = module.canonical(pair)
    expand, contract = pair["locks"]
    execution = module.upgrade.advance_execution(
        execution,
        expected_sha256=module.upgrade.canonical_sha256(execution),
        next_phase="expanded",
        updated_at="2026-08-21T12:02:00Z",
        facts={
            "exomemCommit": "6" * 40,
            "pairSha256": hashlib.sha256(pair_bytes).hexdigest(),
            "expandSha256": hashlib.sha256(module.canonical(expand)).hexdigest(),
            "contractSha256": hashlib.sha256(module.canonical(contract)).hexdigest(),
            "expandEvidenceSha256": "7" * 64,
        },
    )
    facts = module.inventory.execution_inventory_facts(inventory)
    execution = module.upgrade.advance_execution(
        execution,
        expected_sha256=module.upgrade.canonical_sha256(execution),
        next_phase="inventoried",
        updated_at="2026-08-21T12:03:00Z",
        facts=facts,
    )
    return execution


def _rolling_execution(
    module,
    inventory: dict[str, object],
    pair: dict[str, object],
    *,
    canary_cell_id: str | None = None,
):
    execution = _inventoried_execution(module, inventory, pair)
    _, facts = module.begin_rollout(execution, inventory, canary_cell_id=canary_cell_id)
    return module.upgrade.advance_execution(
        execution,
        expected_sha256=module.upgrade.canonical_sha256(execution),
        next_phase="rolling",
        updated_at="2026-08-21T12:04:00Z",
        facts=facts,
    )


def _pair() -> dict[str, object]:
    common = {
        "artifact": "exomem-hosted-deployment-lock",
        "schemaVersion": 2,
        "runtimeTarget": {
            key: _target()[key]
            for key in (
                "releaseVersion",
                "protocolVersion",
                "agentProfile",
                "gatewayContractDigest",
                "commandFingerprint",
                "schemaDigest",
            )
        },
        "components": {},
        "composition": {},
        "rollback": {},
    }
    return {
        "artifact": "exomem-hosted-deployment-lock-pair",
        "schemaVersion": 2,
        "locks": [
            {**copy.deepcopy(common), "admissionMode": "expand"},
            {**copy.deepcopy(common), "admissionMode": "contract"},
        ],
    }


def test_legacy_evidence_is_derived_exactly_from_reconciled_dependencies() -> None:
    module = _module()
    first = _runtime("0.50.0", "8")
    second = _runtime("0.54.1", "9")
    inventory = _inventory(("cell_b", second), ("cell_a", first))
    descriptors = {
        "artifact": "exomem-hosted-runtime-descriptor-catalog",
        "schemaVersion": 1,
        "units": [_descriptor(second, "b"), _descriptor(first, "a")],
    }

    authority, catalog = module.derive_legacy_evidence(inventory, descriptors)

    assert [unit["releaseVersion"] for unit in authority["units"]] == ["0.50.0", "0.54.1"]
    assert [unit["contractSha256"] for unit in catalog["units"]] == ["a" * 64, "b" * 64]

    descriptors["units"].pop()
    with pytest.raises(module.OrchestrationError, match="exactly match"):
        module.derive_legacy_evidence(inventory, descriptors)


def test_expand_adoption_proof_ignores_only_observation_metadata(monkeypatch) -> None:
    module = _module()
    pair = _pair()
    monkeypatch.setattr(module.composer, "validate_deployment_lock_pair", lambda _pair: None)
    before = _inventory(("cell_a", _runtime("0.54.1", "9")))
    after = copy.deepcopy(before)
    after["observedAt"] = "2026-08-21T12:05:00Z"
    after["sourceSha256s"] = {
        "substrate": "a" * 64,
        "provisioner": "b" * 64,
        "kubernetes": "c" * 64,
    }
    execution = module.upgrade.new_execution(
        execution_id="runtime-0-57-2-20260821t120000z",
        target=_target(),
        created_at="2026-08-21T12:00:00Z",
    )
    execution = module.upgrade.advance_execution(
        execution,
        expected_sha256=module.upgrade.canonical_sha256(execution),
        next_phase="trusted",
        updated_at="2026-08-21T12:01:00Z",
        facts={"substrateConsumerCommit": "4" * 40, "releaseEvidenceSha256": "5" * 64},
    )

    proof, facts = module.prove_expand_adoption(
        execution, pair, before, after, exomem_commit="6" * 40
    )
    assert proof["cellCount"] == 1
    assert facts["expandEvidenceSha256"] == hashlib.sha256(module.canonical(proof)).hexdigest()

    after["cells"][0]["surfaces"]["workload"] = False
    with pytest.raises(module.OrchestrationError, match="tenant fleet"):
        module.prove_expand_adoption(execution, pair, before, after, exomem_commit="6" * 40)


def test_canary_then_sequential_rollforward_stops_on_first_failure(monkeypatch) -> None:
    module = _module()
    pair = _pair()
    monkeypatch.setattr(module.composer, "validate_deployment_lock_pair", lambda _pair: None)
    fleet = _inventory(
        ("cell_b", _runtime("0.54.1", "9")),
        ("cell_a", _runtime("0.50.0", "8")),
    )
    inventoried = _inventoried_execution(module, fleet, pair)
    proof, rollout_facts = module.begin_rollout(inventoried, fleet, canary_cell_id="cell_b")
    assert proof["orderedCellIds"] == ["cell_b", "cell_a"]
    assert (
        rollout_facts["rolloutEvidenceSha256"]
        == hashlib.sha256(module.canonical(proof)).hexdigest()
    )
    execution = module.upgrade.advance_execution(
        inventoried,
        expected_sha256=module.upgrade.canonical_sha256(inventoried),
        next_phase="rolling",
        updated_at="2026-08-21T12:04:00Z",
        facts=rollout_facts,
    )

    assert module.next_rollforward_cell(execution, proof)["cellId"] == "cell_b"
    substituted_plan = copy.deepcopy(proof)
    substituted_plan["orderedCellIds"] = ["cell_a", "cell_b"]
    substituted_plan["canaryCellId"] = "cell_a"
    with pytest.raises(module.OrchestrationError, match="execution authority"):
        module.next_rollforward_cell(execution, substituted_plan)
    inventoried_authority = copy.deepcopy(execution)
    inventoried_authority["cells"][1]["assignmentId"] = "assignment_existing"
    inventoried_authority["cells"][1]["operationId"] = "operation_existing"
    with pytest.raises(module.OrchestrationError, match="inventoried authority"):
        module.record_cell_outcome(
            inventoried_authority,
            expected_sha256=module.upgrade.canonical_sha256(inventoried_authority),
            updated_at="2026-08-21T12:05:00Z",
            facts={
                "cellId": "cell_b",
                "assignmentId": "assignment_substituted",
                "operationId": "operation_substituted",
                "status": "rolling",
                "beforeVaultSha256": "a" * 64,
                "afterVaultSha256": None,
                "evidenceSha256": None,
            },
        )
    started = module.record_cell_outcome(
        execution,
        expected_sha256=module.upgrade.canonical_sha256(execution),
        updated_at="2026-08-21T12:05:00Z",
        facts={
            "cellId": "cell_b",
            "assignmentId": "assignment_b",
            "operationId": "operation_b",
            "status": "rolling",
            "beforeVaultSha256": "a" * 64,
            "afterVaultSha256": None,
            "evidenceSha256": None,
        },
    )
    completed = module.record_cell_outcome(
        started,
        expected_sha256=module.upgrade.canonical_sha256(started),
        updated_at="2026-08-21T12:06:00Z",
        facts={
            "cellId": "cell_b",
            "assignmentId": "assignment_b",
            "operationId": "operation_b",
            "status": "complete",
            "beforeVaultSha256": "a" * 64,
            "afterVaultSha256": "a" * 64,
            "evidenceSha256": "b" * 64,
        },
    )
    assert (
        module.record_cell_outcome(
            completed,
            expected_sha256=module.upgrade.canonical_sha256(completed),
            updated_at="2026-08-21T12:06:00Z",
            facts={
                "cellId": "cell_b",
                "assignmentId": "assignment_b",
                "operationId": "operation_b",
                "status": "complete",
                "beforeVaultSha256": "a" * 64,
                "afterVaultSha256": "a" * 64,
                "evidenceSha256": "b" * 64,
            },
        )
        == completed
    )
    assert module.next_rollforward_cell(completed, proof)["cellId"] == "cell_a"
    failed = module.record_cell_outcome(
        completed,
        expected_sha256=module.upgrade.canonical_sha256(completed),
        updated_at="2026-08-21T12:07:00Z",
        facts={
            "cellId": "cell_a",
            "assignmentId": "assignment_a",
            "operationId": "operation_a",
            "status": "failed",
            "beforeVaultSha256": "c" * 64,
            "afterVaultSha256": None,
            "evidenceSha256": "d" * 64,
        },
    )
    assert failed["result"] == {
        "code": "cell_rollforward_failed",
        "nextSafeAction": "hold_expand_and_recover",
    }
    assert module.upgrade.recovery_decision(failed) == "hold_expand_and_recover"
    with pytest.raises(module.OrchestrationError, match="failed"):
        module.next_rollforward_cell(failed, proof)

    post_record_failure = module.record_cell_outcome(
        completed,
        expected_sha256=module.upgrade.canonical_sha256(completed),
        updated_at="2026-08-21T12:08:00Z",
        facts={
            "cellId": "cell_b",
            "assignmentId": "assignment_b",
            "operationId": "operation_b",
            "status": "recovery_required",
            "beforeVaultSha256": "a" * 64,
            "afterVaultSha256": "a" * 64,
            "evidenceSha256": "e" * 64,
        },
    )
    assert post_record_failure["result"]["nextSafeAction"] == "hold_expand_and_recover"


def test_contract_and_promotion_gates_require_fresh_zero_legacy(monkeypatch) -> None:
    module = _module()
    pair = _pair()
    monkeypatch.setattr(module.composer, "validate_deployment_lock_pair", lambda _pair: None)
    empty = _inventory()
    inventoried = _inventoried_execution(module, empty, pair)
    proof, rollout_facts = module.begin_rollout(inventoried, empty, canary_cell_id=None)
    assert proof["mode"] == "no_op"
    assert proof["orderedCellIds"] == []
    execution = module.upgrade.advance_execution(
        inventoried,
        expected_sha256=module.upgrade.canonical_sha256(inventoried),
        next_phase="rolling",
        updated_at="2026-08-21T12:04:00Z",
        facts=rollout_facts,
    )

    proof, drained_facts = module.prove_contract_ready(execution, pair, empty)
    assert proof["legacyCellCount"] == 0
    drained = module.upgrade.advance_execution(
        execution,
        expected_sha256=module.upgrade.canonical_sha256(execution),
        next_phase="drained",
        updated_at="2026-08-21T12:05:00Z",
        facts=drained_facts,
    )
    contracted = module.upgrade.advance_execution(
        drained,
        expected_sha256=module.upgrade.canonical_sha256(drained),
        next_phase="contracted",
        updated_at="2026-08-21T12:06:00Z",
        facts={"contractEvidenceSha256": hashlib.sha256(module.canonical(proof)).hexdigest()},
    )
    preflight = module.promotion_preflight(contracted, empty)
    assert preflight["eligible"] is True

    legacy = _inventory(("cell_a", _runtime("0.54.1", "9")))
    with pytest.raises(module.OrchestrationError, match="legacy"):
        module.prove_contract_ready(execution, pair, legacy)


def test_contract_proof_refuses_a_disappeared_inventoried_tenant(monkeypatch) -> None:
    module = _module()
    pair = _pair()
    monkeypatch.setattr(module.composer, "validate_deployment_lock_pair", lambda _pair: None)
    legacy = _inventory(("cell_a", _runtime("0.54.1", "9")))
    rolling = _rolling_execution(module, legacy, pair, canary_cell_id="cell_a")
    complete = module.record_cell_outcome(
        rolling,
        expected_sha256=module.upgrade.canonical_sha256(rolling),
        updated_at="2026-08-21T12:05:00Z",
        facts={
            "cellId": "cell_a",
            "assignmentId": "assignment_a",
            "operationId": "operation_a",
            "status": "complete",
            "beforeVaultSha256": "a" * 64,
            "afterVaultSha256": "a" * 64,
            "evidenceSha256": "b" * 64,
        },
    )
    target_runtime = {
        key: value
        for key, value in _target().items()
        if key not in {"sourceCommit", "runtimeCandidateSha256"}
    }
    fresh = _inventory(("cell_a", target_runtime))
    fresh["cells"][0]["classification"] = "target"
    fresh["legacyRuntimes"] = []
    fresh["counts"]["legacyCells"] = 0
    fresh["counts"]["targetCells"] = 1

    proof, _ = module.prove_contract_ready(complete, pair, fresh)
    assert proof["cellCount"] == 1

    disappeared = _inventory()
    with pytest.raises(module.OrchestrationError, match="disappeared"):
        module.prove_contract_ready(complete, pair, disappeared)
