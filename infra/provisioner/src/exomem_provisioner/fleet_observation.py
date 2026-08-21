"""Read-only, content-free provisioner fleet authority observation."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import UTC, datetime
from typing import Any, NoReturn

from .config import DeploymentLock, ProvisionerSettings
from .crypto import AesGcmEnvelopeCodec
from .database import ProvisionerDatabase
from .logging import configure_content_free_logging
from .main import _require_production_database
from .models import OperationAction, OperationState
from .repository import FleetOperationSnapshot, OperationRepository

_OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_RUNTIME_FIELDS = {
    "releaseVersion",
    "protocolVersion",
    "agentProfile",
    "gatewayContractDigest",
    "commandFingerprint",
    "schemaDigest",
}
_DEPLOYMENT_FIELDS = _RUNTIME_FIELDS | {"runtimeImage"}


class FleetObservationError(ValueError):
    """A content-free fleet observation cannot be produced safely."""


def _error(message: str) -> NoReturn:
    raise FleetObservationError(message)


def _runtime(target: dict[str, Any], image: str) -> dict[str, str]:
    if set(target) != _RUNTIME_FIELDS or any(
        not isinstance(target[field], str) for field in _RUNTIME_FIELDS
    ):
        _error("deployment runtime identity is invalid")
    return {**{field: target[field] for field in _RUNTIME_FIELDS}, "runtimeImage": image}


def _catalog(lock: DeploymentLock) -> tuple[dict[tuple[tuple[str, str], ...], dict[str, str]], dict[tuple[str, str], dict[str, str]]]:
    exact: dict[tuple[tuple[str, str], ...], dict[str, str]] = {}
    legacy: dict[tuple[str, str], dict[str, str]] = {}

    selections = ["active"]
    if lock.schemaVersion == 3:
        selections.append("rollback")
    for name in selections:
        selected = lock.selected_runtime(name)  # type: ignore[arg-type]
        identity = selected.runtimeTarget.model_dump(mode="json")
        deployment = _runtime(identity, selected.image)
        exact[tuple(sorted(identity.items()))] = deployment

    for unit in lock.composition.legacyCatalog:
        contract = unit.contract.model_dump(mode="json")
        deployment = {
            field: contract[field]
            for field in _DEPLOYMENT_FIELDS
        }
        key = (deployment["releaseVersion"], deployment["protocolVersion"])
        if key in legacy:
            _error("deployment legacy runtime catalog is ambiguous")
        legacy[key] = deployment
    return exact, legacy


def _resolve_runtime(
    identity: dict[str, str] | None,
    *,
    exact: dict[tuple[tuple[str, str], ...], dict[str, str]],
    legacy: dict[tuple[str, str], dict[str, str]],
) -> dict[str, str] | None:
    if identity is None:
        return None
    if set(identity) == _RUNTIME_FIELDS:
        return exact.get(tuple(sorted(identity.items())))
    if set(identity) == {"releaseVersion", "protocolVersion"}:
        return legacy.get((identity["releaseVersion"], identity["protocolVersion"]))
    _error("operation runtime identity is invalid")


def build_fleet_observation(
    operations: tuple[FleetOperationSnapshot, ...],
    *,
    lock: DeploymentLock,
    observed_at: str,
) -> dict[str, Any]:
    """Project encrypted operation history into one bounded redacted document."""

    try:
        parsed = datetime.strptime(observed_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise FleetObservationError("fleet observation timestamp is invalid") from error
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != observed_at:
        _error("fleet observation timestamp is invalid")
    if len(operations) > 100_000:
        _error("fleet operation history exceeds its observation bound")

    exact, legacy = _catalog(lock)
    desired: dict[str, dict[str, Any]] = {}
    resolved: dict[int, dict[str, str] | None] = {}
    ordered = sorted(operations, key=lambda item: (item.created_at, item.external_operation_id))
    for index, operation in enumerate(ordered):
        if not _OPAQUE.fullmatch(operation.cell_id):
            _error("fleet cell identity is invalid")
        runtime = _resolve_runtime(operation.runtime_identity, exact=exact, legacy=legacy)
        resolved[index] = runtime
        if operation.action in {OperationAction.PROVISION, OperationAction.ROLLFORWARD}:
            if runtime is None:
                _error("runtime-setting operation has no reviewed deployment identity")
            state = (
                "ready"
                if operation.state is OperationState.FINAL
                else operation.state.value
            )
            desired[operation.cell_id] = {
                "cellId": operation.cell_id,
                "runtime": runtime,
                "state": state,
            }
        elif (
            operation.action in {OperationAction.DESTROY, OperationAction.DISCARD}
            and operation.state is OperationState.FINAL
        ):
            desired.pop(operation.cell_id, None)

    unfinished: list[dict[str, Any]] = []
    for index, operation in enumerate(ordered):
        if operation.state not in {OperationState.PENDING, OperationState.CLAIMED}:
            continue
        if not _OPAQUE.fullmatch(operation.external_operation_id):
            _error("fleet operation identity is invalid")
        runtime = resolved[index]
        if runtime is None:
            current = desired.get(operation.cell_id)
            runtime = current["runtime"] if current is not None else None
        if runtime is None:
            _error("unfinished operation has no reviewed deployment identity")
        unfinished.append(
            {
                "operationId": operation.external_operation_id,
                "cellId": operation.cell_id,
                "kind": operation.action.value.replace("-", "_"),
                "status": operation.state.value,
                "targetRuntime": runtime,
            }
        )

    return {
        "artifact": "exomem-hosted-provisioner-fleet-observation",
        "schemaVersion": 1,
        "observedAt": observed_at,
        "desiredCells": sorted(desired.values(), key=lambda item: item["cellId"]),
        "unfinishedOperations": sorted(
            unfinished,
            key=lambda item: (item["cellId"], item["operationId"]),
        ),
    }


async def _observe() -> dict[str, Any]:
    settings = ProvisionerSettings()  # type: ignore[call-arg]
    _require_production_database(settings)
    lock = settings.deployment_lock
    if lock is None:
        _error("selected deployment lock is unavailable")
    database = ProvisionerDatabase(settings)
    repository = OperationRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
        claim_seconds=settings.claim_seconds,
        max_failure_attempts=settings.max_failure_attempts,
    )
    try:
        if not await database.ready():
            _error("provisioner database is unavailable")
        return build_fleet_observation(
            await repository.list_fleet_operation_observations(),
            lock=lock,
            observed_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    finally:
        await database.dispose()


def run_fleet_observe() -> None:
    arguments = sys.argv[1:]
    if arguments in (["-h"], ["--help"]):
        print("usage: exomem-provisioner-fleet-observe")
        return
    if arguments:
        raise SystemExit(2)
    configure_content_free_logging()
    try:
        observation = asyncio.run(_observe())
    except Exception:  # noqa: BLE001 - kubectl caller receives one redacted failure
        raise SystemExit(1) from None
    print(json.dumps(observation, sort_keys=True, separators=(",", ":")))
