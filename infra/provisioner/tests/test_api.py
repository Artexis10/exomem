from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from exomem_provisioner.app import create_app
from exomem_provisioner.config import PROVISIONER_PROTOCOL, ProvisionerSettings
from exomem_provisioner.crypto import AesGcmEnvelopeCodec
from exomem_provisioner.database import ProvisionerDatabase
from exomem_provisioner.driver import FakeDriver
from exomem_provisioner.provider_identity import ProviderRecoveryIdentityCodec
from exomem_provisioner.repository import OperationRepository
from exomem_provisioner.schemas import FailureResponse, ProvisionRequest, TargetRequest
from exomem_provisioner.worker import ProvisionerWorker

_BEARER = "provisioner-bearer-sentinel-000000000000"


def _credential(offset: int = 0) -> str:
    raw = bytes((index + offset) % 256 for index in range(32))
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


_SERVICE_CREDENTIAL = _credential()
_NEXT_CREDENTIAL = _credential(32)
_RESTORE_REF = "restore-ref-sentinel"
_EXPORT_REF = "export-ref-sentinel"
_RELEASE_REF = "release-ref-sentinel"


def _settings(path: Path) -> ProvisionerSettings:
    return ProvisionerSettings(
        bearer=_BEARER,
        envelope_key="envelope-key-sentinel-00000000000000",
        database_url=f"sqlite+aiosqlite:///{path}",
        database_schema="exomem_provisioner",
        database_role="exomem_provisioner_runtime",
        trusted_proxy_ips="127.0.0.1",
        request_max_bytes=4096,
    )


def _base_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "operationId": "operation-api-alpha",
        "checkpoint": "requested",
        "fenceGeneration": 1,
        "tenantId": "tenant-api-alpha",
        "cellId": "cell-api-alpha",
        "provisionMode": "serve",
        "protocolVersion": "exomem-hosted.v1",
        "releaseVersion": "0.22.0",
        "serviceCredential": _SERVICE_CREDENTIAL,
        "workerPolicy": {"workerCount": 0, "semantic": False, "media": False},
    }
    body.update(overrides)
    return body


def test_provision_request_requires_explicit_bounded_mode() -> None:
    assert ProvisionRequest.model_validate(_base_body()).provisionMode == "serve"
    assert (
        ProvisionRequest.model_validate(_base_body(provisionMode="restore-candidate")).provisionMode
        == "restore-candidate"
    )
    missing = _base_body()
    missing.pop("provisionMode")
    with pytest.raises(ValidationError):
        ProvisionRequest.model_validate(missing)
    with pytest.raises(ValidationError):
        ProvisionRequest.model_validate(_base_body(provisionMode="initialize"))


def _target_body(**overrides: Any) -> dict[str, Any]:
    body = _base_body()
    body.pop("provisionMode")
    body.update(providerRef="provider-cell-api-alpha", **overrides)
    return body


def test_target_request_excludes_provision_only_mode() -> None:
    assert TargetRequest.model_validate(_target_body()).providerRef == "provider-cell-api-alpha"
    with pytest.raises(ValidationError):
        TargetRequest.model_validate(_target_body(provisionMode="serve"))


def _body_for(action: str) -> dict[str, Any]:
    if action == "provision":
        return _base_body()
    if action == "rotate-credential":
        return _target_body(
            phase="stage",
            credentialVersion=2,
            nextCredential=_NEXT_CREDENTIAL,
        )
    if action == "restore":
        return _target_body(
            restoreRef=_RESTORE_REF,
            sourceCellId="cell-source-alpha",
            archiveSha256="a" * 64,
            manifestSha256="b" * 64,
            archiveSize=1024,
        )
    if action == "export":
        return _target_body(
            expiresAt=(datetime.now(UTC) + timedelta(hours=24)).isoformat().replace("+00:00", "Z")
        )
    if action == "export-release":
        return _target_body(releaseRef=_RELEASE_REF)
    if action == "export":
        return _target_body(
            expiresAt=(datetime.now(UTC) + timedelta(days=7))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    if action in {"export-delete", "export-download"}:
        return {
            "operationId": "operation-api-alpha",
            "checkpoint": "requested",
            "fenceGeneration": 1,
            "tenantId": "tenant-api-alpha",
            "exportRef": _EXPORT_REF,
        }
    if action == "destroy":
        return {
            "operationId": "operation-api-alpha",
            "checkpoint": "requested",
            "fenceGeneration": 1,
            "tenantId": "tenant-api-alpha",
        }
    return _target_body()


@pytest.mark.asyncio
async def test_export_requires_canonical_bounded_expiry(
    api: tuple[httpx.AsyncClient, OperationRepository, Path],
) -> None:
    client, _, _ = api
    valid = _body_for("export")
    response = await client.post(
        "/cells/export", headers=_headers("valid-export-expiry"), json=valid
    )
    assert response.status_code == 202
    changed = {
        **valid,
        "expiresAt": (datetime.now(UTC) + timedelta(days=8))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
    conflict = await client.post(
        "/cells/export", headers=_headers("valid-export-expiry"), json=changed
    )
    assert conflict.status_code == 409

    for index, (expires_at, expected_code) in enumerate(
        (
            (None, "PROVISIONER_REJECTED"),
            (
                (datetime.now(UTC) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
                "EXPORT_REQUEST_EXPIRED",
            ),
            (
                (datetime.now(UTC) + timedelta(days=30, seconds=5))
                .isoformat()
                .replace("+00:00", "Z"),
                "PROVISIONER_REJECTED",
            ),
            ((datetime.now(UTC) + timedelta(days=1)).isoformat(), "PROVISIONER_REJECTED"),
        )
    ):
        body = _target_body()
        if expires_at is not None:
            body["expiresAt"] = expires_at
        rejected = await client.post(
            "/cells/export",
            headers=_headers(f"invalid-export-expiry-{index}"),
            json=body,
        )
        assert rejected.status_code == 422
        assert rejected.json() == {"code": expected_code, "retryable": False}


def test_failure_schema_has_exact_content_free_expired_export_code() -> None:
    assert FailureResponse(
        code="EXPORT_REQUEST_EXPIRED",
        retryable=False,
    ).model_dump(mode="json") == {
        "code": "EXPORT_REQUEST_EXPIRED",
        "retryable": False,
    }
    with pytest.raises(ValidationError):
        FailureResponse.model_validate({"code": "EXPORT_EXPIRED", "retryable": False})


@pytest.mark.asyncio
async def test_accepted_export_continues_and_replays_after_expiry_but_new_expired_is_rejected(
    tmp_path: Path,
) -> None:
    path = tmp_path / "timed-api.sqlite"
    settings = _settings(path)
    database = ProvisionerDatabase(settings)
    await database.create_for_tests()
    repository = OperationRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
        claim_seconds=settings.claim_seconds,
    )
    clock = [datetime(2026, 7, 14, 10, 0, tzinfo=UTC)]
    app = create_app(
        settings=settings,
        readiness_probe=database.ready,
        repository=repository,
        provider_identity_codec=ProviderRecoveryIdentityCodec.from_secret("provider-recovery-root"),
        clock=lambda: clock[0],
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://provisioner.test",
    )
    expires_at = datetime(2026, 7, 14, 10, 1, tzinfo=UTC)
    body = _target_body(expiresAt=expires_at.isoformat().replace("+00:00", "Z"))
    result = {
        "exportRef": "export-completed-before-ack",
        "releaseRef": "release-completed-before-ack",
        "archiveSha256": "a" * 64,
        "manifestSha256": "b" * 64,
        "archiveSize": 1024,
        "encryptionScheme": "envelope-aes-256-gcm",
        "integrityVerified": True,
    }
    try:
        accepted = await client.post(
            "/cells/export",
            headers=_headers("lost-export-ack"),
            json=body,
        )
        assert accepted.status_code == 202
        operation = await repository.get("export", "lost-export-ack")
        assert operation is not None

        clock[0] = datetime(2026, 7, 14, 10, 2, tzinfo=UTC)
        pending_replay = await client.post(
            "/cells/export",
            headers=_headers("lost-export-ack"),
            json=body,
        )
        assert pending_replay.status_code == 202
        pending_operation = await repository.get("export", "lost-export-ack")
        assert pending_operation is not None and pending_operation.id == operation.id

        changed_replay = await client.post(
            "/cells/export",
            headers=_headers("lost-export-ack"),
            json={**body, "checkpoint": "changed-after-expiry"},
        )
        assert changed_replay.status_code == 409

        await _complete_as_worker(
            repository,
            operation.id,
            result,
            worker_id="lost-export-ack-worker",
        )

        replayed = await client.post(
            "/cells/export",
            headers=_headers("lost-export-ack"),
            json=body,
        )
        assert replayed.status_code == 200
        assert replayed.json() == result
        completed_operation = await repository.get("export", "lost-export-ack")
        assert completed_operation is not None and completed_operation.id == operation.id

        rejected = await client.post(
            "/cells/export",
            headers=_headers("new-expired-export"),
            json={**body, "operationId": "new-expired-export-operation"},
        )
        assert rejected.status_code == 422
        assert rejected.json() == {"code": "EXPORT_REQUEST_EXPIRED", "retryable": False}
        assert await repository.get("export", "new-expired-export") is None
    finally:
        await client.aclose()
        await database.dispose()


def _headers(idempotency_key: str = "idempotency-api-alpha") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_BEARER}",
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key,
        "X-Exomem-Provisioner-Protocol": PROVISIONER_PROTOCOL,
    }


@pytest.fixture
async def api(tmp_path: Path) -> tuple[httpx.AsyncClient, OperationRepository, Path]:
    path = tmp_path / "api.sqlite"
    settings = _settings(path)
    database = ProvisionerDatabase(settings)
    await database.create_for_tests()
    repository = OperationRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
        claim_seconds=settings.claim_seconds,
    )
    app = create_app(
        settings=settings,
        readiness_probe=database.ready,
        repository=repository,
        provider_identity_codec=ProviderRecoveryIdentityCodec.from_secret("provider-recovery-root"),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://provisioner.test",
    )
    try:
        yield client, repository, path
    finally:
        await client.aclose()
        await database.dispose()


async def _complete_as_worker(
    repository: OperationRepository,
    operation_id: str,
    result: dict[str, Any],
    *,
    worker_id: str,
) -> None:
    claim = await repository.claim_next(worker_id)
    assert claim is not None and claim.id == operation_id and claim.claim_token
    await repository.complete(
        operation_id,
        result,
        worker_id=worker_id,
        claim_token=claim.claim_token,
        claim_generation=claim.claim_generation,
    )


@pytest.mark.asyncio
async def test_api_exposes_exact_fourteen_post_paths_and_strict_pending_union(
    api: tuple[httpx.AsyncClient, OperationRepository, Path],
) -> None:
    client, _, _ = api
    actions = (
        "provision",
        "health",
        "rotate-credential",
        "quiesce",
        "resume",
        "stop",
        "export",
        "export-release",
        "export-delete",
        "restore",
        "export-download",
        "seal",
        "discard",
        "destroy",
    )

    actual = {
        route.path.removeprefix("/cells/")
        for route in client._transport.app.routes  # type: ignore[attr-defined]
        if route.path.startswith("/cells/")
    }
    assert actual == set(actions)

    for index, action in enumerate(actions, start=1):
        body = _body_for(action)
        body["operationId"] = f"operation-{index}"
        body["fenceGeneration"] = index
        response = await client.post(
            f"/cells/{action}",
            headers=_headers(f"idempotency-{index}"),
            content=json.dumps(body),
        )
        assert response.status_code == 202, (action, response.text)
        assert response.headers["retry-after"] == "2"
        assert response.json() == {
            "status": "pending",
            "operationId": f"operation-{index}",
            "checkpoint": "requested",
            "retryAfterSeconds": 2,
        }


@pytest.mark.asyncio
async def test_api_seals_distinct_provider_recovery_envelopes_before_queueing(
    api: tuple[httpx.AsyncClient, OperationRepository, Path],
) -> None:
    client, repository, _ = api
    response = await client.post(
        "/cells/provision",
        headers=_headers("provider-recovery-envelopes"),
        json=_base_body(),
    )
    assert response.status_code == 202
    operation = await repository.get("provision", "provider-recovery-envelopes")
    assert operation is not None
    queued = await repository.load_request(operation.id)
    envelopes = queued["_providerRecoveryEnvelopes"]
    assert len(envelopes) == 16
    assert len(set(envelopes.values())) == 16
    assert all(value.startswith("eyJ") for value in envelopes.values())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "scheme", "expected_status"),
    [
        ({}, "https", 401),
        ({"Authorization": "Bearer wrong"}, "https", 401),
        ({"X-Exomem-Provisioner-Protocol": "wrong"}, "https", 400),
        ({"Content-Type": "text/plain"}, "https", 415),
        ({"Idempotency-Key": "bad key with spaces"}, "https", 400),
        ({}, "http", 400),
    ],
)
async def test_auth_protocol_transport_and_idempotency_fail_before_mutation(
    api: tuple[httpx.AsyncClient, OperationRepository, Path],
    headers: dict[str, str],
    scheme: str,
    expected_status: int,
) -> None:
    client, repository, _ = api
    request_headers = _headers()
    request_headers.update(headers)
    if "Authorization" in headers and headers["Authorization"] == "":
        request_headers.pop("Authorization")
    if headers == {} and scheme == "https":
        request_headers.pop("Authorization")
    transport = client._transport  # type: ignore[attr-defined]
    selected = httpx.AsyncClient(transport=transport, base_url=f"{scheme}://provisioner.test")
    try:
        response = await selected.post(
            "/cells/provision",
            headers=request_headers,
            content=json.dumps(_base_body()),
        )
    finally:
        await selected.aclose()

    assert response.status_code == expected_status
    assert response.json() == {"code": "PROVISIONER_REJECTED", "retryable": False}
    assert await repository.get("provision", "idempotency-api-alpha") is None


@pytest.mark.asyncio
async def test_unknown_invalid_oversize_and_trailing_slash_requests_are_content_free(
    api: tuple[httpx.AsyncClient, OperationRepository, Path],
) -> None:
    client, repository, _ = api
    unknown = await client.post(
        "/cells/provision",
        headers=_headers("unknown-field"),
        json={**_base_body(), "email": "person@example.invalid"},
    )
    invalid = await client.post(
        "/cells/provision",
        headers=_headers("invalid-id"),
        json=_base_body(cellId="bad cell id"),
    )
    oversized = await client.post(
        "/cells/provision",
        headers=_headers("oversized"),
        content=json.dumps({**_base_body(), "padding": "x" * 5000}),
    )
    redirect = await client.post(
        "/cells/provision/",
        headers=_headers("slash"),
        json=_base_body(),
        follow_redirects=False,
    )

    assert unknown.status_code == invalid.status_code == 422
    assert (
        unknown.json()
        == invalid.json()
        == {
            "code": "PROVISIONER_REJECTED",
            "retryable": False,
        }
    )
    assert "person@example.invalid" not in unknown.text
    assert "bad cell id" not in invalid.text
    assert oversized.status_code == 413
    assert oversized.json() == {"code": "PROVISIONER_REJECTED", "retryable": False}
    assert redirect.status_code == 404
    assert "location" not in redirect.headers
    for action, key in (
        ("provision", "unknown-field"),
        ("provision", "invalid-id"),
        ("provision", "oversized"),
        ("provision", "slash"),
    ):
        assert await repository.get(action, key) is None


@pytest.mark.asyncio
async def test_provider_identity_ids_fit_hcloud_recovery_labels(
    api: tuple[httpx.AsyncClient, OperationRepository, Path],
) -> None:
    client, repository, _ = api
    response = await client.post(
        "/cells/provision",
        headers=_headers("identifier-too-long"),
        json=_base_body(cellId="c" * 65),
    )

    assert response.status_code == 422
    assert response.json() == {"code": "PROVISIONER_REJECTED", "retryable": False}
    assert await repository.get("provision", "identifier-too-long") is None


@pytest.mark.asyncio
async def test_export_requires_future_rfc3339_expiry_no_more_than_thirty_days(
    api: tuple[httpx.AsyncClient, OperationRepository, Path],
) -> None:
    client, repository, _ = api
    missing = await client.post(
        "/cells/export",
        headers=_headers("export-missing-expiry"),
        json=_target_body(),
    )
    too_late = await client.post(
        "/cells/export",
        headers=_headers("export-long-expiry"),
        json=_target_body(
            expiresAt=(datetime.now(UTC) + timedelta(days=31)).isoformat().replace("+00:00", "Z")
        ),
    )

    assert missing.status_code == too_late.status_code == 422
    assert await repository.get("export", "export-missing-expiry") is None
    assert await repository.get("export", "export-long-expiry") is None


@pytest.mark.asyncio
async def test_streaming_body_limit_stops_reading_after_first_oversized_chunk(
    api: tuple[httpx.AsyncClient, OperationRepository, Path],
) -> None:
    client, repository, _ = api

    class CountingStream(httpx.AsyncByteStream):
        emitted = 0

        async def __aiter__(self):
            for _ in range(4):
                self.emitted += 1
                yield b"x" * 2048

    stream = CountingStream()
    response = await client.post(
        "/cells/provision",
        headers=_headers("stream-limit"),
        content=stream,
    )

    assert response.status_code == 413
    assert stream.emitted == 3
    assert await repository.get("provision", "stream-limit") is None


@pytest.mark.asyncio
async def test_forwarded_https_header_is_not_trusted_by_application_layer(
    api: tuple[httpx.AsyncClient, OperationRepository, Path],
) -> None:
    client, repository, _ = api
    forged = httpx.AsyncClient(
        transport=client._transport,  # type: ignore[attr-defined]
        base_url="http://provisioner.test",
    )
    try:
        response = await forged.post(
            "/cells/provision",
            headers={**_headers("forged-proto"), "X-Forwarded-Proto": "https"},
            json=_base_body(),
        )
    finally:
        await forged.aclose()

    assert response.status_code == 400
    assert await repository.get("provision", "forged-proto") is None


@pytest.mark.asyncio
async def test_replay_returns_exact_encrypted_final_proof_and_changed_body_conflicts(
    api: tuple[httpx.AsyncClient, OperationRepository, Path],
) -> None:
    client, repository, database_path = api
    body = _base_body()
    pending = await client.post("/cells/provision", headers=_headers(), json=body)
    operation = await repository.get("provision", "idempotency-api-alpha")
    assert pending.status_code == 202 and operation is not None
    final = {
        "providerRef": "provider-cell-api-alpha",
        "privateEndpoint": "https://cell-api-alpha.cells.internal",
    }
    await _complete_as_worker(repository, operation.id, final, worker_id="final-proof-worker")

    replay = await client.post("/cells/provision", headers=_headers(), json=body)
    conflict = await client.post(
        "/cells/provision",
        headers=_headers(),
        json={**body, "serviceCredential": _NEXT_CREDENTIAL},
    )

    assert replay.status_code == 200
    assert replay.json() == final
    assert conflict.status_code == 409
    assert conflict.json() == {
        "code": "CONTROL_PLANE_STATE_CONFLICT",
        "retryable": False,
    }
    database_bytes = database_path.read_bytes()
    for forbidden in (
        _SERVICE_CREDENTIAL,
        _NEXT_CREDENTIAL,
        final["privateEndpoint"],
        _BEARER,
    ):
        assert forbidden.encode() not in database_bytes
        assert forbidden not in replay.headers.get("server", "")


@pytest.mark.asyncio
async def test_pending_replay_echoes_immutable_caller_checkpoint_not_worker_progress(
    api: tuple[httpx.AsyncClient, OperationRepository, Path],
) -> None:
    client, repository, _ = api
    body = _body_for("restore")
    body["checkpoint"] = "caller-waiting"
    headers = _headers("checkpoint-separation")
    first = await client.post("/cells/restore", headers=headers, json=body)
    driver = FakeDriver()
    driver.remain_pending("restore", polls=1)
    worker = ProvisionerWorker(repository, driver, worker_id="checkpoint-worker")

    assert first.status_code == 202
    assert await worker.run_once() is True
    internal = await repository.get("restore", "checkpoint-separation")
    replay = await client.post("/cells/restore", headers=headers, json=body)

    assert internal is not None and internal.checkpoint == "provider-wait"
    assert replay.status_code == 202
    assert replay.json()["checkpoint"] == "caller-waiting"


@pytest.mark.asyncio
async def test_final_void_and_proof_responses_are_strictly_validated(
    api: tuple[httpx.AsyncClient, OperationRepository, Path],
) -> None:
    client, repository, _ = api
    stop_body = _body_for("stop")
    await client.post("/cells/stop", headers=_headers("stop-key"), json=stop_body)
    stop_operation = await repository.get("stop", "stop-key")
    assert stop_operation is not None
    await _complete_as_worker(repository, stop_operation.id, {}, worker_id="stop-worker")
    stopped = await client.post("/cells/stop", headers=_headers("stop-key"), json=stop_body)
    assert stopped.status_code == 204
    assert stopped.content == b""

    discard_body = _body_for("discard")
    await client.post("/cells/discard", headers=_headers("discard-key"), json=discard_body)
    discard_operation = await repository.get("discard", "discard-key")
    assert discard_operation is not None
    await _complete_as_worker(
        repository,
        discard_operation.id,
        {"computeDestroyed": True, "storageDestroyed": True, "keysDestroyed": False},
        worker_id="discard-worker",
    )
    invalid = await client.post(
        "/cells/discard", headers=_headers("discard-key"), json=discard_body
    )
    assert invalid.status_code == 500
    assert invalid.json() == {
        "code": "PROVISIONER_RESPONSE_INVALID",
        "retryable": False,
    }

    rotate_body = _target_body(
        phase="finalize",
        credentialVersion=2,
        nextCredential=_NEXT_CREDENTIAL,
    )
    await client.post(
        "/cells/rotate-credential",
        headers=_headers("rotate-finalize-key"),
        json=rotate_body,
    )
    rotate_operation = await repository.get("rotate-credential", "rotate-finalize-key")
    assert rotate_operation is not None
    await _complete_as_worker(
        repository,
        rotate_operation.id,
        {"previousCredentialRejected": False},
        worker_id="rotate-worker",
    )
    invalid_rotation = await client.post(
        "/cells/rotate-credential",
        headers=_headers("rotate-finalize-key"),
        json=rotate_body,
    )
    assert invalid_rotation.status_code == 500
    assert invalid_rotation.json()["code"] == "PROVISIONER_RESPONSE_INVALID"


@pytest.mark.asyncio
async def test_fake_worker_produces_client_compatible_final_union_for_every_action(
    api: tuple[httpx.AsyncClient, OperationRepository, Path],
) -> None:
    client, repository, _ = api
    driver = FakeDriver()
    worker = ProvisionerWorker(repository, driver, worker_id="api-integration-worker")
    final_fields: dict[str, set[str]] = {
        "provision": {"providerRef", "privateEndpoint"},
        "health": {
            "live",
            "ready",
            "cellId",
            "protocolVersion",
            "releaseVersion",
            "serviceAuthenticated",
            "mutationAuthority",
            "readAdmission",
            "writeAdmission",
            "workerPolicy",
            "code",
        },
        "rotate-credential": {"previousCredentialRejected"},
        "export": {
            "exportRef",
            "releaseRef",
            "archiveSha256",
            "manifestSha256",
            "archiveSize",
            "encryptionScheme",
            "integrityVerified",
        },
        "export-delete": {"objectDestroyed"},
        "export-download": {"url", "expiresAt"},
        "discard": {"computeDestroyed", "storageDestroyed", "keysDestroyed"},
        "destroy": {
            "computeDestroyed",
            "storageDestroyed",
            "keysDestroyed",
            "tenantResourcesDestroyed",
        },
    }
    void_actions = {"quiesce", "resume", "stop", "export-release", "restore", "seal"}
    actions = (
        "provision",
        "health",
        "rotate-credential",
        "quiesce",
        "resume",
        "stop",
        "export",
        "export-release",
        "export-delete",
        "restore",
        "export-download",
        "seal",
        "discard",
        "destroy",
    )

    for index, action in enumerate(actions, start=1):
        body = _body_for(action)
        body["operationId"] = f"integration-operation-{index}"
        body["fenceGeneration"] = index
        headers = _headers(f"integration-key-{index}")
        pending = await client.post(f"/cells/{action}", headers=headers, json=body)
        assert pending.status_code == 202
        assert await worker.run_once() is True

        final = await client.post(f"/cells/{action}", headers=headers, json=body)
        if action in void_actions:
            assert final.status_code == 204, (action, final.text)
            assert final.content == b""
        else:
            assert final.status_code == 200, (action, final.text)
            assert set(final.json()) == final_fields[action]
            assert "status" not in final.json()


@pytest.mark.asyncio
async def test_unexpected_exception_response_and_repr_do_not_expose_request_secrets(
    api: tuple[httpx.AsyncClient, OperationRepository, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, repository, _ = api

    async def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(f"driver failed with {_SERVICE_CREDENTIAL} and person@example.invalid")

    monkeypatch.setattr(repository, "submit", explode)
    safe_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=client._transport.app,  # type: ignore[attr-defined]
            raise_app_exceptions=False,
        ),
        base_url="https://provisioner.test",
    )
    try:
        response = await safe_client.post(
            "/cells/provision",
            headers=_headers("exception-redaction"),
            json=_base_body(),
        )
    finally:
        await safe_client.aclose()

    assert response.status_code == 500
    assert response.json() == {"code": "PROVISIONER_UNAVAILABLE", "retryable": True}
    assert _SERVICE_CREDENTIAL not in response.text
    assert "person@example.invalid" not in response.text
