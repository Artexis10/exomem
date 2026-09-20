from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from dataclasses import replace

import httpx
import pytest
from test_governance_migration_membership import NOW, TARGET, _identity, _run, _source

from exomem_provisioner.activation_ack_api import ActivationAckService, create_activation_ack_app
from exomem_provisioner.adapters import AuthorizationSessionSecretSnapshot
from exomem_provisioner.authorization_membership import transition_hosted_authorization_bundle
from exomem_provisioner.conflict_reason import ConflictReason
from exomem_provisioner.hosted_activation_ack_protocol import PROTOCOL, encode_message
from exomem_provisioner.lifecycle import MetadataConflict, OpaqueProviderMetadata
from exomem_provisioner.repository import ActivationAckCellSnapshot, RepositoryConflict

REQUEST_ID = "1" * 64
CELL_ID = "cell-alpha"
TENANT_ID = "tenant-alpha"
RECOVERY_ENVELOPE = str(_identity(4)["expected_recovery_envelope"])
METADATA = OpaqueProviderMetadata(TENANT_ID, CELL_ID, "operation-alpha", 7)


class _BlockingRequestStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        await self.release.wait()
        yield self.content


def _credential(offset: int = 0) -> str:
    raw = bytes((index + offset) % 256 for index in range(32))
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_b64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _serving_bundle():
    migrated = _run(_source(3, enrolled=True), "commit")
    return transition_hosted_authorization_bundle(
        migrated.files,
        **_identity(4),
        target_state="SERVING",
        target_no_in_flight=False,
        now=NOW + 4,
    )


def _publication(*, predecessor_epoch: int | None = None) -> dict[str, object]:
    epoch = TARGET["activation_epoch"] if predecessor_epoch is None else predecessor_epoch
    return {
        "publication_event_id": "publication-alpha",
        "predecessor": {
            "activation_store_id": TARGET["activation_store_id"],
            "activation_epoch": epoch,
            "activation_state_digest": TARGET["activation_state_digest"],
        },
        "successor": {
            "activation_store_id": TARGET["activation_store_id"],
            "activation_epoch": epoch + 1,
            "activation_state_digest": "f" * 64,
        },
    }


def _headers(
    *,
    credential: str | None = None,
    version: str = "1",
    cell_id: str = CELL_ID,
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {credential or _credential()}",
        "X-Exomem-Cell-Id": cell_id,
        "X-Exomem-Credential-Version": version,
        "X-Exomem-Activation-Protocol": PROTOCOL,
        "X-Exomem-Request-Id": REQUEST_ID,
    }


def _ack_body(publication: dict[str, object] | None = None, *, budget_ms: int = 3500):
    return {
        "protocol": PROTOCOL,
        "request_id": REQUEST_ID,
        "budget_ms": budget_ms,
        "publication": publication or _publication(),
    }


class _Lookup:
    def __init__(self) -> None:
        self.available = True
        self.calls = 0

    async def lookup_activation_ack_cell(self, cell_id: str) -> ActivationAckCellSnapshot:
        self.calls += 1
        if not self.available or cell_id != CELL_ID:
            raise RepositoryConflict("activation acknowledgement lifecycle state is unavailable")
        return ActivationAckCellSnapshot(metadata=METADATA)


class _Cell:
    def __init__(self) -> None:
        self.credentials = {"1": _credential()}
        self.bundle = _serving_bundle()
        self.recovery_envelope = RECOVERY_ENVELOPE
        self.writes = 0
        self.conflicts = 0

    async def read_credential_bundle_authority(self, metadata: OpaqueProviderMetadata):
        assert metadata == METADATA
        return dict(self.credentials), {"provider": "authenticated"}

    async def read_authorization_session_authority(self, metadata: OpaqueProviderMetadata):
        assert metadata == METADATA
        return AuthorizationSessionSecretSnapshot(
            files=self.bundle.files,
            recovery_envelope=self.recovery_envelope,
            revision=self.bundle.revision,
            resource_version=str(self.writes + 1),
        )

    async def write_authorization_session_bundle(
        self,
        metadata: OpaqueProviderMetadata,
        files: dict[str, bytes],
        *,
        recovery_envelope: str,
        membership_epoch: int,
        membership_digest: str,
        revision: str,
        expected_revision: str,
        effect_guard,
    ) -> None:
        del membership_epoch, membership_digest
        assert metadata == METADATA
        assert recovery_envelope == self.recovery_envelope
        await effect_guard()
        if self.conflicts:
            self.conflicts -= 1
            renewed = transition_hosted_authorization_bundle(
                self.bundle.files,
                **_identity(4),
                target_state="SERVING",
                target_no_in_flight=False,
                renew=True,
                now=NOW + 5,
            )
            self.bundle = renewed
            raise MetadataConflict(
                "authorization session bundle changed concurrently",
                reason=ConflictReason.AUTHORIZATION_SESSION_BUNDLE_CHANGED_CONCURRENTLY,
            )
        if expected_revision != self.bundle.revision:
            raise MetadataConflict(
                "authorization session bundle changed concurrently",
                reason=ConflictReason.AUTHORIZATION_SESSION_BUNDLE_CHANGED_CONCURRENTLY,
            )
        self.bundle = replace(
            self.bundle,
            keyring=files["keyring.json"],
            control=files["control.json"],
            membership=files["serving-membership.json"],
            revision=revision,
            activation_epoch=_publication()["successor"]["activation_epoch"],
            activation_state_digest=_publication()["successor"]["activation_state_digest"],
        )
        self.writes += 1


class _Runtime:
    def __init__(self, cell: _Cell) -> None:
        self.cell = cell
        self.calls: list[dict[str, object]] = []
        self.mode = "valid"
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

    async def activation_proof(self, metadata: OpaqueProviderMetadata, **kwargs: object):
        assert metadata == METADATA
        request = dict(kwargs["request"])
        self.calls.append(request)
        if self.mode == "blocked":
            self.started.set()
            await self.release.wait()
        keyring = json.loads(self.cell.bundle.keyring)
        entry = keyring["accepted_keys"][0]
        response = {
            **request,
            "publication_evidence": {
                "component_kind": "hosted-mutation-child/v1",
                "component_sha256": "a" * 64,
            },
            "signing_key_id": entry["key_id"],
        }
        if self.mode == "stale":
            response["expires_at"] = int(response["issued_at"]) - 1
        canonical = json.dumps(
            response,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        key = base64.urlsafe_b64decode(entry["key"] + "=")
        response["mac"] = hmac.new(
            key,
            b"exomem.hosted-activation-proof/v1\0" + canonical,
            hashlib.sha256,
        ).hexdigest()
        if self.mode == "forged":
            response["mac"] = "0" * 64
        if self.mode == "rotate":
            self.cell.credentials = {"2": _credential(2)}
        if self.mode == "error":
            raise RuntimeError(f"private callback failed with {entry['key']}")
        self.finished.set()
        return response


def _client(
    cell: _Cell | None = None,
    lookup: _Lookup | None = None,
    *,
    clock=lambda: NOW + 5,
    monotonic=None,
):
    cell = cell or _Cell()
    lookup = lookup or _Lookup()
    runtime = _Runtime(cell)
    challenge_counter = 0

    def challenge_bytes(size: int) -> bytes:
        nonlocal challenge_counter
        challenge_counter += 1
        return bytes([challenge_counter]) * size

    service_kwargs = {}
    if monotonic is not None:
        service_kwargs["monotonic"] = monotonic
    service = ActivationAckService(
        cell_lookup=lookup,
        cell=cell,
        runtime=runtime,
        runtime_release="0.48.0",
        runtime_protocol_version="1",
        clock=clock,
        challenge_bytes=challenge_bytes,
        **service_kwargs,
    )
    app = create_activation_ack_app(service)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://activation-ack.test",
    )
    return client, cell, lookup, runtime


async def test_current_returns_only_nonsecret_signed_custody() -> None:
    client, cell, _lookup, _runtime = _client()
    async with client:
        response = await client.get("/cell-runtime/v1/activation/current", headers=_headers())

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["outcome"] == "current"
    assert body["bundle_revision"] == cell.bundle.revision
    assert _decode_b64(body["control_b64"]) == cell.bundle.control
    assert _decode_b64(body["serving_membership_b64"]) == cell.bundle.membership
    serialized = response.content.decode("utf-8")
    assert '"keyring":' not in serialized
    assert _credential() not in serialized
    assert RECOVERY_ENVELOPE not in serialized


async def test_current_returns_fresh_draining_custody_but_ack_refuses_it() -> None:
    cell = _Cell()
    cell.bundle = transition_hosted_authorization_bundle(
        cell.bundle.files,
        **_identity(4),
        target_state="DRAINING",
        target_no_in_flight=True,
        now=NOW + 5,
    )
    client, _cell, _lookup, runtime = _client(cell)
    async with client:
        current = await client.get(
            "/cell-runtime/v1/activation/current",
            headers=_headers(),
        )
        acknowledgement = await client.post(
            "/cell-runtime/v1/activation/ack",
            headers={**_headers(), "Content-Type": "application/json"},
            content=encode_message(_ack_body(), "httpAckRequest"),
        )

    assert current.status_code == 200
    assert current.json()["outcome"] == "current"
    assert acknowledgement.status_code == 409
    assert acknowledgement.json()["code"] == "ACTIVATION_CONFLICT"
    assert runtime.calls == []


async def test_acknowledgement_advances_once_and_exact_replay_is_unchanged() -> None:
    client, cell, _lookup, runtime = _client()
    original_membership = cell.bundle.membership
    async with client:
        first = await client.post(
            "/cell-runtime/v1/activation/ack",
            headers={**_headers(), "Content-Type": "application/json"},
            content=encode_message(_ack_body(), "httpAckRequest"),
        )
        replay = await client.post(
            "/cell-runtime/v1/activation/ack",
            headers={**_headers(), "Content-Type": "application/json"},
            content=encode_message(_ack_body(), "httpAckRequest"),
        )

    assert first.status_code == replay.status_code == 200
    assert first.json()["outcome"] == "advanced"
    assert replay.json()["outcome"] == "unchanged"
    assert cell.writes == 1
    assert cell.bundle.membership == original_membership
    assert len(runtime.calls) == 2


@pytest.mark.parametrize(
    "version,credential",
    [("2", _credential()), ("1", _credential(1))],
)
async def test_wrong_or_rotated_credential_is_refused(version: str, credential: str) -> None:
    client, _cell, _lookup, runtime = _client()
    async with client:
        response = await client.get(
            "/cell-runtime/v1/activation/current",
            headers=_headers(version=version, credential=credential),
        )

    assert response.status_code == 401
    assert response.json()["code"] == "AUTHENTICATION_FAILED"
    assert runtime.calls == []


@pytest.mark.parametrize("mode", ["forged", "stale"])
async def test_forged_or_stale_committed_proof_is_refused(mode: str) -> None:
    client, cell, _lookup, runtime = _client()
    runtime.mode = mode
    async with client:
        response = await client.post(
            "/cell-runtime/v1/activation/ack",
            headers={**_headers(), "Content-Type": "application/json"},
            content=encode_message(_ack_body(), "httpAckRequest"),
        )

    assert response.status_code == 409
    assert response.json()["code"] == "ACTIVATION_CONFLICT"
    assert cell.writes == 0


async def test_foreign_publication_and_expired_authority_are_refused_before_effects() -> None:
    cell = _Cell()
    client, cell, _lookup, runtime = _client(cell)
    foreign = _publication()
    foreign["successor"] = {**foreign["successor"], "activation_store_id": "foreign-store"}
    async with client:
        foreign_response = await client.post(
            "/cell-runtime/v1/activation/ack",
            headers={**_headers(), "Content-Type": "application/json"},
            content=encode_message(_ack_body(foreign), "httpAckRequest"),
        )
    expired_client, _cell, _lookup, _runtime = _client(
        cell,
        clock=lambda: cell.bundle.expires_at,
    )
    async with expired_client:
        expired_response = await expired_client.post(
            "/cell-runtime/v1/activation/ack",
            headers={**_headers(), "Content-Type": "application/json"},
            content=encode_message(_ack_body(), "httpAckRequest"),
        )

    assert foreign_response.status_code == expired_response.status_code == 409
    assert cell.writes == 0
    assert runtime.calls == []


async def test_one_cas_conflict_retries_with_fresh_proof_and_preserves_renewal() -> None:
    cell = _Cell()
    cell.conflicts = 1
    client, cell, _lookup, runtime = _client(cell)
    async with client:
        response = await client.post(
            "/cell-runtime/v1/activation/ack",
            headers={**_headers(), "Content-Type": "application/json"},
            content=encode_message(_ack_body(), "httpAckRequest"),
        )

    assert response.status_code == 200
    assert cell.writes == 1
    assert len(runtime.calls) == 2
    assert runtime.calls[0]["challenge_id"] != runtime.calls[1]["challenge_id"]
    assert (
        runtime.calls[0]["expected_bundle_revision"] != runtime.calls[1]["expected_bundle_revision"]
    )
    assert response.json()["bundle_revision"] == cell.bundle.revision


async def test_credential_rotation_during_proof_refuses_before_secret_cas() -> None:
    client, cell, _lookup, runtime = _client()
    runtime.mode = "rotate"
    async with client:
        response = await client.post(
            "/cell-runtime/v1/activation/ack",
            headers={**_headers(), "Content-Type": "application/json"},
            content=encode_message(_ack_body(), "httpAckRequest"),
        )

    assert response.status_code == 401
    assert response.json()["code"] == "AUTHENTICATION_FAILED"
    assert cell.writes == 0


async def test_proof_challenge_expiry_is_bounded_by_remaining_request_budget() -> None:
    client, _cell, _lookup, runtime = _client()
    async with client:
        response = await client.post(
            "/cell-runtime/v1/activation/ack",
            headers={**_headers(), "Content-Type": "application/json"},
            content=encode_message(_ack_body(budget_ms=1500), "httpAckRequest"),
        )

    assert response.status_code == 200
    assert runtime.calls[0]["expires_at"] == runtime.calls[0]["issued_at"] + 1


async def test_callback_failure_never_exposes_key_or_credential() -> None:
    client, cell, _lookup, runtime = _client()
    runtime.mode = "error"
    signing_key = json.loads(cell.bundle.keyring)["accepted_keys"][0]["key"]
    async with client:
        response = await client.post(
            "/cell-runtime/v1/activation/ack",
            headers={**_headers(), "Content-Type": "application/json"},
            content=encode_message(_ack_body(), "httpAckRequest"),
        )

    assert response.status_code == 503
    assert response.json()["code"] == "ACK_UNAVAILABLE"
    assert signing_key not in response.text
    assert _credential() not in response.text
    assert cell.writes == 0


async def test_malformed_request_is_bounded_and_does_not_echo_input() -> None:
    client, _cell, _lookup, _runtime = _client()
    secret = "request-private-sentinel"
    async with client:
        response = await client.post(
            "/cell-runtime/v1/activation/ack",
            headers={**_headers(), "Content-Type": "application/json"},
            content=json.dumps({**_ack_body(), "unknown": secret}).encode(),
        )

    assert response.status_code == 400
    assert response.json() == {
        "code": "MALFORMED_REQUEST",
        "protocol": PROTOCOL,
        "request_id": REQUEST_ID,
        "retry_after_ms": 0,
    }
    assert secret not in response.text


@pytest.mark.parametrize(
    ("headers", "content"),
    [
        ({**_headers(), "X-Oversized": "x" * (8 * 1024)}, b""),
        (
            {**_headers(), "Content-Type": "application/json"},
            b"{" + b" " * (8 * 1024) + b"}",
        ),
    ],
)
async def test_request_header_and_body_budgets_are_fail_closed(
    headers: dict[str, str], content: bytes
) -> None:
    client, _cell, _lookup, runtime = _client()
    async with client:
        response = await client.post(
            "/cell-runtime/v1/activation/ack",
            headers=headers,
            content=content,
        )

    assert response.status_code == 400
    assert response.json()["code"] == "MALFORMED_REQUEST"
    assert runtime.calls == []


async def test_deadline_keeps_cell_slot_until_blocking_callback_finishes() -> None:
    monotonic_now = 0.0
    client, cell, _lookup, runtime = _client(monotonic=lambda: monotonic_now)
    runtime.mode = "blocked"
    async with client:
        pending = asyncio.create_task(
            client.post(
                "/cell-runtime/v1/activation/ack",
                headers={**_headers(), "Content-Type": "application/json"},
                content=encode_message(_ack_body(budget_ms=1500), "httpAckRequest"),
            )
        )
        await runtime.started.wait()
        capacity = await client.get(
            "/cell-runtime/v1/activation/current",
            headers=_headers(),
        )
        assert not pending.done()
        monotonic_now = 2.0
        runtime.release.set()
        deadline = await pending

    assert capacity.status_code == 429
    assert deadline.status_code == 503
    assert deadline.json()["code"] == "ACK_DEADLINE_EXCEEDED"
    assert cell.writes == 0


async def test_slow_body_is_admitted_before_receive_and_retains_cell_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "exomem_provisioner.activation_ack_api._RECEIVE_TIMEOUT_SECONDS",
        0.05,
    )
    client, _cell, lookup, runtime = _client()
    body = _BlockingRequestStream(encode_message(_ack_body(), "httpAckRequest"))
    async with client:
        pending = asyncio.create_task(
            client.post(
                "/cell-runtime/v1/activation/ack",
                headers={**_headers(), "Content-Type": "application/json"},
                content=body,
            )
        )
        await body.started.wait()
        excess = await asyncio.gather(
            *(
                client.post(
                    "/cell-runtime/v1/activation/ack",
                    headers={**_headers(), "Content-Type": "application/json"},
                    content=encode_message(_ack_body(), "httpAckRequest"),
                )
                for _ in range(8)
            )
        )
        timeout = await asyncio.wait_for(pending, timeout=0.2)
        still_occupied = await client.get(
            "/cell-runtime/v1/activation/current",
            headers=_headers(),
        )
        app = client._transport.app  # type: ignore[attr-defined]
        drain = asyncio.create_task(app.state.activation_ack_drain())
        await asyncio.sleep(0)
        assert not drain.done()
        body.release.set()
        await asyncio.wait_for(drain, timeout=0.2)

    assert timeout.status_code == 503
    assert timeout.json()["code"] == "ACK_DEADLINE_EXCEEDED"
    assert all(response.status_code == 429 for response in excess)
    assert still_occupied.status_code == 429
    assert lookup.calls == 0
    assert runtime.calls == []


async def test_authority_timeout_returns_but_retains_slot_until_native_read_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "exomem_provisioner.activation_ack_api._PROCESS_TIMEOUT_SECONDS",
        0.05,
    )

    class BlockingAuthorityCell(_Cell):
        def __init__(self) -> None:
            super().__init__()
            self.authority_started = asyncio.Event()
            self.authority_release = asyncio.Event()

        async def read_credential_bundle_authority(self, metadata: OpaqueProviderMetadata):
            self.authority_started.set()
            await self.authority_release.wait()
            return await super().read_credential_bundle_authority(metadata)

    cell = BlockingAuthorityCell()
    client, _cell, _lookup, _runtime = _client(cell)
    async with client:
        pending = asyncio.create_task(
            client.get(
                "/cell-runtime/v1/activation/current",
                headers=_headers(),
            )
        )
        await cell.authority_started.wait()
        timeout = await asyncio.wait_for(pending, timeout=0.2)
        occupied = await client.get(
            "/cell-runtime/v1/activation/current",
            headers=_headers(),
        )
        cell.authority_release.set()
        for _ in range(20):
            ready = await client.get(
                "/cell-runtime/v1/activation/current",
                headers=_headers(),
            )
            if ready.status_code == 200:
                break
            await asyncio.sleep(0)

    assert timeout.status_code == 503
    assert timeout.json()["code"] == "ACK_DEADLINE_EXCEEDED"
    assert occupied.status_code == 429
    assert ready.status_code == 200


@pytest.mark.parametrize(("elapsed", "expected_status"), [(3.1, 409), (4.0, 503)])
async def test_final_effect_guard_refuses_expired_proof_or_request_deadline(
    elapsed: float,
    expected_status: int,
) -> None:
    wall_now = float(NOW + 5)
    monotonic_now = 0.0

    class DelayedPredecessorCell(_Cell):
        async def write_authorization_session_bundle(self, *args: object, **kwargs: object) -> None:
            nonlocal wall_now, monotonic_now
            wall_now += elapsed
            monotonic_now += elapsed
            await super().write_authorization_session_bundle(*args, **kwargs)

    cell = DelayedPredecessorCell()
    client, cell, _lookup, _runtime = _client(
        cell,
        clock=lambda: wall_now,
        monotonic=lambda: monotonic_now,
    )
    async with client:
        response = await client.post(
            "/cell-runtime/v1/activation/ack",
            headers={**_headers(), "Content-Type": "application/json"},
            content=encode_message(_ack_body(budget_ms=3500), "httpAckRequest"),
        )

    assert response.status_code == expected_status
    assert cell.writes == 0


async def test_cancelled_request_keeps_cell_slot_until_blocking_callback_finishes() -> None:
    client, _cell, _lookup, runtime = _client()
    runtime.mode = "blocked"
    async with client:
        pending = asyncio.create_task(
            client.post(
                "/cell-runtime/v1/activation/ack",
                headers={**_headers(), "Content-Type": "application/json"},
                content=encode_message(_ack_body(), "httpAckRequest"),
            )
        )
        await runtime.started.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        capacity = await client.get(
            "/cell-runtime/v1/activation/current",
            headers=_headers(),
        )
        runtime.release.set()
        await runtime.finished.wait()
        for _ in range(20):
            ready = await client.get(
                "/cell-runtime/v1/activation/current",
                headers=_headers(),
            )
            if ready.status_code == 200:
                break
            await asyncio.sleep(0)

    assert capacity.status_code == 429
    assert ready.status_code == 200


async def test_global_eight_exchange_limit_refuses_without_waiting() -> None:
    class BlockingCurrentService:
        def __init__(self) -> None:
            self.active = 0
            self.full = asyncio.Event()
            self.release = asyncio.Event()

        async def current(self, *, cell_id: str, request_id: str, **_kwargs: object):
            self.active += 1
            if self.active == 8:
                self.full.set()
            await self.release.wait()
            return {
                "protocol": PROTOCOL,
                "request_id": request_id,
                "outcome": "current",
                "cell_id": cell_id,
                "logical_vault_id": TENANT_ID,
                "registry_attachment_id": "attachment-alpha",
                "attachment_epoch": 1,
                "bundle_revision": "a" * 64,
                "keyring_sha256": "b" * 64,
                "control_b64": "eA",
                "serving_membership_b64": "eA",
            }

    service = BlockingCurrentService()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_activation_ack_app(service)),  # type: ignore[arg-type]
        base_url="https://activation-ack.test",
    )
    async with client:
        active = [
            asyncio.create_task(
                client.get(
                    "/cell-runtime/v1/activation/current",
                    headers=_headers(cell_id=f"cell-{index}"),
                )
            )
            for index in range(8)
        ]
        await service.full.wait()
        excess = await asyncio.wait_for(
            client.get(
                "/cell-runtime/v1/activation/current",
                headers=_headers(cell_id="cell-excess"),
            ),
            timeout=0.1,
        )
        service.release.set()
        responses = await asyncio.gather(*active)

    assert excess.status_code == 429
    assert excess.json()["code"] == "ACK_CAPACITY_EXCEEDED"
    assert all(response.status_code == 200 for response in responses)
