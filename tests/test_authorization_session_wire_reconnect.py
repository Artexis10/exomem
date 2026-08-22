"""Actual-wire authorization-session continuity across stateless replicas."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from exomem import server, writer_lease
from exomem.governance import authorization_custody, schema_v4, store

SERVICE_BEARER = "wire-service-principal"
UNKNOWN_SESSION_BEARER = (
    "as1.AQEBAQEBAQEBAQEBAQEBAQ."
    "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI"
)


@pytest.fixture(autouse=True)
def _reset_writer_state() -> None:
    writer_lease.reset_managers_for_tests()
    yield
    writer_lease.reset_managers_for_tests()


def _seed(now: int) -> schema_v4.MigrationSeed:
    return schema_v4.MigrationSeed(
        activation_store_id="activation-store-wire",
        logical_vault_id="logical-vault-wire",
        activation_epoch=1,
        policy=schema_v4.PolicyGenerationSeed(
            generation_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            source_documents=(("rules/release.yaml", b"governance_version: 1\n"),),
            source_fingerprint="1" * 64,
            conflict_digest="2" * 64,
            compiled_policy=b'{"rules":[]}',
            policy_fingerprint="3" * 64,
            compiler_schema_version=1,
            projector_schema_version=1,
            predecessor_generation_id=None,
            authoring_event_id="event-wire-policy",
            receipt_event_id="receipt-wire-policy",
            created_at=now,
        ),
        catalog=schema_v4.CatalogGenerationSeed(
            catalog_generation=1,
            descriptor=b'{"artifacts":[]}',
            artifact_count=0,
            created_at=now,
        ),
        namespace=schema_v4.ProjectionNamespaceSeed(
            namespace_id="projection-namespace-wire",
            evidence=b'{"ready":true}',
            ready_at=now,
        ),
        migrated_at=now,
    )


def _private_file(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    if os.name != "nt":
        path.chmod(0o600)


def _framed(domain: bytes, fields: list[bytes]) -> bytes:
    output = bytearray(domain)
    output.append(0)
    for field in fields:
        output.extend(len(field).to_bytes(4, "big"))
        output.extend(field)
    return bytes(output)


def _configure_v4_authority(
    vault: Path,
    custody_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    connection = store.open_connection(vault)
    try:
        migration = schema_v4.migrate_v3_connection(connection, _seed(now))
    finally:
        connection.close()

    key = b"k" * 32
    key_id = "auth-key-wire"
    keyring_id = "keyring-wire"
    cell_id = "cell-wire"
    logical_vault_id = "logical-vault-wire"
    not_before = now - 60
    not_after = now + 7_200
    issued_at = now - 1
    expires_at = now + 3_600
    keyring = {
        "version": 1,
        "keyring_id": keyring_id,
        "cell_id": cell_id,
        "logical_vault_id": logical_vault_id,
        "active_key_id": key_id,
        "accepted_keys": [
            {
                "key_id": key_id,
                "key": base64.urlsafe_b64encode(key).rstrip(b"=").decode("ascii"),
                "not_before": not_before,
                "not_after": not_after,
            }
        ],
    }
    control: dict[str, object] = {
        "version": 1,
        "keyring_id": keyring_id,
        "cell_id": cell_id,
        "logical_vault_id": logical_vault_id,
        "registry_attachment_id": "attachment-wire",
        "attachment_epoch": 1,
        "governance_enrolled": True,
        "activation_store_id": migration.activation_store_id,
        "activation_epoch": 1,
        "activation_state_digest": migration.activation_state_digest,
        "serving_membership_epoch": 1,
        "serving_membership_digest": "4" * 64,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "signing_key_id": key_id,
    }
    fields = [
        str(control["version"]).encode(),
        str(control["keyring_id"]).encode(),
        str(control["cell_id"]).encode(),
        str(control["logical_vault_id"]).encode(),
        str(control["registry_attachment_id"]).encode(),
        str(control["attachment_epoch"]).encode(),
        b"true",
        str(control["activation_store_id"]).encode(),
        str(control["activation_epoch"]).encode(),
        str(control["activation_state_digest"]).encode(),
        str(control["serving_membership_epoch"]).encode(),
        str(control["serving_membership_digest"]).encode(),
        str(control["issued_at"]).encode(),
        str(control["expires_at"]).encode(),
        str(control["signing_key_id"]).encode(),
    ]
    control["mac"] = (
        base64.urlsafe_b64encode(
            hmac.new(
                key,
                _framed(b"exomem.authorization-session.control/v1", fields),
                hashlib.sha256,
            ).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )

    keyring_path = custody_root / "authorization-keyring.json"
    control_path = custody_root / "authorization-control.json"
    _private_file(keyring_path, json.dumps(keyring, separators=(",", ":")).encode())
    _private_file(control_path, json.dumps(control, separators=(",", ":")).encode())
    monkeypatch.setenv(authorization_custody.KEYRING_FILE_ENV, str(keyring_path))
    monkeypatch.setenv(authorization_custody.CONTROL_FILE_ENV, str(control_path))


def _build_replica(vault: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(vault.parent / "writer-lease-state")
    )
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_WARMUP", "1")
    monkeypatch.delenv("EXOMEM_REST_API_KEY", raising=False)
    return server.build_server(require_auth=False).http_app(
        stateless_http=True,
        json_response=True,
    )


def _request(
    client: TestClient,
    request_id: int,
    arguments: dict[str, object],
    *,
    service_bearer: str = SERVICE_BEARER,
):
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": "govern_memory", "arguments": arguments},
        },
        headers={
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            "authorization": f"Bearer {service_bearer}",
        },
    )


def _call(
    client: TestClient,
    request_id: int,
    arguments: dict[str, object],
) -> dict[str, object]:
    response = _request(client, request_id, arguments)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result.get("isError") is not True, result
    structured = result.get("structuredContent")
    assert isinstance(structured, dict), result
    if arguments.get("session_action") in {"open", "rotate"}:
        credential = structured.get("issued_credential")
        if not isinstance(credential, dict):
            diagnostics = structured.get("diagnostics")
            assert isinstance(diagnostics, dict), structured
            credential = diagnostics.get("issued_credential")
        assert isinstance(credential, dict), structured
        bearer = credential.get("bearer")
        assert isinstance(bearer, str), credential
        assert response.text.count(bearer) == 1, response.text
    return structured


def test_stateless_mcp_session_resumes_on_another_replica(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A server-issued bearer survives disconnect without trusting MCP session IDs."""

    _configure_v4_authority(vault, tmp_path / "custody", monkeypatch)

    replica_a = _build_replica(vault, monkeypatch)
    with TestClient(replica_a) as client:
        opened_terminal = _call(
            client,
            1,
            {
                "operation": "session",
                "session_action": "open",
                "ttl_seconds": 600,
                "response_detail": "compact",
            },
        )
    issued = opened_terminal["issued_credential"]
    assert isinstance(issued, dict)
    bearer = issued["bearer"]
    assert isinstance(bearer, str)

    writer_lease.reset_managers_for_tests()
    replica_b = _build_replica(vault, monkeypatch)
    with TestClient(replica_b) as client:
        replayed_open = _call(
            client,
            2,
            {
                "operation": "session",
                "session_action": "open",
                "ttl_seconds": 600,
            },
        )
        replayed_diagnostics = replayed_open["diagnostics"]
        assert isinstance(replayed_diagnostics, dict)
        assert replayed_diagnostics["issued_credential"]["bearer"] == bearer

        resumed_terminal = _call(
            client,
            3,
            {
                "operation": "session",
                "session_action": "status",
                "authorization_session_credential": bearer,
            },
        )
        for request_id, arguments, service_bearer in (
            (
                4,
                {"operation": "session", "session_action": "status"},
                SERVICE_BEARER,
            ),
            (
                5,
                {
                    "operation": "session",
                    "session_action": "status",
                    "authorization_session_credential": UNKNOWN_SESSION_BEARER,
                },
                SERVICE_BEARER,
            ),
            (
                6,
                {
                    "operation": "session",
                    "session_action": "status",
                    "authorization_session_credential": bearer,
                },
                "other-wire-principal",
            ),
        ):
            refused = _request(
                client,
                request_id,
                arguments,
                service_bearer=service_bearer,
            )
            assert refused.status_code == 200
            assert "authorization session is unavailable" in refused.text
            assert bearer not in refused.text
            assert UNKNOWN_SESSION_BEARER not in refused.text

    resumed = resumed_terminal["diagnostics"]
    assert isinstance(resumed, dict)
    assert resumed["status"] == "active"
    assert "issued_credential" not in resumed
    assert bearer not in json.dumps(resumed, sort_keys=True)
    assert bearer not in caplog.text
