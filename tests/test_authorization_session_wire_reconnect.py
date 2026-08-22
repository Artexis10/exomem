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

from record_fixtures import copy_dataset_fixture

from exomem import commands, server, video_frames, writer_lease
from exomem.governance import (
    authorization_custody,
    authorization_session_authority,
    authorization_session_lifecycle,
    membership,
    policy,
    schema_v4,
    store,
)
from exomem.governance import principal as principal_module
from exomem.governance import tool as governance_tool

SERVICE_BEARER = "wire-service-principal"
UNKNOWN_SESSION_BEARER = (
    "as1.AQEBAQEBAQEBAQEBAQEBAQ."
    "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI"
)
SCOPE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
RULE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FB0"
NOTE_PATH = "Knowledge Base/Notes/Insights/session-route.md"
COLLECTION_PATH = "Knowledge Base/Records/dataset/_collection.md"
DATASET_PATH = "Knowledge Base/Records/dataset/readings.csv"
VIDEO_PATH = "Knowledge Base/Sources/session-route.mp4"
NOTE_MARKER = "wire-session-route-sentinel"
DATASET_MARKER = "category-068"


@pytest.fixture(autouse=True)
def _reset_writer_state() -> None:
    writer_lease.reset_managers_for_tests()
    yield
    writer_lease.reset_managers_for_tests()


def _seed(
    now: int,
    *,
    policy_fingerprint: str = "3" * 64,
) -> schema_v4.MigrationSeed:
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
            policy_fingerprint=policy_fingerprint,
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
    *,
    policy_fingerprint: str = "3" * 64,
) -> None:
    now = int(time.time())
    connection = store.open_connection(vault)
    try:
        migration = schema_v4.migrate_v3_connection(
            connection,
            _seed(now, policy_fingerprint=policy_fingerprint),
        )
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
    tool_name: str = "govern_memory",
    service_bearer: str = SERVICE_BEARER,
    mcp_session_id: str | None = None,
):
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "authorization": f"Bearer {service_bearer}",
    }
    if mcp_session_id is not None:
        headers["mcp-session-id"] = mcp_session_id
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
        headers=headers,
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


def _service_identity(service_bearer: str = SERVICE_BEARER) -> tuple[str, str]:
    return (
        principal_module.normalize_audience(
            subject=service_bearer,
            issuer="bearer",
        ),
        "mcp-oauth:" + hashlib.sha256(b"bearer").hexdigest(),
    )


def _write_route_fixture(vault: Path, audience: str) -> tuple[str, tuple[str, ...]]:
    note = vault / NOTE_PATH
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        "---\n"
        "type: insight\n"
        "exomem_id: 00000000-0000-4000-8000-000000000737\n"
        "status: active\n"
        "created: 2026-08-22\n"
        "updated: 2026-08-22\n"
        "tags: [wire-session]\n"
        "---\n\n"
        "# Session route\n\n"
        "## Observations\n\n"
        f"- [fact] {NOTE_MARKER} remains visible only to the granted session.\n",
        encoding="utf-8",
    )
    collection_root = copy_dataset_fixture(vault)
    video = vault / VIDEO_PATH
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"\x00wire-session-video")

    governance = vault / "Knowledge Base" / "_Governance"
    (governance / "scopes").mkdir(parents=True, exist_ok=True)
    (governance / "rules").mkdir(parents=True, exist_ok=True)
    (governance / "scopes" / "wire-routes.yaml").write_text(
        "governance_version: 1\n"
        f"id: {SCOPE_ID}\n"
        "name: Wire session routes\n"
        'paths: ["Notes/**", "Records/**", "Sources/**"]\n',
        encoding="utf-8",
    )
    (governance / "rules" / "wire-routes.yaml").write_text(
        "governance_version: 1\n"
        f"id: {RULE_ID}\n"
        f'scope_ids: ["{SCOPE_ID}"]\n'
        f"audience: {audience}\n"
        "ceiling: 0\n",
        encoding="utf-8",
    )
    policy._CACHE.clear()
    membership.clear_memo()
    compiled = policy.load(vault)
    assert not compiled.empty and not compiled.blocked

    paths = tuple(
        sorted(
            {
                NOTE_PATH,
                COLLECTION_PATH,
                DATASET_PATH,
                VIDEO_PATH,
                *(
                    path.relative_to(vault).as_posix()
                    for path in collection_root.rglob("*")
                    if path.is_file()
                ),
            }
        )
    )
    return compiled.fingerprint, paths


def _mint_route_grant_token(
    vault: Path,
    *,
    bearer: str,
    paths: tuple[str, ...],
) -> str:
    now = int(time.time())
    audience, issuer_family = _service_identity()
    custody = authorization_custody.load_authorization_custody(vault, now=now)
    connection = store.open_authorization_session_connection(vault)
    try:
        context = authorization_session_lifecycle.resume_session(
            connection,
            custody=custody,
            bearer=bearer,
            principal_id=audience,
            issuer_family=issuer_family,
            now=now,
        )
        compiled = policy.load(vault)
        assert not compiled.empty and not compiled.blocked and compiled.scopes, compiled
        manifest = governance_tool._resolved_membership_manifest(
            vault,
            compiled,
            paths,
        )
        assert all(row["scope_ids"] == [SCOPE_ID] for row in manifest), manifest
        return authorization_session_authority.mint_escalation_token(
            connection,
            context=context,
            signing_key=custody.keyring.active_key.key,
            audience=audience,
            purpose=None,
            max_level=6,
            org_ceiling=6,
            paths=paths,
            fingerprints=tuple(
                hashlib.sha256((vault / path).read_bytes()).hexdigest()
                for path in paths
            ),
            scope_ids=(SCOPE_ID,),
            now=now,
            expires_at=now + 300,
        )
    finally:
        connection.close()


def _tool_response(
    client: TestClient,
    request_id: int,
    tool_name: str,
    arguments: dict[str, object],
    *,
    service_bearer: str = SERVICE_BEARER,
    mcp_session_id: str | None = None,
):
    response = _request(
        client,
        request_id,
        arguments,
        tool_name=tool_name,
        service_bearer=service_bearer,
        mcp_session_id=mcp_session_id,
    )
    assert response.status_code == 200, response.text
    return response


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


def test_stateless_mcp_grant_is_bound_across_every_content_route_family(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only the granted capability survives reconnect across content families."""

    audience, _issuer_family = _service_identity()
    policy_fingerprint, grant_paths = _write_route_fixture(vault, audience)
    _configure_v4_authority(
        vault,
        tmp_path / "route-custody",
        monkeypatch,
        policy_fingerprint=policy_fingerprint,
    )

    canned = video_frames.FramesResult(
        path=VIDEO_PATH,
        duration_sec=42.0,
        frames=(video_frames.Frame(timestamp_sec=1.5, jpeg=b"\xff\xd8wire"),),
        candidates=1,
        dedup_dropped=0,
        max_frames_effective=1,
    )

    class _CannedVideoFramesModule:
        @staticmethod
        def get_frames(*_args, **_kwargs):
            return canned

    monkeypatch.setattr(
        commands,
        "_video_frames_module",
        lambda: _CannedVideoFramesModule,
    )

    replica_a = _build_replica(vault, monkeypatch)
    with TestClient(replica_a) as client:
        opened = _call(
            client,
            100,
            {
                "operation": "session",
                "session_action": "open",
                "ttl_seconds": 600,
            },
        )
        issued = opened["diagnostics"]["issued_credential"]
        assert isinstance(issued, dict)
        granted_bearer = issued["bearer"]
        assert isinstance(granted_bearer, str)
        token = _mint_route_grant_token(
            vault,
            bearer=granted_bearer,
            paths=grant_paths,
        )
        granted = _call(
            client,
            101,
            {
                "operation": "grant",
                "token": token,
                "duration_seconds": 300,
                "authorization_session_credential": granted_bearer,
            },
        )
        assert granted["status"] == "committed"

    writer_lease.reset_managers_for_tests()
    replica_b = _build_replica(vault, monkeypatch)
    collection = COLLECTION_PATH
    routes = (
        (
            "ask_memory",
            "ask_memory",
            {
                "query": NOTE_MARKER,
                "mode": "keyword",
                "graph": False,
                "limit": 5,
            },
            NOTE_MARKER,
        ),
        (
            "find",
            "find",
            {
                "query": NOTE_MARKER,
                "mode": "keyword",
                "graph": False,
                "limit": 5,
            },
            NOTE_MARKER,
        ),
        (
            "read_memory",
            "read_memory",
            {"path": NOTE_PATH, "include_raw": True},
            NOTE_MARKER,
        ),
        (
            "get",
            "get",
            {"path": NOTE_PATH, "include_raw": True},
            NOTE_MARKER,
        ),
        (
            "records-inspect",
            "record_memory",
            {"action": "inspect", "collection": collection},
            "Meter readings",
        ),
        (
            "records-query",
            "record_memory",
            {
                "action": "query",
                "collection": collection,
                "columns": ["reading_id", "category", "value"],
                "limit": 100,
            },
            DATASET_MARKER,
        ),
        (
            "query_dataset",
            "query_dataset",
            {
                "path": DATASET_PATH,
                "columns": ["reading_id", "category", "value"],
                "limit": 100,
            },
            DATASET_MARKER,
        ),
        (
            "read_media",
            "read_media",
            {"path": VIDEO_PATH, "max_frames": 1},
            '"duration_sec":42.0',
        ),
        (
            "get_video_frames",
            "get_video_frames",
            {"path": VIDEO_PATH, "max_frames": 1},
            '"duration_sec":42.0',
        ),
    )

    observed_wire: list[str] = []
    request_id = 102
    with TestClient(replica_b) as client:
        sibling = _call(
            client,
            request_id,
            {
                "operation": "session",
                "session_action": "open",
                "ttl_seconds": 601,
            },
        )
        request_id += 1
        sibling_issued = sibling["diagnostics"]["issued_credential"]
        assert isinstance(sibling_issued, dict)
        sibling_bearer = sibling_issued["bearer"]
        assert isinstance(sibling_bearer, str)
        assert sibling_bearer != granted_bearer

        for label, tool_name, arguments, marker in routes:
            valid = _tool_response(
                client,
                request_id,
                tool_name,
                {
                    **arguments,
                    "authorization_session_credential": granted_bearer,
                },
            )
            request_id += 1
            observed_wire.append(valid.text)
            assert marker in valid.text, (label, valid.text)
            assert valid.json()["result"].get("isError") is not True, (
                label,
                valid.text,
            )

            standing_only = _tool_response(
                client,
                request_id,
                tool_name,
                arguments,
            )
            request_id += 1
            transport_only = _tool_response(
                client,
                request_id,
                tool_name,
                arguments,
                mcp_session_id="transport-session-is-not-authority",
            )
            request_id += 1
            sibling_only = _tool_response(
                client,
                request_id,
                tool_name,
                {
                    **arguments,
                    "authorization_session_credential": sibling_bearer,
                },
            )
            request_id += 1
            assert standing_only.json()["result"] == transport_only.json()["result"], label
            for withheld in (standing_only, transport_only, sibling_only):
                observed_wire.append(withheld.text)
                assert marker not in withheld.text, (label, withheld.text)
                assert "authorization session is unavailable" not in withheld.text

            invalid = _tool_response(
                client,
                request_id,
                tool_name,
                {
                    **arguments,
                    "authorization_session_credential": UNKNOWN_SESSION_BEARER,
                },
            )
            request_id += 1
            cross_principal = _tool_response(
                client,
                request_id,
                tool_name,
                {
                    **arguments,
                    "authorization_session_credential": granted_bearer,
                },
                service_bearer="other-wire-principal",
            )
            request_id += 1
            for refused in (invalid, cross_principal):
                observed_wire.append(refused.text)
                assert "authorization session is unavailable" in refused.text, (
                    label,
                    refused.text,
                )
                assert marker not in refused.text

    wire_and_logs = "\n".join(observed_wire) + "\n" + caplog.text
    for raw_bearer in (
        granted_bearer,
        sibling_bearer,
        UNKNOWN_SESSION_BEARER,
    ):
        assert raw_bearer not in wire_and_logs
