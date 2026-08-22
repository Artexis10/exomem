#!/usr/bin/env python3
"""Compose the safe, version-neutral phases of one Hosted runtime upgrade."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn, cast

MAX_INPUT_BYTES = 1024 * 1024
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_RUNTIME_FIELDS = {
    "releaseVersion",
    "runtimeImage",
    "protocolVersion",
    "agentProfile",
    "gatewayContractDigest",
    "commandFingerprint",
    "schemaDigest",
    "compatibilityDigest",
}
_CELL_OUTCOME_FIELDS = {
    "cellId",
    "assignmentId",
    "operationId",
    "status",
    "beforeVaultSha256",
    "afterVaultSha256",
    "evidenceSha256",
}
_TRUST_FIELDS = {
    "artifact",
    "schemaVersion",
    "consumerCommit",
    "target",
    "pinnedSites",
    "fixtureSha256s",
}


class OrchestrationError(ValueError):
    """Raised when a fleet phase cannot be proven safe."""


def _error(message: str) -> NoReturn:
    raise OrchestrationError(message)


def _load_sibling(filename: str, module_name: str) -> Any:
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        _error(f"{module_name} is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


upgrade = _load_sibling("hosted_runtime_upgrade.py", "hosted_runtime_upgrade_operator_state")
inventory = _load_sibling("hosted_fleet_inventory.py", "hosted_runtime_upgrade_inventory")
composer = _load_sibling("hosted_composition_lock.py", "hosted_runtime_upgrade_composer")


def canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _closed(value: object, fields: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        _error(f"{label} fields are incomplete or unknown")
    return cast(dict[str, Any], value)


def _sha(value: object, *, label: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _error(f"{label} must be a SHA-256 digest")
    return value


def _commit(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
        _error(f"{label} must be an exact commit")
    return value


def _runtime(value: object, *, label: str) -> dict[str, str]:
    try:
        return inventory._runtime(value, label=label)
    except inventory.InventoryError as exc:
        raise OrchestrationError(str(exc)) from None


def _target_runtime(execution: dict[str, Any]) -> dict[str, str]:
    target = upgrade.validate_execution(execution)["target"]
    return {field: cast(str, target[field]) for field in _RUNTIME_FIELDS}


def _inventory_gate(
    value: dict[str, Any], *, action: str, target: dict[str, str] | None = None
) -> dict[str, Any]:
    if value.get("artifact") != "exomem-hosted-fleet-inventory" or value.get("schemaVersion") != 1:
        _error("fleet inventory identity is invalid")
    try:
        inventory.require_inventory_gate(value, action=action)
    except inventory.InventoryError as exc:
        raise OrchestrationError(str(exc)) from None
    if target is not None and _runtime(value.get("target"), label="inventory target") != target:
        _error("inventory target differs from execution target")
    return value


def prove_substrate_trust(
    execution: dict[str, Any], trust_report: dict[str, Any]
) -> dict[str, object]:
    """Project a clean reviewed Substrate trust report into trusted-phase facts."""

    record = upgrade.validate_execution(execution)
    if record["phase"] != "selected":
        _error("Substrate trust proof requires the selected phase")
    report = _closed(trust_report, _TRUST_FIELDS, label="Substrate trust report")
    if (
        report["artifact"] != "exomem-hosted-substrate-runtime-trust"
        or report["schemaVersion"] != 1
        or report["target"] != record["target"]
    ):
        _error("Substrate trust report differs from the selected target")
    consumer = _commit(report["consumerCommit"], label="Substrate consumer commit")
    expected_sites = sorted(composer._SUBSTRATE_RUNTIME_TRUST_SITES)
    if report["pinnedSites"] != expected_sites:
        _error("Substrate trust report pinned sites are incomplete or invalid")
    fixtures = _closed(
        report["fixtureSha256s"], {"agent", "gateway"}, label="Substrate fixture digests"
    )
    _sha(fixtures["agent"], label="Substrate agent fixture digest")
    _sha(fixtures["gateway"], label="Substrate gateway fixture digest")
    return {
        "substrateConsumerCommit": consumer,
        "releaseEvidenceSha256": hashlib.sha256(canonical(report)).hexdigest(),
    }


def derive_legacy_evidence(
    reconciled_inventory: dict[str, Any], descriptor_catalog: dict[str, Any]
) -> tuple[dict[str, object], dict[str, object]]:
    """Narrow immutable legacy descriptors to the exact reconciled dependency set."""

    _inventory_gate(reconciled_inventory, action="expand")
    catalog = _closed(
        descriptor_catalog,
        {"artifact", "schemaVersion", "units"},
        label="runtime descriptor catalog",
    )
    if (
        catalog["artifact"] != "exomem-hosted-runtime-descriptor-catalog"
        or catalog["schemaVersion"] != 1
        or not isinstance(catalog["units"], list)
        or len(catalog["units"]) > 128
    ):
        _error("runtime descriptor catalog identity is invalid")
    supplied: dict[bytes, dict[str, object]] = {}
    release_units: set[tuple[str, str]] = set()
    for index, raw in enumerate(catalog["units"]):
        unit = _closed(
            raw,
            {"runtime", "sourceCommit", "contractSha256"},
            label=f"runtime descriptor {index}",
        )
        runtime = _runtime(unit["runtime"], label=f"runtime descriptor {index}")
        encoded = canonical(runtime)
        release_unit = (runtime["releaseVersion"], runtime["protocolVersion"])
        if encoded in supplied or release_unit in release_units:
            _error("runtime descriptor catalog contains duplicate release authority")
        release_units.add(release_unit)
        supplied[encoded] = {
            "runtime": runtime,
            "sourceCommit": _commit(unit["sourceCommit"], label="legacy source commit"),
            "contractSha256": _sha(unit["contractSha256"], label="legacy contract digest"),
        }

    raw_legacy = reconciled_inventory.get("legacyRuntimes")
    if not isinstance(raw_legacy, list) or len(raw_legacy) > 128:
        _error("inventory legacy runtime set is invalid")
    referenced: dict[bytes, dict[str, str]] = {}
    for index, raw in enumerate(raw_legacy):
        runtime = _runtime(raw, label=f"inventory legacy runtime {index}")
        encoded = canonical(runtime)
        if encoded in referenced:
            _error("inventory legacy runtime set contains duplicates")
        referenced[encoded] = runtime
    if set(referenced) != set(supplied):
        _error("runtime descriptors do not exactly match reconciled legacy dependencies")

    authority_units: list[dict[str, str]] = []
    catalog_units: list[dict[str, str]] = []
    for encoded in sorted(referenced, key=lambda item: (referenced[item]["releaseVersion"], item)):
        runtime = referenced[encoded]
        descriptor = supplied[encoded]
        authority_units.append(
            {
                "releaseVersion": runtime["releaseVersion"],
                "protocolVersion": runtime["protocolVersion"],
                "runtimeImage": runtime["runtimeImage"],
                "sourceCommit": cast(str, descriptor["sourceCommit"]),
            }
        )
        catalog_units.append(
            {
                **authority_units[-1],
                "contractSha256": cast(str, descriptor["contractSha256"]),
            }
        )
    return (
        {
            "artifact": "exomem-hosted-authoritative-legacy-v1-release-set",
            "schemaVersion": 1,
            "units": authority_units,
        },
        {"schemaVersion": 1, "units": catalog_units},
    )


def _validated_pair(
    pair: dict[str, Any], execution: dict[str, Any] | None = None
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    try:
        composer.validate_deployment_lock_pair(pair)
    except composer.CompositionError as exc:
        raise OrchestrationError(str(exc)) from None
    locks = pair.get("locks")
    if not isinstance(locks, list) or len(locks) != 2:
        _error("deployment lock pair is invalid")
    expand, contract = locks
    if not isinstance(expand, dict) or not isinstance(contract, dict):
        _error("deployment lock pair is invalid")
    if expand.get("admissionMode") != "expand" or contract.get("admissionMode") != "contract":
        _error("deployment lock pair phases are invalid")
    if execution is not None:
        target = upgrade.validate_execution(execution)["target"]
        expected = {
            field: target[field]
            for field in (
                "releaseVersion",
                "protocolVersion",
                "agentProfile",
                "gatewayContractDigest",
                "commandFingerprint",
                "schemaDigest",
            )
        }
        if expand.get("runtimeTarget") != expected or contract.get("runtimeTarget") != expected:
            _error("deployment lock target differs from execution target")
    return (
        cast(dict[str, Any], expand),
        cast(dict[str, Any], contract),
        {
            "pairSha256": hashlib.sha256(canonical(pair)).hexdigest(),
            "expandSha256": hashlib.sha256(canonical(expand)).hexdigest(),
            "contractSha256": hashlib.sha256(canonical(contract)).hexdigest(),
        },
    )


def _fleet_projection(value: dict[str, Any]) -> dict[str, Any]:
    projection = copy.deepcopy(value)
    projection.pop("observedAt", None)
    projection.pop("sourceSha256s", None)
    return projection


def prove_expand_adoption(
    execution: dict[str, Any],
    pair: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    exomem_commit: str,
) -> tuple[dict[str, object], dict[str, object]]:
    """Prove expand changed future admission without touching an existing cell."""

    record = upgrade.validate_execution(execution)
    if record["phase"] != "trusted":
        _error("expand adoption proof requires a trusted execution")
    target = _target_runtime(record)
    _inventory_gate(before, action="expand", target=target)
    _inventory_gate(after, action="expand", target=target)
    expand, contract, hashes = _validated_pair(pair, record)
    before_projection = _fleet_projection(before)
    after_projection = _fleet_projection(after)
    if before_projection != after_projection:
        _error("expand adoption changed the existing tenant fleet")
    projection_sha = hashlib.sha256(canonical(before_projection)).hexdigest()
    counts = before.get("counts")
    if not isinstance(counts, dict) or not isinstance(counts.get("cells"), int):
        _error("inventory counts are invalid")
    proof: dict[str, object] = {
        "artifact": "exomem-hosted-expand-adoption-proof",
        "schemaVersion": 1,
        "executionSha256": upgrade.canonical_sha256(record),
        "beforeInventorySha256": inventory.inventory_sha256(before),
        "afterInventorySha256": inventory.inventory_sha256(after),
        "fleetProjectionSha256": projection_sha,
        "pairSha256": hashes["pairSha256"],
        "expandSha256": hashes["expandSha256"],
        "contractSha256": hashes["contractSha256"],
        "cellCount": counts["cells"],
    }
    return proof, {
        "exomemCommit": _commit(exomem_commit, label="Exomem composition commit"),
        **hashes,
        "expandEvidenceSha256": hashlib.sha256(canonical(proof)).hexdigest(),
    }


def begin_rollout(
    execution: dict[str, Any],
    reconciled_inventory: dict[str, Any],
    *,
    canary_cell_id: str | None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Bind the inventoried execution to one explicit, stable rollout order."""

    record = upgrade.validate_execution(execution)
    if record["phase"] != "inventoried":
        _error("rollout plan requires the inventoried phase")
    target = _target_runtime(record)
    _inventory_gate(reconciled_inventory, action="rollforward", target=target)
    if inventory.inventory_sha256(reconciled_inventory) != record["inventory"]["sha256"]:
        _error("rollout inventory differs from the recorded inventory")
    pending = sorted(cell["cellId"] for cell in record["cells"] if cell["status"] == "pending")
    if pending:
        if canary_cell_id not in pending:
            _error("explicit canary is not a pending legacy cell")
        ordered = [canary_cell_id, *(cell_id for cell_id in pending if cell_id != canary_cell_id)]
        mode = "sequential"
    else:
        if canary_cell_id is not None:
            _error("a canary was supplied for a dependency-free fleet")
        ordered = []
        mode = "no_op"
    proof: dict[str, object] = {
        "artifact": "exomem-hosted-runtime-rollout-plan",
        "schemaVersion": 1,
        "executionSha256": upgrade.canonical_sha256(record),
        "inventorySha256": record["inventory"]["sha256"],
        "mode": mode,
        "canaryCellId": ordered[0] if ordered else None,
        "orderedCellIds": ordered,
    }
    return proof, {"rolloutEvidenceSha256": hashlib.sha256(canonical(proof)).hexdigest()}


def next_rollforward_cell(
    execution: dict[str, Any], rollout_plan: dict[str, Any]
) -> dict[str, object]:
    """Select at most one cell from the exact rollout plan bound to execution."""

    record = upgrade.validate_execution(execution)
    if record["phase"] != "rolling":
        _error("cell selection requires the rolling phase")
    if record["result"]["code"] != "in_progress":
        _error("cell rollout has failed and cannot select another cell")
    plan = _closed(
        rollout_plan,
        {
            "artifact",
            "schemaVersion",
            "executionSha256",
            "inventorySha256",
            "mode",
            "canaryCellId",
            "orderedCellIds",
        },
        label="rollout plan",
    )
    plan_sha = hashlib.sha256(canonical(plan)).hexdigest()
    if (
        plan["artifact"] != "exomem-hosted-runtime-rollout-plan"
        or plan["schemaVersion"] != 1
        or plan["inventorySha256"] != record["inventory"]["sha256"]
        or not record["evidence"]["cells"]
        or plan_sha != record["evidence"]["cells"][0]
    ):
        _error("rollout plan differs from the execution authority")
    ordered = plan["orderedCellIds"]
    if (
        not isinstance(ordered, list)
        or len(set(ordered)) != len(ordered)
        or any(
            not isinstance(cell_id, str) or not _OPAQUE_ID.fullmatch(cell_id) for cell_id in ordered
        )
    ):
        _error("rollout plan cell order is invalid")
    planned_cells = {cell["cellId"] for cell in record["cells"] if cell["status"] != "no_op"}
    if set(ordered) != planned_cells:
        _error("rollout plan cells differ from the inventoried execution")
    if ordered:
        if plan["mode"] != "sequential" or plan["canaryCellId"] != ordered[0]:
            _error("rollout plan canary is invalid")
        canary_cell_id = cast(str, ordered[0])
    else:
        if plan["mode"] != "no_op" or plan["canaryCellId"] is not None:
            _error("dependency-free rollout plan is invalid")
        canary_cell_id = None
    active = [cell for cell in record["cells"] if cell["status"] == "rolling"]
    if len(active) > 1:
        _error("more than one cell is rolling")
    if active:
        return {"action": "resume", "cellId": active[0]["cellId"]}
    cells_by_id = {cell["cellId"]: cell for cell in record["cells"]}
    pending = [
        cells_by_id[cell_id] for cell_id in ordered if cells_by_id[cell_id]["status"] == "pending"
    ]
    completed = [cell for cell in record["cells"] if cell["status"] == "complete"]
    if not pending:
        return {"action": "prove_zero_legacy", "cellId": None}
    if not completed:
        selected = next((cell for cell in pending if cell["cellId"] == canary_cell_id), None)
        if selected is None:
            _error("explicit canary is not a pending legacy cell")
        return {"action": "start_canary", "cellId": selected["cellId"]}
    if not any(cell["cellId"] == canary_cell_id for cell in completed):
        _error("canary has not completed successfully")
    return {"action": "start_next", "cellId": pending[0]["cellId"]}


def record_cell_outcome(
    execution: dict[str, Any],
    *,
    expected_sha256: str,
    updated_at: str,
    facts: dict[str, object],
) -> dict[str, Any]:
    """Record one fenced cell checkpoint without performing the lifecycle effect."""

    record = upgrade.validate_execution(execution)
    if upgrade.canonical_sha256(record) != expected_sha256:
        _error("execution changed before cell outcome recording")
    if record["phase"] != "rolling":
        _error("cell outcome requires the rolling phase")
    outcome = _closed(facts, _CELL_OUTCOME_FIELDS, label="cell outcome")
    cell_id = outcome["cellId"]
    assignment_id = outcome["assignmentId"]
    operation_id = outcome["operationId"]
    if any(
        not isinstance(value, str) or _OPAQUE_ID.fullmatch(value) is None
        for value in (cell_id, assignment_id, operation_id)
    ):
        _error("cell outcome authority is invalid")
    status = outcome["status"]
    if status not in {"rolling", "complete", "failed", "recovery_required"}:
        _error("cell outcome status is invalid")
    before = _sha(outcome["beforeVaultSha256"], label="before vault fingerprint")
    after = _sha(outcome["afterVaultSha256"], label="after vault fingerprint", nullable=True)
    evidence = _sha(outcome["evidenceSha256"], label="cell evidence", nullable=True)
    if status == "rolling" and (after is not None or evidence is not None):
        _error("rolling cell contains terminal evidence")
    if status == "complete" and (after != before or evidence is None):
        _error("complete cell lacks exact preservation evidence")
    if status in {"failed", "recovery_required"} and evidence is None:
        _error("failed cell lacks terminal evidence")

    advanced = copy.deepcopy(record)
    matches = [cell for cell in advanced["cells"] if cell["cellId"] == cell_id]
    if len(matches) != 1:
        _error("cell outcome does not name one inventoried cell")
    cell = matches[0]
    current = cell["status"]
    same_outcome = all(cell[field] == outcome[field] for field in _CELL_OUTCOME_FIELDS)
    if current == status and same_outcome:
        if record["updatedAt"] != upgrade._timestamp(updated_at, label="updatedAt"):
            _error("cell outcome retry differs from the committed facts")
        return copy.deepcopy(record)
    if record["result"]["code"] != "in_progress":
        _error("cell outcome cannot be recorded after rollout failure")
    post_record_failure = current == "complete" and status == "recovery_required"
    if current not in {"pending", "rolling"} and not post_record_failure:
        _error("cell outcome would replay a terminal cell")
    if current == "pending" and any(other["status"] == "rolling" for other in advanced["cells"]):
        _error("another cell is already rolling")
    if current == "pending" and any(
        cell[field] is not None and cell[field] != outcome[field]
        for field in ("assignmentId", "operationId")
    ):
        _error("cell outcome authority differs from the inventoried authority")
    if current == "rolling" and (
        cell["assignmentId"] != assignment_id or cell["operationId"] != operation_id
    ):
        _error("cell outcome authority changed during rollforward")
    cell.update(
        {
            "assignmentId": assignment_id,
            "operationId": operation_id,
            "status": status,
            "beforeVaultSha256": before,
            "afterVaultSha256": after,
            "evidenceSha256": evidence,
        }
    )
    if evidence is not None and evidence not in advanced["evidence"]["cells"]:
        advanced["evidence"]["cells"].append(evidence)
    if status == "complete":
        cell["class"] = "target"
        cell["releaseVersion"] = advanced["target"]["releaseVersion"]
        advanced["inventory"]["legacyCellCount"] -= 1
    if status in {"failed", "recovery_required"}:
        advanced["result"] = {
            "code": "cell_rollforward_failed",
            "nextSafeAction": "hold_expand_and_recover",
        }
    else:
        advanced["result"] = {
            "code": "in_progress",
            "nextSafeAction": (
                "prove_zero_legacy"
                if advanced["inventory"]["legacyCellCount"] == 0
                else "continue_rollforward"
            ),
        }
    advanced["updatedAt"] = upgrade._timestamp(updated_at, label="updatedAt")
    return upgrade.validate_execution(advanced)


def prove_contract_ready(
    execution: dict[str, Any], pair: dict[str, Any], fresh_inventory: dict[str, Any]
) -> tuple[dict[str, object], dict[str, object]]:
    """Bind a fresh zero-legacy inventory to the original expand/contract pair."""

    record = upgrade.validate_execution(execution)
    if record["phase"] != "rolling" or record["result"]["code"] != "in_progress":
        _error("contract proof requires an active rolling execution")
    if any(cell["status"] not in {"complete", "no_op"} for cell in record["cells"]):
        _error("contract proof requires every inventoried cell to finish")
    if record["inventory"]["legacyCellCount"] != 0:
        _error("recorded legacy dependencies block contract")
    target = _target_runtime(record)
    _inventory_gate(fresh_inventory, action="contract", target=target)
    _, _, hashes = _validated_pair(pair, record)
    if any(record["locks"][field] != value for field, value in hashes.items()):
        _error("contract lock lineage differs from the recorded expand pair")
    counts = fresh_inventory.get("counts")
    if not isinstance(counts, dict):
        _error("inventory counts are invalid")
    raw_fresh_cells = fresh_inventory.get("cells")
    if not isinstance(raw_fresh_cells, list):
        _error("inventory cells are invalid")
    fresh_cells: dict[str, dict[str, Any]] = {}
    for raw_cell in raw_fresh_cells:
        if not isinstance(raw_cell, dict) or not isinstance(raw_cell.get("cellId"), str):
            _error("inventory cells are invalid")
        cell_id = cast(str, raw_cell["cellId"])
        if cell_id in fresh_cells:
            _error("inventory cells contain duplicate authority")
        fresh_cells[cell_id] = cast(dict[str, Any], raw_cell)
    for recorded_cell in record["cells"]:
        if recorded_cell["class"] == "terminal":
            continue
        fresh_cell = fresh_cells.get(recorded_cell["cellId"])
        if fresh_cell is None:
            _error("an inventoried tenant cell disappeared before contract")
        if _runtime(fresh_cell.get("runtime"), label="fresh inventory cell runtime") != target:
            _error("an inventoried tenant cell is not on the exact target")
    proof: dict[str, object] = {
        "artifact": "exomem-hosted-zero-legacy-proof",
        "schemaVersion": 1,
        "executionSha256": upgrade.canonical_sha256(record),
        "pairSha256": hashes["pairSha256"],
        "expandSha256": hashes["expandSha256"],
        "contractSha256": hashes["contractSha256"],
        "inventorySha256": inventory.inventory_sha256(fresh_inventory),
        "cellCount": counts.get("cells"),
        "legacyCellCount": counts.get("legacyCells"),
        "unfinishedOperations": counts.get("unfinishedOperations"),
        "activeAssignments": counts.get("activeAssignments"),
    }
    proof_sha = hashlib.sha256(canonical(proof)).hexdigest()
    return proof, {
        "inventorySha256": proof["inventorySha256"],
        "inventoryEvidenceSha256": proof_sha,
        "cellCount": proof["cellCount"],
    }


def promotion_preflight(
    execution: dict[str, Any], fresh_inventory: dict[str, Any]
) -> dict[str, object]:
    """Run the free upgrade gates before any reviewer clock or authority starts."""

    record = upgrade.validate_execution(execution)
    if record["phase"] != "contracted":
        _error("promotion preflight requires contract cutover")
    _inventory_gate(fresh_inventory, action="promotion", target=_target_runtime(record))
    counts = fresh_inventory.get("counts")
    if not isinstance(counts, dict):
        _error("inventory counts are invalid")
    return {
        "artifact": "exomem-hosted-runtime-promotion-preflight",
        "schemaVersion": 1,
        "eligible": True,
        "executionSha256": upgrade.canonical_sha256(record),
        "inventorySha256": inventory.inventory_sha256(fresh_inventory),
        "cellCount": counts.get("cells"),
        "legacyCellCount": counts.get("legacyCells"),
        "unfinishedOperations": counts.get("unfinishedOperations"),
    }


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _error("operator input contains duplicate JSON keys")
        result[key] = value
    return result


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        information = path.lstat()
        if stat.S_ISLNK(information.st_mode) or not stat.S_ISREG(information.st_mode):
            _error(f"{label} must be a regular file")
        if information.st_size > MAX_INPUT_BYTES:
            _error(f"{label} is too large")
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicates)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OrchestrationError(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        _error(f"{label} must be a JSON object")
    return cast(dict[str, Any], value)


def _write_private(path: Path, value: object) -> None:
    if path.exists() or path.is_symlink():
        _error("operator evidence output already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(canonical(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_pair(first_path: Path, first: object, second_path: Path, second: object) -> None:
    if first_path == second_path:
        _error("operator evidence outputs must be distinct")
    _write_private(first_path, first)
    try:
        _write_private(second_path, second)
    except Exception:
        try:
            first_path.unlink()
        except OSError:
            pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    trust = commands.add_parser("prove-trust", help="prove reviewed Substrate target trust")
    trust.add_argument("--execution", type=Path, required=True)
    trust.add_argument("--substrate-trust", type=Path, required=True)
    trust.add_argument("--facts-output", type=Path, required=True)
    derive = commands.add_parser("derive-legacy", help="derive exact legacy composition evidence")
    derive.add_argument("--inventory", type=Path, required=True)
    derive.add_argument("--descriptors", type=Path, required=True)
    derive.add_argument("--authority-output", type=Path, required=True)
    derive.add_argument("--catalog-output", type=Path, required=True)
    adoption = commands.add_parser("prove-adoption", help="prove expand did not mutate tenants")
    adoption.add_argument("--execution", type=Path, required=True)
    adoption.add_argument("--pair", type=Path, required=True)
    adoption.add_argument("--before-inventory", type=Path, required=True)
    adoption.add_argument("--after-inventory", type=Path, required=True)
    adoption.add_argument("--exomem-commit", required=True)
    adoption.add_argument("--evidence-output", type=Path, required=True)
    adoption.add_argument("--facts-output", type=Path, required=True)
    select = commands.add_parser("next-cell", help="select the canary or next sequential cell")
    select.add_argument("--execution", type=Path, required=True)
    select.add_argument("--rollout-plan", type=Path, required=True)
    begin = commands.add_parser("begin-rollout", help="bind an explicit canary and rollout order")
    begin.add_argument("--execution", type=Path, required=True)
    begin.add_argument("--inventory", type=Path, required=True)
    begin.add_argument("--canary-cell")
    begin.add_argument("--evidence-output", type=Path, required=True)
    begin.add_argument("--facts-output", type=Path, required=True)
    outcome = commands.add_parser("record-cell", help="record one fenced cell checkpoint")
    outcome.add_argument("--execution", type=Path, required=True)
    outcome.add_argument("--expected-sha256", required=True)
    outcome.add_argument("--facts", type=Path, required=True)
    outcome.add_argument("--at", required=True)
    contract = commands.add_parser("prove-contract", help="prove fresh zero-legacy cutover")
    contract.add_argument("--execution", type=Path, required=True)
    contract.add_argument("--pair", type=Path, required=True)
    contract.add_argument("--inventory", type=Path, required=True)
    contract.add_argument("--evidence-output", type=Path, required=True)
    contract.add_argument("--facts-output", type=Path, required=True)
    promotion = commands.add_parser("promotion-preflight", help="run free upgrade promotion gates")
    promotion.add_argument("--execution", type=Path, required=True)
    promotion.add_argument("--inventory", type=Path, required=True)
    promotion.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prove-trust":
            facts = prove_substrate_trust(
                upgrade.load_execution(args.execution),
                _load_json(args.substrate_trust, label="Substrate trust report"),
            )
            _write_private(args.facts_output, facts)
            print(
                json.dumps(
                    {"substrateConsumerCommit": facts["substrateConsumerCommit"], "trusted": True},
                    separators=(",", ":"),
                )
            )
        elif args.command == "derive-legacy":
            authority, catalog = derive_legacy_evidence(
                _load_json(args.inventory, label="inventory"),
                _load_json(args.descriptors, label="runtime descriptors"),
            )
            _write_pair(args.authority_output, authority, args.catalog_output, catalog)
            print(
                json.dumps(
                    {"legacyUnits": len(cast(list[object], catalog["units"]))},
                    separators=(",", ":"),
                )
            )
        elif args.command == "prove-adoption":
            proof, facts = prove_expand_adoption(
                upgrade.load_execution(args.execution),
                _load_json(args.pair, label="deployment lock pair"),
                _load_json(args.before_inventory, label="before inventory"),
                _load_json(args.after_inventory, label="after inventory"),
                exomem_commit=args.exomem_commit,
            )
            _write_pair(args.evidence_output, proof, args.facts_output, facts)
            print(
                json.dumps(
                    {"eligible": True, "cellCount": proof["cellCount"]}, separators=(",", ":")
                )
            )
        elif args.command == "begin-rollout":
            proof, facts = begin_rollout(
                upgrade.load_execution(args.execution),
                _load_json(args.inventory, label="inventory"),
                canary_cell_id=args.canary_cell,
            )
            _write_pair(args.evidence_output, proof, args.facts_output, facts)
            print(
                json.dumps(
                    {
                        "canaryCellId": proof["canaryCellId"],
                        "mode": proof["mode"],
                        "rolloutCellCount": len(cast(list[object], proof["orderedCellIds"])),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        elif args.command == "next-cell":
            print(
                json.dumps(
                    next_rollforward_cell(
                        upgrade.load_execution(args.execution),
                        _load_json(args.rollout_plan, label="rollout plan"),
                    ),
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        elif args.command == "record-cell":
            current = upgrade.load_execution(args.execution)
            advanced = record_cell_outcome(
                current,
                expected_sha256=args.expected_sha256,
                updated_at=args.at,
                facts=_load_json(args.facts, label="cell outcome facts"),
            )
            upgrade.write_execution(args.execution, advanced, expected_sha256=args.expected_sha256)
            print(
                json.dumps(
                    {
                        "executionSha256": upgrade.canonical_sha256(advanced),
                        "nextSafeAction": advanced["result"]["nextSafeAction"],
                        "resultCode": advanced["result"]["code"],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        elif args.command == "prove-contract":
            proof, facts = prove_contract_ready(
                upgrade.load_execution(args.execution),
                _load_json(args.pair, label="deployment lock pair"),
                _load_json(args.inventory, label="inventory"),
            )
            _write_pair(args.evidence_output, proof, args.facts_output, facts)
            print(json.dumps({"eligible": True, "legacyCellCount": 0}, separators=(",", ":")))
        else:
            proof = promotion_preflight(
                upgrade.load_execution(args.execution),
                _load_json(args.inventory, label="inventory"),
            )
            _write_private(args.output, proof)
            print(json.dumps({"eligible": True}, separators=(",", ":")))
    except (OrchestrationError, upgrade.UpgradeExecutionError) as exc:
        print(f"hosted runtime upgrade orchestrator: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
