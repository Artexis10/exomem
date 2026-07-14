from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from exomem import hosted_probe, hosted_security


def test_hosted_probe_module_is_available() -> None:
    assert importlib.util.find_spec("exomem.hosted_probe") is not None


def _credential(label: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(label.encode()).digest()).rstrip(b"=").decode()


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _authority(
    tmp_path: Path,
    *,
    overlap: bool = True,
) -> tuple[hosted_security.HostedSecurityAuthority, list[hosted_security.CredentialBundle]]:
    active = _credential("active")
    pending = _credential("pending")
    bundle = [hosted_security.CredentialBundle({"active": active})]
    authority = hosted_security.HostedSecurityAuthority(
        tmp_path / "state",
        cell_id="cell-alpha",
        vault_id="vault-alpha",
        bundle_loader=lambda: bundle[0],
    )
    authority.bootstrap(
        active_version="active",
        operation_id="bootstrap",
        request_digest=_digest("bootstrap"),
    )
    if overlap:
        bundle[0] = hosted_security.CredentialBundle(
            {"active": active, "pending": pending}
        )
        authority.stage(
            pending_version="pending",
            expected_revision=1,
            operation_id="stage",
            request_digest=_digest("stage"),
        )
    return authority, bundle


def _request(
    *,
    selected_version: str = "pending",
    expected_revision: int = 2,
    operation_id: str = "probe-operation",
    port: int = 8765,
) -> hosted_probe.HostedProbeRequest:
    return hosted_probe.HostedProbeRequest(
        request_id="11111111-1111-4111-8111-111111111111",
        operation_id=operation_id,
        request_digest=_digest(operation_id),
        cell_id="cell-alpha",
        vault_id="vault-alpha",
        selected_credential_version=selected_version,
        expected_release="0.20.0",
        expected_protocol="1",
        expected_worker_policy_digest=_digest("workers"),
        expected_revision=expected_revision,
        port=port,
    )


def _readiness(
    *,
    selected_version: str = "pending",
    security_revision: int = 2,
    **overrides: Any,
) -> dict[str, Any]:
    data = {
        "cell_id": "cell-alpha",
        "vault_id": "vault-alpha",
        "exomem_release": "0.20.0",
        "hosted_protocol": "1",
        "authenticated_credential_version": selected_version,
        "security_revision": security_revision,
        "service_authenticated": True,
        "mutation_authority": True,
        "admission_phase": "active",
        "read_admission": True,
        "write_admission": True,
        "worker_policy_digest": _digest("workers"),
    }
    data.update(overrides)
    return {"success": True, "data": data}


def _transport(
    payload: dict[str, Any] | bytes,
    *,
    status: int = 200,
    media_type: str = "application/json",
    requests: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return httpx.Response(
            status,
            headers={"Content-Type": media_type},
            content=raw,
            request=request,
        )

    return httpx.MockTransport(handler)


def test_probe_uses_only_literal_loopback_and_hardened_http_client(tmp_path: Path) -> None:
    authority, _bundle = _authority(tmp_path)
    seen_requests: list[httpx.Request] = []
    client_options: list[dict[str, Any]] = []
    transport = _transport(_readiness(), requests=seen_requests)

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        client_options.append(kwargs)
        return httpx.AsyncClient(**kwargs)

    result = asyncio.run(
        hosted_probe.run_hosted_probe(
            _request(),
            authority=authority,
            transport=transport,
            client_factory=client_factory,
            uuid_factory=lambda: uuid.UUID("22222222-2222-4222-8222-222222222222"),
            random_bytes=lambda size: b"p" * size,
            now=lambda: 1_700_000_000,
        )
    )

    assert result.proof_recorded is True
    assert result.proof_valid_until == "2023-11-14T22:18:20Z"
    assert result.as_data()["proof_valid_until"] == "2023-11-14T22:18:20Z"
    [sent] = seen_requests
    assert str(sent.url) == "http://127.0.0.1:8765/private/exomem/v1/ready"
    assert sent.url.host == "127.0.0.1"
    assert sent.url.query == b""
    assert sent.headers["X-Exomem-Request-Id"] == "22222222-2222-4222-8222-222222222222"
    assert len(sent.headers["X-Exomem-Principal-Scope"]) == 43
    assert sent.headers["Authorization"] == f"Bearer {_credential('pending')}"
    assert client_options[0]["trust_env"] is False
    assert client_options[0]["follow_redirects"] is False
    timeout = client_options[0]["timeout"]
    assert timeout.connect == 1.0
    assert timeout.read == 2.0
    assert client_options[0]["transport"] is transport


def test_probe_retry_always_sends_fresh_request_and_principal(tmp_path: Path) -> None:
    authority, _bundle = _authority(tmp_path)
    requests: list[httpx.Request] = []
    transport = _transport(_readiness(), requests=requests)
    uuids = iter(
        [
            uuid.UUID("22222222-2222-4222-8222-222222222222"),
            uuid.UUID("33333333-3333-4333-8333-333333333333"),
        ]
    )
    random_values = iter([b"a" * 32, b"b" * 32])

    for _ in range(2):
        result = asyncio.run(
            hosted_probe.run_hosted_probe(
                _request(),
                authority=authority,
                transport=transport,
                uuid_factory=lambda: next(uuids),
                random_bytes=lambda _size: next(random_values),
                now=lambda: 1_700_000_000,
            )
        )
        assert result.proof_recorded is True

    assert len(requests) == 2
    assert requests[0].headers["X-Exomem-Request-Id"] != requests[1].headers[
        "X-Exomem-Request-Id"
    ]
    assert requests[0].headers["X-Exomem-Principal-Scope"] != requests[1].headers[
        "X-Exomem-Principal-Scope"
    ]


def test_active_probe_never_records_rotation_proof(tmp_path: Path) -> None:
    authority, _bundle = _authority(tmp_path, overlap=False)
    result = asyncio.run(
        hosted_probe.run_hosted_probe(
            _request(selected_version="active", expected_revision=1),
            authority=authority,
            transport=_transport(_readiness(selected_version="active", security_revision=1)),
            now=lambda: 1_700_000_000,
        )
    )

    assert result.proof_recorded is False
    assert result.proof_valid_until is None
    assert authority.snapshot().proof_valid_until is None


def test_probe_maps_weak_projected_credential_to_stable_bundle_error() -> None:
    class WeakBundleAuthority:
        def credential_material(self, version: str) -> hosted_security.CredentialMaterial:
            raise hosted_security.HostedCredentialWeak()

    with pytest.raises(hosted_probe.HostedProbeError) as error:
        asyncio.run(
            hosted_probe.run_hosted_probe(
                _request(),
                authority=WeakBundleAuthority(),
                transport=_transport(_readiness()),
            )
        )

    assert error.value.code == "HOSTED_CREDENTIAL_BUNDLE_INVALID"


@pytest.mark.parametrize("port", [0, 80, 1023, 65536, True])
def test_probe_rejects_invalid_port_before_loading_a_credential(port: object) -> None:
    class NeverAuthority:
        def credential_material(self, _version: str) -> object:
            pytest.fail("credential loaded for invalid transport")

    with pytest.raises(hosted_probe.HostedProbeError) as error:
        asyncio.run(
            hosted_probe.run_hosted_probe(
                _request(port=port),  # type: ignore[arg-type]
                authority=NeverAuthority(),  # type: ignore[arg-type]
                transport=_transport(_readiness()),
            )
        )

    assert error.value.code == "HOSTED_PROBE_TRANSPORT_INVALID"


def test_probe_rejects_redirect_without_following_or_recording(tmp_path: Path) -> None:
    authority, _bundle = _authority(tmp_path)
    requests: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            headers={"Location": "https://attacker.invalid/steal"},
            request=request,
        )

    with pytest.raises(hosted_probe.HostedProbeError) as error:
        asyncio.run(
            hosted_probe.run_hosted_probe(
                _request(),
                authority=authority,
                transport=httpx.MockTransport(redirect),
            )
        )

    assert error.value.code == "HOSTED_PROBE_REDIRECT"
    assert len(requests) == 1
    assert authority.snapshot().proof_valid_until is None
    assert "attacker" not in str(error.value)


@pytest.mark.parametrize(
    ("payload", "media_type", "status", "code"),
    [
        (b"x" * 16_385, "application/json", 200, "HOSTED_PROBE_RESPONSE_TOO_LARGE"),
        (_readiness(), "application/json; charset=utf-8", 200, "HOSTED_PROBE_MEDIA_INVALID"),
        (b'{"success":true,"success":true,"data":{}}', "application/json", 200, "HOSTED_PROBE_SCHEMA_INVALID"),
        ({**_readiness(), "extra": True}, "application/json", 200, "HOSTED_PROBE_SCHEMA_INVALID"),
        (_readiness(), "application/json", 401, "HOSTED_PROBE_AUTH_FAILED"),
    ],
)
def test_probe_enforces_bounded_exact_response_contract(
    tmp_path: Path,
    payload: dict[str, Any] | bytes,
    media_type: str,
    status: int,
    code: str,
) -> None:
    authority, _bundle = _authority(tmp_path)

    with pytest.raises(hosted_probe.HostedProbeError) as error:
        asyncio.run(
            hosted_probe.run_hosted_probe(
                _request(),
                authority=authority,
                transport=_transport(payload, media_type=media_type, status=status),
            )
        )

    assert error.value.code == code
    assert authority.snapshot().proof_valid_until is None
    assert _credential("pending") not in str(error.value)


@pytest.mark.parametrize(
    "override",
    [
        {"cell_id": "cell-foreign"},
        {"vault_id": "vault-foreign"},
        {"exomem_release": "other"},
        {"hosted_protocol": "2"},
        {"authenticated_credential_version": "active"},
        {"security_revision": 1},
        {"service_authenticated": False},
        {"mutation_authority": False},
        {"admission_phase": "quiesced"},
        {"read_admission": False},
        {"write_admission": False},
        {"worker_policy_digest": _digest("other-workers")},
    ],
)
def test_probe_contract_mismatch_records_no_proof(
    tmp_path: Path, override: dict[str, Any]
) -> None:
    authority, _bundle = _authority(tmp_path)

    with pytest.raises(hosted_probe.HostedProbeError) as error:
        asyncio.run(
            hosted_probe.run_hosted_probe(
                _request(),
                authority=authority,
                transport=_transport(_readiness(**override)),
            )
        )

    assert error.value.code == "HOSTED_PROBE_CONTRACT_MISMATCH"
    assert authority.snapshot().proof_valid_until is None


@pytest.mark.parametrize(
    "failure",
    [
        hosted_security.HostedCredentialRevisionConflict,
        hosted_security.HostedCredentialTransitionInvalid,
        hosted_security.HostedOperationConflict,
    ],
)
def test_probe_maps_proof_persistence_races_to_stable_state_error(
    tmp_path: Path, failure: type[hosted_security.HostedSecurityError]
) -> None:
    authority, _bundle = _authority(tmp_path)

    class RacingAuthority:
        def credential_material(self, version: str) -> hosted_security.CredentialMaterial:
            return authority.credential_material(version)

        def record_probe_proof(self, **kwargs: Any) -> hosted_security.ProofPersistence:
            raise failure()

    with pytest.raises(hosted_probe.HostedProbeError) as error:
        asyncio.run(
            hosted_probe.run_hosted_probe(
                _request(),
                authority=RacingAuthority(),
                transport=_transport(_readiness()),
            )
        )

    assert error.value.code == "HOSTED_CREDENTIAL_STATE_INVALID"


def test_probe_maps_timeout_and_never_exposes_transport_details(tmp_path: Path) -> None:
    authority, _bundle = _authority(tmp_path)

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret endpoint timed out", request=request)

    with pytest.raises(hosted_probe.HostedProbeError) as error:
        asyncio.run(
            hosted_probe.run_hosted_probe(
                _request(),
                authority=authority,
                transport=httpx.MockTransport(timeout),
            )
        )

    assert error.value.code == "HOSTED_PROBE_TIMEOUT"
    assert str(error.value) == "HOSTED_PROBE_TIMEOUT: hosted probe timed out"


def test_probe_operator_adapter_returns_exact_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = hosted_probe.HostedProbeResult(
        **_readiness()["data"],
        proof_recorded=True,
        proof_valid_until="2023-11-14T22:18:20Z",
    )
    seen: dict[str, Any] = {}

    async def run(request: hosted_probe.HostedProbeRequest, *, authority: Any) -> Any:
        seen["request"] = request
        seen["authority"] = authority
        return result

    class OperatorFailure(RuntimeError):
        def __init__(self, code: str) -> None:
            self.code = code

    monkeypatch.setattr(hosted_probe, "run_hosted_probe", run)
    monkeypatch.setattr(
        hosted_security,
        "load_credential_bundle",
        lambda: hosted_security.CredentialBundle({"pending": _credential("pending")}),
    )
    monkeypatch.setitem(
        sys.modules,
        "exomem.hosted_operator",
        SimpleNamespace(
            OperatorFailure=OperatorFailure,
            canonical_request_digest=lambda request: _digest("operator-request"),
        ),
    )
    request = {
        "request_id": str(uuid.uuid4()),
        "operation_id": "probe",
        "cell_id": "cell-alpha",
        "vault_id": "vault-alpha",
        "state_root": str(tmp_path / "state"),
        "selected_credential_version": "pending",
        "expected_release": "0.20.0",
        "expected_protocol": "1",
        "expected_worker_policy_digest": _digest("workers"),
        "expected_revision": 2,
        "port": 8765,
    }

    code, data = hosted_security.execute_probe_operator(request)

    assert code == "HOSTED_PROBE_READY"
    assert data == result.as_data()
    assert seen["request"].request_digest == _digest("operator-request")
    assert seen["authority"].cell_id == "cell-alpha"


def test_probe_operator_adapter_maps_modeled_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(request: hosted_probe.HostedProbeRequest, *, authority: Any) -> Any:
        raise hosted_probe.HostedProbeError("HOSTED_PROBE_TIMEOUT")

    class OperatorFailure(RuntimeError):
        def __init__(self, code: str) -> None:
            self.code = code

    monkeypatch.setattr(hosted_probe, "run_hosted_probe", fail)
    monkeypatch.setitem(
        sys.modules,
        "exomem.hosted_operator",
        SimpleNamespace(
            OperatorFailure=OperatorFailure,
            canonical_request_digest=lambda request: _digest("operator-request"),
        ),
    )

    with pytest.raises(OperatorFailure) as error:
        hosted_security.execute_probe_operator(
            {
                "request_id": str(uuid.uuid4()),
                "operation_id": "probe",
                "cell_id": "cell-alpha",
                "vault_id": "vault-alpha",
                "state_root": str(tmp_path / "state"),
                "selected_credential_version": "pending",
                "expected_release": "0.22.0",
                "expected_protocol": "1",
                "expected_worker_policy_digest": _digest("workers"),
                "expected_revision": 2,
                "port": 8765,
            }
        )

    assert error.value.code == "HOSTED_PROBE_TIMEOUT"
