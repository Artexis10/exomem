from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from exomem import commands
from exomem import hosted_gateway as gateway
from exomem.hosted_runtime import HostedCellConfig, HostedResourceLimits
from exomem.server_auth import HostedCellTokenVerifier

PRINCIPAL_ALPHA = (
    base64.urlsafe_b64encode(hashlib.sha256(b"principal-alpha").digest()).rstrip(b"=").decode()
)
PRINCIPAL_BRAVO = (
    base64.urlsafe_b64encode(hashlib.sha256(b"principal-bravo").digest()).rstrip(b"=").decode()
)


def _config(
    tmp_path: Path,
    *,
    cell_id: str = "cell-alpha",
    credential: str = "alpha-private-service-credential-0001",
) -> HostedCellConfig:
    return HostedCellConfig(
        cell_id=cell_id,
        vault_root=tmp_path / cell_id / "vault",
        state_root=tmp_path / cell_id / "state",
        log_root=tmp_path / cell_id / "logs",
        service_credential=credential,
        resource_limits=HostedResourceLimits(
            storage_bytes=4096,
            upload_bytes=1024,
            worker_count=0,
        ),
    )


def _decode_payload(token: str) -> dict:
    encoded = token.split(".", 1)[0]
    padding = "=" * (-len(encoded) % 4)
    return json.loads(base64.urlsafe_b64decode(encoded + padding))


def test_gateway_contract_is_canonical_versioned_and_registry_derived() -> None:
    contract = gateway.build_gateway_contract()
    repeated = gateway.build_gateway_contract()
    expected = commands.product_commands_for("rest")

    assert gateway.canonical_contract_json(contract) == gateway.canonical_contract_json(repeated)
    assert contract["schema_version"] == 1
    assert contract["protocol_version"] == "1"
    assert contract["compatibility"]["policy"] == "additive"
    assert contract["envelopes"]["success"]["required"] == ["success", "data"]
    assert contract["envelopes"]["error"]["required"] == ["success", "error"]
    assert "tenant" not in contract["trusted_headers"]
    with pytest.raises(gateway.HostedGatewayError) as unsupported:
        gateway.build_gateway_contract(protocol_version="2.alpha")
    assert unsupported.value.code == "HOSTED_PROTOCOL_UNSUPPORTED"

    rendered = contract["commands"]
    assert [item["name"] for item in rendered] == [command.name for command in expected]
    for item, command in zip(rendered, expected, strict=True):
        assert item["read_only"] is command.read_only
        assert item["mode"] == ("read" if command.read_only else "write")
        assert item["tier"] == command.tier
        assert item["capability"] == ("core" if command.tier == 1 else "tier-2")
        assert item["product_surface"] == command.product_surface
        assert item["actions"] == list(command.product_actions)
        assert item["routes"] == list(command.routes)
        assert item["params"] == [
            {
                "name": param.name,
                "type": param.type,
                "required": param.required,
                "description": param.help,
            }
            for param in command.params
        ]

    unsigned = dict(contract)
    digest = unsigned.pop("digest")
    assert digest == {
        "algorithm": "sha256",
        "value": hashlib.sha256(gateway.canonical_json(unsigned)).hexdigest(),
    }
    assert "Paddle" not in gateway.canonical_contract_json(contract).decode()


def test_hosted_token_verifier_uses_constant_time_cell_credential(tmp_path: Path) -> None:
    config = _config(tmp_path)
    verifier = HostedCellTokenVerifier(config)

    accepted = asyncio.run(verifier.verify_token(config.service_credential))
    rejected = asyncio.run(verifier.verify_token("wrong-private-credential-value-00000"))

    assert accepted is not None
    assert accepted.client_id == config.cell_id
    assert accepted.claims == {"cell_id": config.cell_id, "kind": "hosted-cell-service"}
    assert config.service_credential not in repr(accepted)
    assert rejected is None


def test_transfer_grants_are_strictly_bound_and_use_no_second_secret(tmp_path: Path) -> None:
    config = _config(tmp_path)
    grant = gateway.mint_transfer_grant(
        config,
        tenant_scope="tenant-7fa1",
        principal_scope=PRINCIPAL_ALPHA,
        operation="upload",
        jti="grant-001",
        max_bytes=512,
        now=1_700_000_000,
        ttl_seconds=300,
    )
    claims = _decode_payload(grant)

    assert claims == {
        "aud": gateway.TRANSFER_AUDIENCE,
        "cell": "cell-alpha",
        "exp": 1_700_000_300,
        "iat": 1_700_000_000,
        "jti": "grant-001",
        "limits": {"max_bytes": 512},
        "op": "upload",
        "principal": PRINCIPAL_ALPHA,
        "tenant": "tenant-7fa1",
        "v": 1,
    }
    assert config.service_credential not in grant
    assert str(config.vault_root) not in grant

    verified = gateway.verify_transfer_grant(
        grant,
        config,
        expected_operation="upload",
        expected_tenant_scope="tenant-7fa1",
        expected_principal_scope=PRINCIPAL_ALPHA,
        now=1_700_000_010,
    )
    assert verified.jti == "grant-001"
    assert verified.max_bytes == 512

    mismatches = [
        {"expected_operation": "download"},
        {"expected_tenant_scope": "tenant-other"},
        {"expected_principal_scope": PRINCIPAL_BRAVO},
    ]
    for override in mismatches:
        kwargs = {
            "expected_operation": "upload",
            "expected_tenant_scope": "tenant-7fa1",
            "expected_principal_scope": PRINCIPAL_ALPHA,
            "now": 1_700_000_010,
            **override,
        }
        with pytest.raises(gateway.HostedGatewayError) as error:
            gateway.verify_transfer_grant(grant, config, **kwargs)
        assert error.value.code == "HOSTED_TRANSFER_GRANT_INVALID"
        assert config.service_credential not in str(error.value)


def test_transfer_grants_reject_expiry_tampering_cross_cell_and_unsafe_limits(
    tmp_path: Path,
) -> None:
    alpha = _config(tmp_path)
    bravo = _config(
        tmp_path,
        cell_id="cell-bravo",
        credential="bravo-private-service-credential-0002",
    )
    grant = gateway.mint_transfer_grant(
        alpha,
        tenant_scope="tenant-alpha",
        principal_scope=PRINCIPAL_ALPHA,
        operation="download",
        jti="grant-expiry",
        max_bytes=256,
        now=1_700_000_000,
        ttl_seconds=30,
    )
    expected = {
        "expected_operation": "download",
        "expected_tenant_scope": "tenant-alpha",
        "expected_principal_scope": PRINCIPAL_ALPHA,
    }

    with pytest.raises(gateway.HostedGatewayError) as expired:
        gateway.verify_transfer_grant(grant, alpha, now=1_700_000_031, **expected)
    assert expired.value.code == "HOSTED_TRANSFER_GRANT_EXPIRED"

    with pytest.raises(gateway.HostedGatewayError) as cross_cell:
        gateway.verify_transfer_grant(grant, bravo, now=1_700_000_001, **expected)
    assert cross_cell.value.code == "HOSTED_TRANSFER_GRANT_INVALID"

    payload, signature = grant.split(".")
    tampered_payload = payload[:-1] + ("A" if payload[-1] != "A" else "B")
    with pytest.raises(gateway.HostedGatewayError) as tampered:
        gateway.verify_transfer_grant(
            f"{tampered_payload}.{signature}", alpha, now=1_700_000_001, **expected
        )
    assert tampered.value.code == "HOSTED_TRANSFER_GRANT_INVALID"

    with pytest.raises(gateway.HostedGatewayError) as excessive:
        gateway.mint_transfer_grant(
            alpha,
            tenant_scope="tenant-alpha",
            principal_scope=PRINCIPAL_ALPHA,
            operation="upload",
            jti="grant-large",
            max_bytes=alpha.resource_limits.upload_bytes + 1,
            now=1_700_000_000,
        )
    assert excessive.value.code == "HOSTED_TRANSFER_LIMIT_INVALID"


def test_hosted_idempotency_scope_changes_by_cell_and_principal(tmp_path: Path) -> None:
    alpha = gateway.TrustedGatewayContext(
        cell_id="cell-alpha",
        protocol_version="1",
        request_id="11111111-1111-4111-8111-111111111111",
        principal_scope=PRINCIPAL_ALPHA,
        idempotency_key="same-public-key",
    )
    bravo = gateway.TrustedGatewayContext(
        cell_id="cell-bravo",
        protocol_version="1",
        request_id="11111111-1111-4111-8111-111111111111",
        principal_scope=PRINCIPAL_ALPHA,
        idempotency_key="same-public-key",
    )
    other_principal = gateway.TrustedGatewayContext(
        cell_id="cell-alpha",
        protocol_version="1",
        request_id="11111111-1111-4111-8111-111111111111",
        principal_scope=PRINCIPAL_BRAVO,
        idempotency_key="same-public-key",
    )

    keys = {gateway.scoped_idempotency_key(context) for context in (alpha, bravo, other_principal)}
    assert len(keys) == 3
    assert all(key and key.startswith("hosted:") for key in keys)
    assert not any("principal" in key or "tenant" in key for key in keys)


def test_implicit_retry_scope_is_stable_across_gateway_request_ids() -> None:
    first = gateway.TrustedGatewayContext(
        cell_id="cell-alpha",
        protocol_version="1",
        request_id="11111111-1111-4111-8111-111111111111",
        principal_scope=PRINCIPAL_ALPHA,
    )
    second = replace(
        first,
        request_id="22222222-2222-4222-8222-222222222222",
    )

    assert gateway.implicit_retry_scope(first) == gateway.implicit_retry_scope(second)


def test_gateway_context_identity_formats_are_exact() -> None:
    assert (
        gateway.validate_request_id("11111111-1111-4111-8111-111111111111")
        == "11111111-1111-4111-8111-111111111111"
    )
    assert gateway.validate_principal_scope(PRINCIPAL_ALPHA) == PRINCIPAL_ALPHA

    for invalid in (
        "request-001",
        "11111111-1111-1111-8111-111111111111",
        "11111111-1111-4111-8111-111111111111-extra",
    ):
        with pytest.raises(gateway.HostedGatewayError) as error:
            gateway.validate_request_id(invalid)
        assert error.value.code == "HOSTED_CONTEXT_INVALID"

    for invalid in ("principal-alpha", PRINCIPAL_ALPHA + "A", "a" * 43):
        with pytest.raises(gateway.HostedGatewayError) as error:
            gateway.validate_principal_scope(invalid)
        assert error.value.code == "HOSTED_CONTEXT_INVALID"
