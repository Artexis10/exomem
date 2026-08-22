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
_BASE_RUNTIME_FIELDS = {
    "releaseVersion",
    "protocolVersion",
    "agentProfile",
    "gatewayContractDigest",
    "commandFingerprint",
    "schemaDigest",
}
_COMPATIBILITY_FIELD = "compatibilityDigest"
_DEPLOYMENT_FIELDS = _BASE_RUNTIME_FIELDS | {"runtimeImage"}


class FleetObservationError(ValueError):
    """A content-free fleet observation cannot be produced safely."""


def _error(message: str) -> NoReturn:
    raise FleetObservationError(message)


def _runtime(target: dict[str, Any], image: str) -> dict[str, str]:
    fields = frozenset(target)
    if fields not in {
        frozenset(_BASE_RUNTIME_FIELDS),
        frozenset(_BASE_RUNTIME_FIELDS | {_COMPATIBILITY_FIELD}),
    } or any(not isinstance(value, str) for value in target.values()):
        _error("deployment runtime identity is invalid")
    return {**target, "runtimeImage": image}


def _catalog(
    lock: DeploymentLock,
) -> tuple[
    dict[tuple[tuple[str, str], ...], dict[str, str]], dict[tuple[str, str], dict[str, str]]
]:
    exact: dict[tuple[tuple[str, str], ...], dict[str, str]] = {}
    legacy: dict[tuple[str, str], dict[str, str]] = {}

    def register_exact(identity: dict[str, str], deployment: dict[str, str]) -> None:
        key = tuple(sorted(identity.items()))
        existing = exact.get(key)
        if existing is not None and existing != deployment:
            _error("deployment runtime catalog is ambiguous")
        exact[key] = deployment

    selections = ["active"]
    if lock.schemaVersion == 3:
        selections.append("rollback")
    for name in selections:
        selected = lock.selected_runtime(name)  # type: ignore[arg-type]
        identity = selected.runtimeTarget.model_dump(mode="json")
        if selected.compatibilityDigest is not None:
            identity[_COMPATIBILITY_FIELD] = selected.compatibilityDigest
        deployment = _runtime(identity, selected.image)
        register_exact(identity, deployment)

    for unit in lock.composition.legacyCatalog:
        contract = unit.contract.model_dump(mode="json")
        deployment = {field: contract[field] for field in _DEPLOYMENT_FIELDS}
        key = (deployment["releaseVersion"], deployment["protocolVersion"])
        if key in legacy:
            _error("deployment legacy runtime catalog is ambiguous")
        legacy[key] = deployment
        register_exact(
            {field: deployment[field] for field in _BASE_RUNTIME_FIELDS},
            deployment,
        )
    return exact, legacy


def _resolve_runtime(
    identity: dict[str, str] | None,
    *,
    exact: dict[tuple[tuple[str, str], ...], dict[str, str]],
    legacy: dict[tuple[str, str], dict[str, str]],
) -> dict[str, str] | None:
    if identity is None:
        return None
    if frozenset(identity) in {
        frozenset(_BASE_RUNTIME_FIELDS),
        frozenset(_BASE_RUNTIME_FIELDS | {_COMPATIBILITY_FIELD}),
    }:
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
    rollforward_priors: dict[tuple[str, str], dict[str, Any]] = {}
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
            if operation.action is OperationAction.ROLLFORWARD:
                prior = desired.get(operation.cell_id)
                if prior is None:
                    _error("rollforward operation has no prior reviewed deployment identity")
                rollforward_priors[(operation.external_operation_id, operation.cell_id)] = prior
            state = "ready" if operation.state is OperationState.FINAL else operation.state.value
            desired[operation.cell_id] = {
                "cellId": operation.cell_id,
                "runtime": runtime,
                "state": state,
            }
        elif (
            operation.action is OperationAction.ROLLBACK_ROLLFORWARD
            and operation.state is OperationState.FINAL
        ):
            prior = rollforward_priors.get((operation.external_operation_id, operation.cell_id))
            if prior is None:
                _error("rollforward rollback has no prior reviewed deployment identity")
            desired[operation.cell_id] = {**prior, "state": "ready"}
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
