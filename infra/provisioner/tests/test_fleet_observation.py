from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from exomem_provisioner.fleet_observation import FleetObservationError, build_fleet_observation
from exomem_provisioner.models import OperationAction, OperationState
from exomem_provisioner.repository import FleetOperationSnapshot


def _target(release: str, *, compatibility: bool = True) -> dict[str, str]:
    target = {
        "releaseVersion": release,
        "protocolVersion": "1",
        "agentProfile": "hosted-alpha-agent-v2",
        "gatewayContractDigest": "a" * 64,
        "commandFingerprint": "b" * 64,
        "schemaDigest": "c" * 64,
    }
    if compatibility:
        target["compatibilityDigest"] = "9" * 64
    return target


def _lock():
    active = _target("0.57.2")
    legacy = {
        **_target("0.54.1", compatibility=False),
        "runtimeImage": "ghcr.io/artexis10/exomem@sha256:" + "d" * 64,
        "sourceCommit": "e" * 40,
    }
    return SimpleNamespace(
        schemaVersion=2,
        selected_runtime=lambda _selection: SimpleNamespace(
            runtimeTarget=SimpleNamespace(
                model_dump=lambda **_kwargs: {
                    key: value for key, value in active.items() if key != "compatibilityDigest"
                }
            ),
            image="ghcr.io/artexis10/exomem@sha256:" + "f" * 64,
            compatibilityDigest=active["compatibilityDigest"],
        ),
        composition=SimpleNamespace(
            legacyCatalog=(
                SimpleNamespace(contract=SimpleNamespace(model_dump=lambda **_kwargs: legacy)),
            )
        ),
    )


def _operation(
    *,
    operation_id: str,
    action: OperationAction,
    state: OperationState,
    runtime: dict[str, str] | None,
    offset: int,
) -> FleetOperationSnapshot:
    return FleetOperationSnapshot(
        external_operation_id=operation_id,
        action=action,
        state=state,
        cell_id="cell-alpha",
        runtime_identity=runtime,
        created_at=datetime(2026, 8, 21, 10, tzinfo=UTC) + timedelta(seconds=offset),
    )


def test_fleet_observation_projects_the_complete_reviewed_target_identity() -> None:
    operations = (
        _operation(
            operation_id="provision-alpha",
            action=OperationAction.PROVISION,
            state=OperationState.FINAL,
            runtime=_target("0.54.1", compatibility=False),
            offset=0,
        ),
        _operation(
            operation_id="rollforward-alpha",
            action=OperationAction.ROLLFORWARD,
            state=OperationState.PENDING,
            runtime=_target("0.57.2"),
            offset=1,
        ),
    )

    observation = build_fleet_observation(
        operations,
        lock=_lock(),
        observed_at="2026-08-21T11:00:00Z",
    )

    desired = observation["desiredCells"][0]
    assert desired["state"] == "pending"
    assert desired["runtime"]["releaseVersion"] == "0.57.2"
    assert desired["runtime"]["runtimeImage"].endswith("f" * 64)
    assert desired["runtime"]["compatibilityDigest"] == "9" * 64
    assert observation["unfinishedOperations"] == [
        {
            "operationId": "rollforward-alpha",
            "cellId": "cell-alpha",
            "kind": "rollforward",
            "status": "pending",
            "targetRuntime": desired["runtime"],
        }
    ]


def test_terminal_rollforward_rollback_restores_the_prior_desired_runtime() -> None:
    operations = (
        _operation(
            operation_id="provision-alpha",
            action=OperationAction.PROVISION,
            state=OperationState.FINAL,
            runtime=_target("0.54.1", compatibility=False),
            offset=0,
        ),
        _operation(
            operation_id="rollforward-alpha",
            action=OperationAction.ROLLFORWARD,
            state=OperationState.FINAL,
            runtime=_target("0.57.2"),
            offset=1,
        ),
        _operation(
            operation_id="rollforward-alpha",
            action=OperationAction.ROLLBACK_ROLLFORWARD,
            state=OperationState.FINAL,
            runtime=_target("0.57.2"),
            offset=2,
        ),
    )

    observation = build_fleet_observation(
        operations,
        lock=_lock(),
        observed_at="2026-08-21T11:00:00Z",
    )

    assert observation["desiredCells"][0]["runtime"]["releaseVersion"] == "0.54.1"
    assert observation["desiredCells"][0]["state"] == "ready"
    assert observation["unfinishedOperations"] == []


def test_terminal_destroy_removes_provisioner_desired_state() -> None:
    operations = (
        _operation(
            operation_id="provision-alpha",
            action=OperationAction.PROVISION,
            state=OperationState.FINAL,
            runtime={"releaseVersion": "0.54.1", "protocolVersion": "1"},
            offset=0,
        ),
        _operation(
            operation_id="destroy-alpha",
            action=OperationAction.DESTROY,
            state=OperationState.FINAL,
            runtime=None,
            offset=1,
        ),
    )

    observation = build_fleet_observation(
        operations,
        lock=_lock(),
        observed_at="2026-08-21T11:00:00Z",
    )

    assert observation["desiredCells"] == []
    assert observation["unfinishedOperations"] == []


def test_terminal_destroy_does_not_make_dead_runtime_history_a_catalog_dependency() -> None:
    operations = (
        _operation(
            operation_id="provision-alpha",
            action=OperationAction.PROVISION,
            state=OperationState.FINAL,
            runtime={"releaseVersion": "0.24.0", "protocolVersion": "1"},
            offset=0,
        ),
        _operation(
            operation_id="destroy-alpha",
            action=OperationAction.DESTROY,
            state=OperationState.FINAL,
            runtime=None,
            offset=1,
        ),
    )

    observation = build_fleet_observation(
        operations,
        lock=_lock(),
        observed_at="2026-08-21T11:00:00Z",
    )

    assert observation["desiredCells"] == []
    assert observation["unfinishedOperations"] == []


def test_live_desired_state_still_requires_a_reviewed_runtime() -> None:
    operations = (
        _operation(
            operation_id="provision-alpha",
            action=OperationAction.PROVISION,
            state=OperationState.FINAL,
            runtime={"releaseVersion": "0.24.0", "protocolVersion": "1"},
            offset=0,
        ),
    )

    with pytest.raises(
        FleetObservationError,
        match="runtime-setting operation has no reviewed deployment identity",
    ):
        build_fleet_observation(
            operations,
            lock=_lock(),
            observed_at="2026-08-21T11:00:00Z",
        )
