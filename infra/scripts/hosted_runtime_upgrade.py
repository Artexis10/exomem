#!/usr/bin/env python3
"""Drive one governed, restartable Exomem Hosted runtime upgrade."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, cast

MAX_EXECUTION_BYTES = 1024 * 1024
MAX_INPUT_BYTES = 128 * 1024
_EXECUTION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_RELEASE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE = re.compile(r"^ghcr\.io/artexis10/exomem@sha256:[0-9a-f]{64}$")
_PROTOCOL = re.compile(r"^[1-9][0-9]{0,7}$")
_PROFILE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_RESULT_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PHASES = (
    "selected",
    "trusted",
    "expanded",
    "inventoried",
    "rolling",
    "drained",
    "contracted",
    "promoted",
    "accepted",
    "complete",
)
_TARGET_FIELDS = {
    "releaseVersion",
    "sourceCommit",
    "runtimeImage",
    "runtimeCandidateSha256",
    "protocolVersion",
    "agentProfile",
    "gatewayContractDigest",
    "commandFingerprint",
    "schemaDigest",
    "compatibilityDigest",
}
_EXECUTION_FIELDS = {
    "artifact",
    "schemaVersion",
    "executionId",
    "phase",
    "target",
    "repositories",
    "locks",
    "inventory",
    "cells",
    "evidence",
    "result",
    "createdAt",
    "updatedAt",
}
_REPOSITORY_FIELDS = {"substrateConsumerCommit", "exomemCommit"}
_LOCK_FIELDS = {"pairSha256", "expandSha256", "contractSha256"}
_INVENTORY_FIELDS = {"status", "sha256", "cellCount", "legacyCellCount"}
_CELL_FIELDS = {
    "cellId",
    "class",
    "releaseVersion",
    "assignmentId",
    "operationId",
    "status",
    "beforeVaultSha256",
    "afterVaultSha256",
    "evidenceSha256",
}
_EVIDENCE_FIELDS = {"release", "inventory", "cells", "promotion", "acceptance"}
_RESULT_FIELDS = {"code", "nextSafeAction"}
_NEXT_PHASE = {
    "selected": "trusted",
    "trusted": "expanded",
    "expanded": "inventoried",
    "inventoried": "rolling",
    "rolling": "drained",
    "drained": "contracted",
    "contracted": "promoted",
    "promoted": "accepted",
    "accepted": "complete",
}
_NEXT_ACTION = {
    "trusted": "deploy_expand",
    "expanded": "inventory_fleet",
    "inventoried": "begin_rollforward",
    "rolling": "continue_rollforward",
    "drained": "deploy_contract",
    "contracted": "run_promotion",
    "promoted": "run_acceptance",
    "accepted": "finalize",
    "complete": "none",
}
_EXPECTED_ACTION = {"selected": "trust_target", **_NEXT_ACTION}


class UpgradeExecutionError(ValueError):
    """Raised when an upgrade execution cannot be represented safely."""


def _error(message: str) -> NoReturn:
    raise UpgradeExecutionError(message)


def _timestamp(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value) is None
    ):
        _error(f"{label} must be canonical RFC3339 UTC")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise UpgradeExecutionError(f"{label} must be canonical RFC3339 UTC") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        _error(f"{label} must be canonical RFC3339 UTC")
    return value


def _target(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _TARGET_FIELDS:
        _error("target fields are incomplete or unknown")
    target = cast(dict[str, object], value)
    if not isinstance(target["releaseVersion"], str) or not _RELEASE.fullmatch(
        target["releaseVersion"]
    ):
        _error("target release is invalid")
    if not isinstance(target["sourceCommit"], str) or not _COMMIT.fullmatch(target["sourceCommit"]):
        _error("target source commit is invalid")
    if not isinstance(target["runtimeImage"], str) or not _IMAGE.fullmatch(target["runtimeImage"]):
        _error("target runtime image is invalid")
    if not isinstance(target["protocolVersion"], str) or not _PROTOCOL.fullmatch(
        target["protocolVersion"]
    ):
        _error("target protocol is invalid")
    if not isinstance(target["agentProfile"], str) or not _PROFILE.fullmatch(
        target["agentProfile"]
    ):
        _error("target profile is invalid")
    for field in (
        "runtimeCandidateSha256",
        "gatewayContractDigest",
        "commandFingerprint",
        "schemaDigest",
        "compatibilityDigest",
    ):
        digest = target[field]
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            _error(f"target {field} is invalid")
    return dict(target)


def new_execution(
    *, execution_id: str, target: dict[str, object], created_at: str
) -> dict[str, Any]:
    """Create the immutable authority skeleton for one selected target."""

    if not _EXECUTION_ID.fullmatch(execution_id):
        _error("execution ID is invalid")
    timestamp = _timestamp(created_at, label="createdAt")
    record = {
        "artifact": "exomem-hosted-runtime-upgrade-execution",
        "schemaVersion": 1,
        "executionId": execution_id,
        "phase": "selected",
        "target": _target(target),
        "repositories": {"substrateConsumerCommit": None, "exomemCommit": None},
        "locks": {"pairSha256": None, "expandSha256": None, "contractSha256": None},
        "inventory": {
            "status": "pending",
            "sha256": None,
            "cellCount": None,
            "legacyCellCount": None,
        },
        "cells": [],
        "evidence": {
            "release": [],
            "inventory": [],
            "cells": [],
            "promotion": [],
            "acceptance": [],
        },
        "result": {"code": "in_progress", "nextSafeAction": "trust_target"},
        "createdAt": timestamp,
        "updatedAt": timestamp,
    }
    return validate_execution(record)


def _closed_dict(value: object, fields: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        _error(f"{label} fields are incomplete or unknown")
    return cast(dict[str, Any], value)


def _nullable_pattern(value: object, pattern: re.Pattern[str], *, label: str) -> None:
    if value is not None and (not isinstance(value, str) or not pattern.fullmatch(value)):
        _error(f"{label} is invalid")


def _non_negative_count(value: object, *, label: str, nullable: bool) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _error(f"{label} must be a non-negative integer")


def _digest_list(value: object, *, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 4096:
        _error(f"{label} evidence must be a bounded list")
    if any(not isinstance(item, str) or not _SHA256.fullmatch(item) for item in value):
        _error(f"{label} evidence contains an invalid digest")
    if len(set(value)) != len(value):
        _error(f"{label} evidence contains a duplicate digest")
    return cast(list[str], value)


def _validate_cell(value: object) -> dict[str, Any]:
    cell = _closed_dict(value, _CELL_FIELDS, label="cell")
    if not isinstance(cell["cellId"], str) or not _OPAQUE_ID.fullmatch(cell["cellId"]):
        _error("cellId is invalid")
    if cell["class"] not in {"target", "legacy", "reviewer", "terminal", "inconsistent"}:
        _error("cell class is invalid")
    if not isinstance(cell["releaseVersion"], str) or not _RELEASE.fullmatch(
        cell["releaseVersion"]
    ):
        _error("cell releaseVersion is invalid")
    for field in ("assignmentId", "operationId"):
        _nullable_pattern(cell[field], _OPAQUE_ID, label=f"cell {field}")
    if cell["status"] not in {
        "pending",
        "rolling",
        "complete",
        "failed",
        "recovery_required",
        "no_op",
    }:
        _error("cell status is invalid")
    for field in ("beforeVaultSha256", "afterVaultSha256", "evidenceSha256"):
        _nullable_pattern(cell[field], _SHA256, label=f"cell {field}")
    if cell["status"] == "complete" and (
        cell["beforeVaultSha256"] is None
        or cell["afterVaultSha256"] is None
        or cell["evidenceSha256"] is None
        or cell["beforeVaultSha256"] != cell["afterVaultSha256"]
    ):
        _error("complete cell lacks preservation evidence")
    return cell


def validate_execution(value: object) -> dict[str, Any]:
    """Validate the closed authority boundary before reading or writing a record."""

    record = _closed_dict(value, _EXECUTION_FIELDS, label="execution")
    if record["artifact"] != "exomem-hosted-runtime-upgrade-execution":
        _error("execution artifact is invalid")
    if record["schemaVersion"] != 1 or isinstance(record["schemaVersion"], bool):
        _error("execution schemaVersion is invalid")
    if not isinstance(record["executionId"], str) or not _EXECUTION_ID.fullmatch(
        record["executionId"]
    ):
        _error("execution ID is invalid")
    phase = record["phase"]
    if phase not in _PHASES:
        _error("execution phase is invalid")
    _target(record.get("target"))

    repositories = _closed_dict(record["repositories"], _REPOSITORY_FIELDS, label="repository")
    for field in _REPOSITORY_FIELDS:
        _nullable_pattern(repositories[field], _COMMIT, label=f"repository {field}")

    locks = _closed_dict(record["locks"], _LOCK_FIELDS, label="lock")
    for field in _LOCK_FIELDS:
        _nullable_pattern(locks[field], _SHA256, label=f"lock {field}")

    inventory = _closed_dict(record["inventory"], _INVENTORY_FIELDS, label="inventory")
    if inventory["status"] not in {"pending", "consistent", "inconsistent", "empty"}:
        _error("inventory status is invalid")
    _nullable_pattern(inventory["sha256"], _SHA256, label="inventory sha256")
    _non_negative_count(inventory["cellCount"], label="cellCount", nullable=True)
    _non_negative_count(inventory["legacyCellCount"], label="legacyCellCount", nullable=True)
    if inventory["status"] == "pending":
        if any(
            inventory[field] is not None for field in ("sha256", "cellCount", "legacyCellCount")
        ):
            _error("pending inventory must not contain observations")
    else:
        if any(inventory[field] is None for field in ("sha256", "cellCount", "legacyCellCount")):
            _error("observed inventory is incomplete")
        if cast(int, inventory["legacyCellCount"]) > cast(int, inventory["cellCount"]):
            _error("legacyCellCount exceeds cellCount")
        empty = inventory["cellCount"] == 0 and inventory["legacyCellCount"] == 0
        if (inventory["status"] == "empty") != empty:
            _error("empty inventory counts are inconsistent")

    cells_value = record["cells"]
    if not isinstance(cells_value, list) or len(cells_value) > 1024:
        _error("execution cells must be a bounded list")
    cells = [_validate_cell(cell) for cell in cells_value]
    cell_ids = [cast(str, cell["cellId"]) for cell in cells]
    if len(set(cell_ids)) != len(cell_ids):
        _error("execution contains duplicate cellId")
    if inventory["cellCount"] is not None and len(cells) != inventory["cellCount"]:
        _error("execution cells do not match cellCount")

    evidence = _closed_dict(record["evidence"], _EVIDENCE_FIELDS, label="evidence")
    digests = {field: _digest_list(evidence[field], label=field) for field in _EVIDENCE_FIELDS}

    result = _closed_dict(record["result"], _RESULT_FIELDS, label="result")
    if not isinstance(result["code"], str) or not _RESULT_CODE.fullmatch(result["code"]):
        _error("result code is invalid")
    failed_rollout = phase == "rolling" and any(
        cell["status"] in {"failed", "recovery_required"} for cell in cells
    )
    if failed_rollout:
        expected_result = {
            "code": "cell_rollforward_failed",
            "nextSafeAction": "hold_expand_and_recover",
        }
    else:
        expected_action = _EXPECTED_ACTION[phase]
        if phase == "rolling" and inventory["legacyCellCount"] == 0:
            expected_action = "prove_zero_legacy"
        expected_result = {
            "code": "complete" if phase == "complete" else "in_progress",
            "nextSafeAction": expected_action,
        }
    if result != expected_result:
        _error("result does not match execution phase")

    created = _timestamp(record["createdAt"], label="createdAt")
    updated = _timestamp(record["updatedAt"], label="updatedAt")
    if updated < created:
        _error("updatedAt is earlier than createdAt")

    rank = _PHASES.index(phase)
    if rank >= _PHASES.index("trusted"):
        if repositories["substrateConsumerCommit"] is None or not digests["release"]:
            _error(f"{phase} phase lacks trusted release authority")
    elif any(repositories[field] is not None for field in _REPOSITORY_FIELDS):
        _error("selected phase contains repository authority")
    if rank >= _PHASES.index("expanded"):
        if repositories["exomemCommit"] is None or any(
            locks[field] is None for field in _LOCK_FIELDS
        ):
            _error(f"{phase} phase lacks expanded lock authority")
    elif any(locks[field] is not None for field in _LOCK_FIELDS):
        _error(f"{phase} phase contains premature lock authority")
    if rank >= _PHASES.index("inventoried"):
        if inventory["status"] not in {"consistent", "empty"} or not digests["inventory"]:
            _error(f"{phase} phase lacks reconciled inventory authority")
    elif inventory["status"] != "pending" or cells:
        _error(f"{phase} phase contains premature inventory authority")
    if rank >= _PHASES.index("rolling") and not digests["cells"]:
        _error(f"{phase} phase lacks rollout evidence")
    if rank >= _PHASES.index("drained") and inventory["legacyCellCount"] != 0:
        _error(f"{phase} phase has legacy dependencies")
    if rank >= _PHASES.index("promoted") and not digests["promotion"]:
        _error(f"{phase} phase lacks promotion evidence")
    if rank >= _PHASES.index("accepted") and not digests["acceptance"]:
        _error(f"{phase} phase lacks acceptance evidence")
    return record


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


def canonical_sha256(record: dict[str, Any]) -> str:
    """Hash the exact canonical execution bytes used for optimistic fencing."""

    return hashlib.sha256(_canonical(record)).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _error(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_regular(path: Path, *, label: str, maximum: int) -> bytes:
    try:
        information = path.lstat()
    except OSError as exc:
        raise UpgradeExecutionError(f"cannot inspect {label}: {exc}") from exc
    if stat.S_ISLNK(information.st_mode):
        _error(f"{label} must not be a symlink")
    if not stat.S_ISREG(information.st_mode):
        _error(f"{label} must be a regular file")
    if information.st_size > maximum:
        _error(f"{label} exceeds maximum size of {maximum} bytes")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise UpgradeExecutionError(f"cannot read {label}: {exc}") from exc
    if len(data) > maximum:
        _error(f"{label} exceeds maximum size of {maximum} bytes")
    return data


def _json_object(path: Path, *, label: str, maximum: int) -> dict[str, Any]:
    raw = _read_regular(path, label=label, maximum=maximum)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpgradeExecutionError(f"invalid {label} JSON: {exc}") from exc
    if not isinstance(value, dict):
        _error(f"{label} must be a JSON object")
    return cast(dict[str, Any], value)


def load_execution(path: Path) -> dict[str, Any]:
    """Load one exact canonical, schema-valid execution record."""

    raw = _read_regular(path, label="execution", maximum=MAX_EXECUTION_BYTES)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpgradeExecutionError(f"invalid execution JSON: {exc}") from exc
    record = validate_execution(value)
    if raw != _canonical(record):
        _error("execution bytes are not canonical")
    return record


def _write_atomic(path: Path, data: bytes) -> None:
    if path.exists() or path.is_symlink():
        try:
            existing = path.lstat()
        except OSError as exc:
            raise UpgradeExecutionError(f"cannot inspect execution output: {exc}") from exc
        if stat.S_ISLNK(existing.st_mode):
            _error("execution output must not be a symlink")
        if not stat.S_ISREG(existing.st_mode):
            _error("execution output must be a regular file")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_execution(
    path: Path,
    record: dict[str, Any],
    *,
    expected_sha256: str | None = None,
) -> str:
    """Atomically persist a validated record behind an optimistic hash fence."""

    validated = validate_execution(record)
    if path.exists() or path.is_symlink():
        if expected_sha256 is None:
            _error("execution output exists; an expected SHA-256 fence is required")
        existing = load_execution(path)
        if canonical_sha256(existing) != expected_sha256:
            _error("execution write fence does not match current bytes")
    elif expected_sha256 is not None:
        _error("execution write fence was supplied for a missing record")
    data = _canonical(validated)
    _write_atomic(path, data)
    return hashlib.sha256(data).hexdigest()


def recovery_decision(record: dict[str, Any]) -> str:
    """Return the tenant-safe recovery action for the last observed phase."""

    phase = record.get("phase")
    cells = record.get("cells")
    if not isinstance(cells, list):
        _error("execution cells are invalid")

    target_observed = any(
        isinstance(cell, dict)
        and (
            cell.get("status") == "complete"
            or cell.get("afterVaultSha256") is not None
            or cell.get("evidenceSha256") is not None
        )
        for cell in cells
    )
    if target_observed or phase in {
        "drained",
        "contracted",
        "promoted",
        "accepted",
        "complete",
    }:
        return "hold_expand_and_recover"
    if phase == "rolling" and any(
        isinstance(cell, dict) and cell.get("status") == "rolling" for cell in cells
    ):
        return "retry_cell_atomically"
    if phase in {"expanded", "inventoried", "rolling"}:
        return "restore_prior_platform_lock"
    if phase in {"selected", "trusted"}:
        return "discard_target_artifacts"
    _error("execution phase is invalid")


def _exact_facts(facts: dict[str, object], fields: set[str], *, phase: str) -> None:
    if set(facts) != fields:
        _error(f"{phase} phase facts are incomplete or unknown")


def _sha_fact(facts: dict[str, object], field: str) -> str:
    value = facts[field]
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        _error(f"{field} must be a SHA-256 digest")
    return value


def _commit_fact(facts: dict[str, object], field: str) -> str:
    value = facts[field]
    if not isinstance(value, str) or not _COMMIT.fullmatch(value):
        _error(f"{field} must be an exact commit")
    return value


def _count_fact(facts: dict[str, object], field: str) -> int:
    value = facts[field]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _error(f"{field} must be a non-negative integer")
    return value


def _append_evidence(record: dict[str, Any], kind: str, digest: str) -> None:
    values = record["evidence"][kind]
    if digest not in values:
        values.append(digest)


def _apply_trusted(record: dict[str, Any], facts: dict[str, object]) -> None:
    _exact_facts(
        facts,
        {"substrateConsumerCommit", "releaseEvidenceSha256"},
        phase="trusted",
    )
    record["repositories"]["substrateConsumerCommit"] = _commit_fact(
        facts, "substrateConsumerCommit"
    )
    _append_evidence(record, "release", _sha_fact(facts, "releaseEvidenceSha256"))


def _apply_expanded(record: dict[str, Any], facts: dict[str, object]) -> None:
    _exact_facts(
        facts,
        {
            "exomemCommit",
            "pairSha256",
            "expandSha256",
            "contractSha256",
            "expandEvidenceSha256",
        },
        phase="expanded",
    )
    record["repositories"]["exomemCommit"] = _commit_fact(facts, "exomemCommit")
    for field in ("pairSha256", "expandSha256", "contractSha256"):
        record["locks"][field] = _sha_fact(facts, field)
    _append_evidence(record, "release", _sha_fact(facts, "expandEvidenceSha256"))


def _apply_inventoried(record: dict[str, Any], facts: dict[str, object]) -> None:
    _exact_facts(
        facts,
        {
            "inventoryStatus",
            "inventorySha256",
            "cellCount",
            "legacyCellCount",
            "inventoryEvidenceSha256",
            "cells",
        },
        phase="inventoried",
    )
    status = facts["inventoryStatus"]
    if status not in {"empty", "consistent"}:
        _error("inventoryStatus must be empty or consistent")
    cell_count = _count_fact(facts, "cellCount")
    legacy_count = _count_fact(facts, "legacyCellCount")
    cells = facts["cells"]
    if not isinstance(cells, list) or len(cells) != cell_count:
        _error("inventory cells do not match cellCount")
    if (status == "empty") is not (cell_count == 0 and legacy_count == 0):
        _error("empty inventory counts are inconsistent")
    record["inventory"] = {
        "status": status,
        "sha256": _sha_fact(facts, "inventorySha256"),
        "cellCount": cell_count,
        "legacyCellCount": legacy_count,
    }
    record["cells"] = copy.deepcopy(cells)
    _append_evidence(record, "inventory", _sha_fact(facts, "inventoryEvidenceSha256"))


def _apply_rolling(record: dict[str, Any], facts: dict[str, object]) -> None:
    _exact_facts(facts, {"rolloutEvidenceSha256"}, phase="rolling")
    _append_evidence(record, "cells", _sha_fact(facts, "rolloutEvidenceSha256"))


def _apply_drained(record: dict[str, Any], facts: dict[str, object]) -> None:
    _exact_facts(
        facts,
        {"inventorySha256", "inventoryEvidenceSha256", "cellCount"},
        phase="drained",
    )
    cell_count = _count_fact(facts, "cellCount")
    record["inventory"] = {
        "status": "empty" if cell_count == 0 else "consistent",
        "sha256": _sha_fact(facts, "inventorySha256"),
        "cellCount": cell_count,
        "legacyCellCount": 0,
    }
    _append_evidence(record, "inventory", _sha_fact(facts, "inventoryEvidenceSha256"))


def _apply_single_evidence(
    record: dict[str, Any], facts: dict[str, object], *, phase: str, field: str, kind: str
) -> None:
    _exact_facts(facts, {field}, phase=phase)
    _append_evidence(record, kind, _sha_fact(facts, field))


def _apply_phase(record: dict[str, Any], phase: str, facts: dict[str, object]) -> None:
    if phase == "trusted":
        _apply_trusted(record, facts)
    elif phase == "expanded":
        _apply_expanded(record, facts)
    elif phase == "inventoried":
        _apply_inventoried(record, facts)
    elif phase == "rolling":
        _apply_rolling(record, facts)
    elif phase == "drained":
        _apply_drained(record, facts)
    elif phase == "contracted":
        _apply_single_evidence(
            record,
            facts,
            phase=phase,
            field="contractEvidenceSha256",
            kind="release",
        )
    elif phase == "promoted":
        _apply_single_evidence(
            record,
            facts,
            phase=phase,
            field="promotionEvidenceSha256",
            kind="promotion",
        )
    elif phase == "accepted":
        _apply_single_evidence(
            record,
            facts,
            phase=phase,
            field="acceptanceEvidenceSha256",
            kind="acceptance",
        )
    elif phase == "complete":
        _apply_single_evidence(
            record,
            facts,
            phase=phase,
            field="finalEvidenceSha256",
            kind="acceptance",
        )
    else:
        _error("execution phase is invalid")


def advance_execution(
    record: dict[str, Any],
    *,
    expected_sha256: str,
    next_phase: str,
    updated_at: str,
    facts: dict[str, object],
) -> dict[str, Any]:
    """Advance one reviewed phase without performing an external effect."""

    if not _SHA256.fullmatch(expected_sha256):
        _error("execution fence is invalid")
    if canonical_sha256(record) != expected_sha256:
        _error("execution changed before phase advancement")
    validate_execution(record)
    if record.get("phase") == next_phase:
        probe = copy.deepcopy(record)
        _apply_phase(probe, next_phase, facts)
        if record != probe or record.get("updatedAt") != _timestamp(updated_at, label="updatedAt"):
            _error(f"{next_phase} phase retry differs from the committed facts")
        return copy.deepcopy(record)
    current_phase = record.get("phase")
    if not isinstance(current_phase, str) or _NEXT_PHASE.get(current_phase) != next_phase:
        _error("execution phase transition is invalid")
    advanced = copy.deepcopy(record)
    _apply_phase(advanced, next_phase, facts)
    advanced["phase"] = next_phase
    next_action = _NEXT_ACTION[next_phase]
    if next_phase == "rolling" and advanced["inventory"]["legacyCellCount"] == 0:
        next_action = "prove_zero_legacy"
    advanced["result"] = {
        "code": "complete" if next_phase == "complete" else "in_progress",
        "nextSafeAction": next_action,
    }
    advanced["updatedAt"] = _timestamp(updated_at, label="updatedAt")
    return validate_execution(advanced)


def _summary(record: dict[str, Any]) -> dict[str, object]:
    return {
        "executionId": record["executionId"],
        "releaseVersion": record["target"]["releaseVersion"],
        "phase": record["phase"],
        "executionSha256": canonical_sha256(record),
        "nextSafeAction": record["result"]["nextSafeAction"],
        "recoveryAction": recovery_decision(record),
    }


def _print_json(value: object) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="create a selected execution")
    create.add_argument("--execution", type=Path, required=True)
    create.add_argument("--execution-id", required=True)
    create.add_argument("--target", type=Path, required=True)
    create.add_argument("--at", required=True)

    for name, help_text in (
        ("inspect", "inspect a validated execution"),
        ("validate", "revalidate canonical execution bytes"),
        ("recover", "show the tenant-safe recovery action"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--execution", type=Path, required=True)

    advance = commands.add_parser("advance", help="advance one reviewed phase")
    advance.add_argument("--execution", type=Path, required=True)
    advance.add_argument("--expected-sha256", required=True)
    advance.add_argument("--phase", choices=_PHASES[1:], required=True)
    advance.add_argument("--facts", type=Path, required=True)
    advance.add_argument("--at", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            target = _json_object(args.target, label="target", maximum=MAX_INPUT_BYTES)
            record = new_execution(
                execution_id=args.execution_id,
                target=target,
                created_at=args.at,
            )
            write_execution(args.execution, record)
            _print_json(_summary(record))
        elif args.command == "advance":
            current = load_execution(args.execution)
            facts = _json_object(args.facts, label="phase facts", maximum=MAX_INPUT_BYTES)
            advanced = advance_execution(
                current,
                expected_sha256=args.expected_sha256,
                next_phase=args.phase,
                updated_at=args.at,
                facts=facts,
            )
            write_execution(
                args.execution,
                advanced,
                expected_sha256=args.expected_sha256,
            )
            _print_json(_summary(advanced))
        else:
            record = load_execution(args.execution)
            if args.command == "validate":
                _print_json({"executionSha256": canonical_sha256(record), "valid": True})
            elif args.command == "recover":
                _print_json(
                    {
                        "executionSha256": canonical_sha256(record),
                        "recoveryAction": recovery_decision(record),
                    }
                )
            else:
                _print_json(_summary(record))
    except UpgradeExecutionError as exc:
        print(f"hosted runtime upgrade: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
