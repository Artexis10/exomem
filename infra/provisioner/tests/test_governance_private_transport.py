from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import replace

import httpx
import pytest

from exomem_provisioner.adapters import PrivateCellApiAdapter
from exomem_provisioner.driver import DriverRetryable
from exomem_provisioner.lifecycle import LifecycleConfig, MetadataConflict, OpaqueProviderMetadata

METADATA = OpaqueProviderMetadata("tenant-alpha", "cell-alpha", "operation-alpha", 7)
CREDENTIAL = "a" * 43


def _health_config() -> LifecycleConfig:
    return LifecycleConfig(
        image="registry.example/provisioner@sha256:" + "a" * 64,
        chart_path="chart",
        chart_version="0.1.0",
        helm_version="3.19.4",
        control_hostname="control.example.invalid",
        transfer_hostname="transfer.example.invalid",
        browser_origin="https://example.invalid",
        release_version="0.22.0",
        protocol_version="1",
        contract_digest="b" * 64,
        location="fsn1",
    )


def _ready() -> dict[str, object]:
    return {
        "cell_id": METADATA.subject_id,
        "vault_id": METADATA.tenant_id,
        "exomem_release": "0.22.0",
        "hosted_protocol": "1",
        "authenticated_credential_version": "1",
        "security_revision": 1,
        "service_authenticated": True,
        "mutation_authority": True,
        "admission_phase": "active",
        "read_admission": True,
        "write_admission": True,
        "worker_policy_digest": hashlib.sha256(b"{}").hexdigest(),
    }


def _runtime_health_config() -> LifecycleConfig:
    target = {
        "releaseVersion": "0.22.0",
        "protocolVersion": "1",
        "agentProfile": "hosted-alpha-agent-v1",
        "gatewayContractDigest": "b" * 64,
        "commandFingerprint": "c" * 64,
        "schemaDigest": "d" * 64,
    }
    return replace(
        _health_config(),
        runtime_target=target,
        legacy_runtime_units={("0.22.0", "1"): target},
    )


@pytest.mark.parametrize("failure", ["transport", "status"])
async def test_governance_health_retries_agent_contract_failure(failure: str) -> None:
    agent_contract = {
        "schema_version": 1,
        "protocol_version": "1",
        "exomem_release": "0.22.0",
        "agent_profile": {
            "profile": "hosted-alpha-agent-v1",
            "active_capability_sha256": "c" * 64,
        },
        "commands": [],
    }
    runtime_digest = hashlib.sha256(
        json.dumps(agent_contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    async def request(method: str, url: str, **kwargs: object) -> httpx.Response:
        del method, kwargs
        if url.endswith("/agent/hosted-alpha-agent-v1/contract"):
            if failure == "transport":
                raise httpx.ReadTimeout("private payload")
            return httpx.Response(503)
        if url.endswith("/live"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"live": True, "cell_id": METADATA.subject_id, "protocol_version": "1"},
                },
            )
        if url.endswith("/ready"):
            return httpx.Response(200, json={"success": True, "data": _ready()})
        if url.endswith("/contract"):
            return httpx.Response(200, json={"digest": {"algorithm": "sha256", "value": "b" * 64}})
        return httpx.Response(
            200, json={**agent_contract, "digest": {"algorithm": "sha256", "value": runtime_digest}}
        )

    adapter = PrivateCellApiAdapter(request=request, internal_origin="https://cells.invalid")
    with pytest.raises(DriverRetryable):
        await adapter.health(
            METADATA,
            credential=CREDENTIAL,
            protocol_version="1",
            config=_runtime_health_config(),
            expected_release="0.22.0",
            expected_worker_policy={},
            require_runtime_identity=True,
            retry_transport=True,
        )


@pytest.mark.parametrize("route", ["live", "ready", "contract"])
async def test_governance_health_retries_each_required_get_transport_failure(route: str) -> None:
    async def request(method: str, url: str, **kwargs: object) -> httpx.Response:
        del method, kwargs
        if url.endswith("/" + route):
            raise httpx.ReadTimeout("private payload")
        if url.endswith("/live"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"live": True, "cell_id": METADATA.subject_id, "protocol_version": "1"},
                },
            )
        if url.endswith("/ready"):
            return httpx.Response(200, json={"success": True, "data": _ready()})
        return httpx.Response(200, json={"digest": {"algorithm": "sha256", "value": "b" * 64}})

    adapter = PrivateCellApiAdapter(request=request, internal_origin="https://cells.invalid")
    with pytest.raises(DriverRetryable) as raised:
        await adapter.health(
            METADATA,
            credential=CREDENTIAL,
            protocol_version="1",
            config=_health_config(),
            expected_release="0.22.0",
            expected_worker_policy={},
            retry_transport=True,
        )
    assert "private payload" not in str(raised.value)


@pytest.mark.parametrize("route", ["live", "ready", "contract"])
async def test_governance_health_retries_each_required_get_503(route: str) -> None:
    async def request(method: str, url: str, **kwargs: object) -> httpx.Response:
        del method, kwargs
        if url.endswith("/" + route):
            return httpx.Response(503)
        if url.endswith("/live"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"live": True, "cell_id": METADATA.subject_id, "protocol_version": "1"},
                },
            )
        if url.endswith("/ready"):
            return httpx.Response(200, json={"success": True, "data": _ready()})
        return httpx.Response(200, json={"digest": {"algorithm": "sha256", "value": "b" * 64}})

    adapter = PrivateCellApiAdapter(request=request, internal_origin="https://cells.invalid")
    with pytest.raises(DriverRetryable):
        await adapter.health(
            METADATA,
            credential=CREDENTIAL,
            protocol_version="1",
            config=_health_config(),
            expected_release="0.22.0",
            expected_worker_policy={},
            retry_transport=True,
        )


async def test_governance_health_keeps_default_and_unauthorized_failures_strict() -> None:
    async def unavailable(*args: object, **kwargs: object) -> httpx.Response:
        return httpx.Response(503)

    adapter = PrivateCellApiAdapter(request=unavailable, internal_origin="https://cells.invalid")
    with pytest.raises(MetadataConflict):
        await adapter.health(
            METADATA,
            credential=CREDENTIAL,
            protocol_version="1",
            config=_health_config(),
            expected_release="0.22.0",
            expected_worker_policy={},
        )

    async def unauthorized(*args: object, **kwargs: object) -> httpx.Response:
        return httpx.Response(401)

    adapter = PrivateCellApiAdapter(request=unauthorized, internal_origin="https://cells.invalid")
    with pytest.raises(MetadataConflict):
        await adapter.health(
            METADATA,
            credential=CREDENTIAL,
            protocol_version="1",
            config=_health_config(),
            expected_release="0.22.0",
            expected_worker_policy={},
            retry_transport=True,
        )


async def test_governance_private_calls_retry_transport_loss_only_when_requested() -> None:
    async def timeout(*args: object, **kwargs: object):
        raise httpx.ReadTimeout("private payload")

    adapter = PrivateCellApiAdapter(request=timeout, internal_origin="https://cells.invalid")
    with pytest.raises(httpx.ReadTimeout):
        await adapter.resume(
            METADATA,
            credential=CREDENTIAL,
            protocol_version="1",
            operation_id=METADATA.operation_id,
        )
    with pytest.raises(DriverRetryable) as raised:
        await adapter.resume(
            METADATA,
            credential=CREDENTIAL,
            protocol_version="1",
            operation_id=METADATA.operation_id,
            retry_transport=True,
        )
    assert "private payload" not in str(raised.value)


@pytest.mark.parametrize("status", [408, 429, 503])
async def test_governance_private_calls_retry_only_transient_statuses(status: int) -> None:
    async def response(*args: object, **kwargs: object):
        return httpx.Response(status)

    adapter = PrivateCellApiAdapter(request=response, internal_origin="https://cells.invalid")
    with pytest.raises(DriverRetryable):
        await adapter.quiesce(
            METADATA,
            credential=CREDENTIAL,
            protocol_version="1",
            operation_id=METADATA.operation_id,
            retry_transport=True,
        )


async def test_governance_private_calls_keep_unauthorized_response_terminal() -> None:
    async def response(*args: object, **kwargs: object):
        return httpx.Response(401)

    adapter = PrivateCellApiAdapter(request=response, internal_origin="https://cells.invalid")
    with pytest.raises(MetadataConflict):
        await adapter.resume(
            METADATA,
            credential=CREDENTIAL,
            protocol_version="1",
            operation_id=METADATA.operation_id,
            retry_transport=True,
        )


def test_governance_private_retry_option_is_keyword_only_and_off_by_default() -> None:
    for method in (
        PrivateCellApiAdapter._call,
        PrivateCellApiAdapter.resume,
        PrivateCellApiAdapter.quiesce,
        PrivateCellApiAdapter.attest_authorization_session_membership,
    ):
        parameter = inspect.signature(method).parameters["retry_transport"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is False
