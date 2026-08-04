from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from exomem_provisioner.driver import EffectContext, FakeDriver
from exomem_provisioner.schemas import (
    REQUEST_MODELS,
    FailureResponse,
    PendingResponse,
    ProvisionRequest,
    request_plaintext,
)
from exomem_provisioner.wire_protocol import (
    FINAL_MODELS_BY_PROTOCOL,
    REQUEST_MODELS_BY_PROTOCOL,
    WIRE_PROTOCOL_V1,
    WIRE_PROTOCOL_V2,
    runtime_identity,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_ACTIVE_CREDENTIAL = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
_NEXT_CREDENTIAL = "ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8"


def _runtime_target() -> dict[str, str]:
    return {
        "releaseVersion": "0.35.1",
        "protocolVersion": "1",
        "agentProfile": "hosted-alpha-agent-v1",
        "gatewayContractDigest": "a" * 64,
        "commandFingerprint": "b" * 64,
        "schemaDigest": "c" * 64,
    }


def test_v2_cell_requests_require_only_a_strict_six_field_runtime_target() -> None:
    body = {
        "operationId": "operation-v2-alpha",
        "checkpoint": "requested",
        "fenceGeneration": 1,
        "tenantId": "tenant-v2-alpha",
        "cellId": "cell-v2-alpha",
        "provisionMode": "serve",
        "serviceCredential": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
        "workerPolicy": {"workerCount": 2, "semantic": True, "media": False},
        "runtimeTarget": _runtime_target(),
    }

    request = REQUEST_MODELS_BY_PROTOCOL[WIRE_PROTOCOL_V2]["provision"].model_validate(body)

    assert runtime_identity(request.model_dump(mode="python")) == _runtime_target()
    with pytest.raises(ValidationError):
        REQUEST_MODELS_BY_PROTOCOL[WIRE_PROTOCOL_V2]["provision"].model_validate(
            {**body, "releaseVersion": "0.35.1"}
        )
    with pytest.raises(ValidationError):
        REQUEST_MODELS_BY_PROTOCOL[WIRE_PROTOCOL_V2]["provision"].model_validate(
            {
                **body,
                "runtimeTarget": {**_runtime_target(), "candidateId": "candidate-secret"},
            }
        )
    with pytest.raises(ValidationError):
        REQUEST_MODELS_BY_PROTOCOL[WIRE_PROTOCOL_V2]["provision"].model_validate(
            {
                **body,
                "runtimeTarget": {**_runtime_target(), "schemaDigest": "C" * 64},
            }
        )


def test_v2_has_one_closed_model_per_action_and_explicit_target_free_actions() -> None:
    request_models = REQUEST_MODELS_BY_PROTOCOL[WIRE_PROTOCOL_V2]

    assert len(request_models) == 14
    assert len(set(request_models.values())) == 14
    assert set(request_models["export-delete"].model_fields) == {
        "operationId",
        "checkpoint",
        "fenceGeneration",
        "tenantId",
        "exportRef",
    }
    assert set(request_models["export-download"].model_fields) == {
        "operationId",
        "checkpoint",
        "fenceGeneration",
        "tenantId",
        "exportRef",
    }
    assert set(request_models["destroy"].model_fields) == {
        "operationId",
        "checkpoint",
        "fenceGeneration",
        "tenantId",
    }


def test_header_selected_v1_maps_are_immutable_snapshots_of_legacy_maps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = REQUEST_MODELS_BY_PROTOCOL[WIRE_PROTOCOL_V1]
    original = selected["provision"]

    monkeypatch.setitem(REQUEST_MODELS, "provision", REQUEST_MODELS["health"])

    assert selected["provision"] is original
    with pytest.raises(TypeError):
        selected["provision"] = ProvisionRequest  # type: ignore[index]


def test_runtime_identity_is_pure_for_legacy_and_v2_request_dictionaries() -> None:
    legacy: dict[str, Any] = {"releaseVersion": "0.22.0", "protocolVersion": "1"}
    v2 = {"runtimeTarget": _runtime_target()}
    legacy_before = legacy.copy()
    v2_before = {"runtimeTarget": v2["runtimeTarget"].copy()}

    assert runtime_identity(legacy) == {"releaseVersion": "0.22.0", "protocolVersion": "1"}
    assert runtime_identity(v2) == _runtime_target()
    assert legacy == legacy_before
    assert v2 == v2_before


@pytest.mark.asyncio
async def test_fake_driver_emits_the_selected_health_protocol_shape() -> None:
    driver = FakeDriver()
    context = EffectContext(
        operation_id="health-v2-alpha",
        provider_operation_id="provider-health-v2-alpha",
        tenant_id="tenant-v2-alpha",
        cell_id="cell-v2-alpha",
        fence_generation=1,
    )
    request = {
        "cellId": "cell-v2-alpha",
        "workerPolicy": {"workerCount": 2, "semantic": True, "media": False},
        "runtimeTarget": _runtime_target(),
    }

    result = await driver.execute("health", request, context)

    assert result.result == {
        "live": True,
        "ready": True,
        "cellId": "cell-v2-alpha",
        "runtimeIdentity": _runtime_target(),
        "serviceAuthenticated": True,
        "mutationAuthority": True,
        "readAdmission": True,
        "writeAdmission": True,
        "workerPolicy": {"workerCount": 2, "semantic": True, "media": False},
        "code": "CELL_READY",
    }


def _substitute_tokens(value: object) -> object:
    if value == "$ACTIVE_SERVICE_CREDENTIAL":
        return _ACTIVE_CREDENTIAL
    if value == "$NEXT_SERVICE_CREDENTIAL":
        return _NEXT_CREDENTIAL
    if value == "$NOW_PLUS_86400_SECONDS":
        return (datetime.now(UTC) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    if value == "$NOW_PLUS_600_SECONDS":
        return (datetime.now(UTC) + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {name: _substitute_tokens(item) for name, item in value.items()}
    if isinstance(value, list):
        return [_substitute_tokens(item) for item in value]
    return value


@pytest.mark.parametrize(
    ("filename", "protocol", "expected_hash"),
    [
        (
            "provisioner-wire-v1.json",
            WIRE_PROTOCOL_V1,
            "ced714a5aa204a837e22cab831262cc0ae4766e44720b2896e61b8c157ddd3b5",
        ),
        ("provisioner-wire-v2.json", WIRE_PROTOCOL_V2, None),
    ],
)
def test_frozen_wire_corpora_validate_all_request_response_and_failure_shapes(
    filename: str, protocol: str, expected_hash: str | None
) -> None:
    fixture = _FIXTURES / filename
    payload = json.loads(fixture.read_bytes())

    if expected_hash is not None:
        assert hashlib.sha256(fixture.read_bytes()).hexdigest() == expected_hash
    assert payload["protocol"] == protocol
    assert set(payload["actions"]) == set(REQUEST_MODELS_BY_PROTOCOL[protocol])
    for action, sample in payload["actions"].items():
        request = _substitute_tokens(sample["request"])
        model = REQUEST_MODELS_BY_PROTOCOL[protocol][action].model_validate(request)
        assert request_plaintext(model) == request
        pending = sample["pending"]
        assert pending["status"] == 202
        assert pending["headers"] == {"retry-after": "2"}
        PendingResponse.model_validate(pending["body"])
        final = sample["final"]
        response_model = FINAL_MODELS_BY_PROTOCOL[protocol][action]
        if response_model is None:
            assert final == {"status": 204, "body": None}
        else:
            assert final["status"] == 200
            response_model.model_validate(_substitute_tokens(final["body"]))
    for failure in payload["errors"] if protocol == WIRE_PROTOCOL_V1 else payload["failures"]:
        assert failure["status"] in {400, 409, 422, 500, 503}
        FailureResponse.model_validate(failure["body"])
        assert "sentinel" not in json.dumps(failure["body"])
    if protocol == WIRE_PROTOCOL_V2:
        assert {
            (failure["status"], failure["body"]["code"], failure["body"]["retryable"])
            for failure in payload["failures"]
        } == {
            (400, "PROVISIONER_REJECTED", False),
            (409, "CONTROL_PLANE_STATE_CONFLICT", False),
            (422, "EXPORT_REQUEST_EXPIRED", False),
            (422, "PROVISIONER_REJECTED", False),
            (500, "PROVISIONER_RESPONSE_INVALID", False),
            (503, "PROVISIONER_UNAVAILABLE", True),
        }
        assert FailureResponse.model_validate(payload["mismatch"]["body"]).code == "PROVISIONER_REJECTED"
        assert FailureResponse.model_validate(payload["replayFailure"]["body"]).code == (
            "CONTROL_PLANE_STATE_CONFLICT"
        )


def test_selected_health_final_models_reject_mixed_version_envelopes() -> None:
    v1 = json.loads((_FIXTURES / "provisioner-wire-v1.json").read_bytes())
    v2 = json.loads((_FIXTURES / "provisioner-wire-v2.json").read_bytes())
    v1_health = _substitute_tokens(v1["actions"]["health"]["final"]["body"])
    v2_health = _substitute_tokens(v2["actions"]["health"]["final"]["body"])

    with pytest.raises(ValidationError):
        FINAL_MODELS_BY_PROTOCOL[WIRE_PROTOCOL_V2]["health"].model_validate(v1_health)  # type: ignore[union-attr]
    with pytest.raises(ValidationError):
        FINAL_MODELS_BY_PROTOCOL[WIRE_PROTOCOL_V1]["health"].model_validate(v2_health)  # type: ignore[union-attr]
