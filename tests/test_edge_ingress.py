"""Unit tests for the origin-side edge-transit stamp enforcement (design.md
Decision 1): the pure predicate, the HMAC helper, and the ASGI middleware
matrix (enforce / exempt / kill-switch / lease-disabled)."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from exomem import edge_ingress

# --------------------------------------------------------------------------- #
# Pure HMAC + predicate helpers
# --------------------------------------------------------------------------- #
# Golden cross-implementation vector: the worker suite asserts the identical
# constant for edgeAuthHmac (deploy/cloudflare-ha/test/worker.test.mjs), so a
# unilateral change to key/message encoding or hex casing in either
# implementation fails one of the two suites instead of 403ing production.
_GOLDEN_EDGE_AUTH = "1d489d84d7a8dcec3ddef522064e6ee09269bb80fd3dc91cd62c4ebf1ab220b4"


def test_compute_edge_auth_matches_worker_hmac() -> None:
    digest = edge_ingress.compute_edge_auth("secret-token", "11111111-1111-4111-8111-111111111111")
    assert digest == _GOLDEN_EDGE_AUTH


def test_is_valid_stamp_accepts_matching_hmac() -> None:
    token = "secret-token"
    request_id = "11111111-1111-4111-8111-111111111111"
    auth = edge_ingress.compute_edge_auth(token, request_id)
    assert edge_ingress.is_valid_stamp(token, request_id, auth) is True


def test_is_valid_stamp_rejects_wrong_hmac() -> None:
    token = "secret-token"
    request_id = "11111111-1111-4111-8111-111111111111"
    assert edge_ingress.is_valid_stamp(token, request_id, "0" * 64) is False


def test_is_valid_stamp_rejects_malformed_auth_without_raising() -> None:
    # Non-ASCII header bytes decode via latin-1 into a non-ASCII str, which
    # hmac.compare_digest refuses with TypeError; malformed proofs must be
    # invalid, not a 500.
    token = "secret-token"
    request_id = "11111111-1111-4111-8111-111111111111"
    assert edge_ingress.is_valid_stamp(token, request_id, "\xff" * 64) is False
    assert edge_ingress.is_valid_stamp(token, request_id, "not-hex") is False
    assert edge_ingress.is_valid_stamp(token, request_id, "A" * 64) is False


@pytest.mark.parametrize(
    ("request_id", "presented_auth"),
    [
        (None, "a" * 64),
        ("11111111-1111-4111-8111-111111111111", None),
        (None, None),
        ("", ""),
    ],
)
def test_is_valid_stamp_requires_both_pieces(
    request_id: str | None, presented_auth: str | None
) -> None:
    assert edge_ingress.is_valid_stamp("secret-token", request_id, presented_auth) is False


class TestIsIngressViolation:
    _TOKEN = "secret-token"
    _REQUEST_ID = "11111111-1111-4111-8111-111111111111"

    def _valid_auth(self) -> str:
        return edge_ingress.compute_edge_auth(self._TOKEN, self._REQUEST_ID)

    def test_lease_disabled_is_never_a_violation(self) -> None:
        assert (
            edge_ingress.is_ingress_violation(
                lease_enabled=False,
                token=None,
                method="POST",
                has_cf_ray=True,
                request_id=None,
                presented_auth=None,
            )
            is False
        )

    def test_no_token_configured_is_never_a_violation(self) -> None:
        assert (
            edge_ingress.is_ingress_violation(
                lease_enabled=True,
                token=None,
                method="POST",
                has_cf_ray=True,
                request_id=None,
                presented_auth=None,
            )
            is False
        )

    def test_no_cf_ray_is_exempt_local_traffic(self) -> None:
        assert (
            edge_ingress.is_ingress_violation(
                lease_enabled=True,
                token=self._TOKEN,
                method="POST",
                has_cf_ray=False,
                request_id=None,
                presented_auth=None,
            )
            is False
        )

    def test_get_is_exempt_even_without_a_stamp(self) -> None:
        assert (
            edge_ingress.is_ingress_violation(
                lease_enabled=True,
                token=self._TOKEN,
                method="GET",
                has_cf_ray=True,
                request_id=None,
                presented_auth=None,
            )
            is False
        )

    def test_valid_stamp_on_post_is_not_a_violation(self) -> None:
        assert (
            edge_ingress.is_ingress_violation(
                lease_enabled=True,
                token=self._TOKEN,
                method="POST",
                has_cf_ray=True,
                request_id=self._REQUEST_ID,
                presented_auth=self._valid_auth(),
            )
            is False
        )

    def test_missing_stamp_on_post_is_a_violation(self) -> None:
        assert (
            edge_ingress.is_ingress_violation(
                lease_enabled=True,
                token=self._TOKEN,
                method="POST",
                has_cf_ray=True,
                request_id=None,
                presented_auth=None,
            )
            is True
        )

    def test_wrong_stamp_on_post_is_a_violation(self) -> None:
        assert (
            edge_ingress.is_ingress_violation(
                lease_enabled=True,
                token=self._TOKEN,
                method="POST",
                has_cf_ray=True,
                request_id=self._REQUEST_ID,
                presented_auth="0" * 64,
            )
            is True
        )

    def test_auth_present_without_its_request_id_is_a_violation(self) -> None:
        assert (
            edge_ingress.is_ingress_violation(
                lease_enabled=True,
                token=self._TOKEN,
                method="POST",
                has_cf_ray=True,
                request_id=None,
                presented_auth=self._valid_auth(),
            )
            is True
        )


# --------------------------------------------------------------------------- #
# enforcement_disabled kill switch
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw", ["0", "false", "False", "0 "])
def test_enforcement_disabled_recognizes_kill_switch_values(raw: str) -> None:
    assert edge_ingress.enforcement_disabled({"EXOMEM_EDGE_STAMP_ENFORCE": raw}) is True


@pytest.mark.parametrize("raw", ["1", "true", "", "no", "off"])
def test_enforcement_disabled_defaults_to_enforcing(raw: str) -> None:
    assert edge_ingress.enforcement_disabled({"EXOMEM_EDGE_STAMP_ENFORCE": raw}) is False


def test_enforcement_disabled_defaults_to_enforcing_when_unset() -> None:
    assert edge_ingress.enforcement_disabled({}) is False


# --------------------------------------------------------------------------- #
# ASGI middleware matrix
# --------------------------------------------------------------------------- #
_TOKEN = "secret-token"
_VAULT_ID = "main"
_REPLICA_ID = "desktop"
_REQUEST_ID = "11111111-1111-4111-8111-111111111111"


def _set_lease_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_URL", "https://coordinator.example.com")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_VAULT_ID", _VAULT_ID)
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_REPLICA_ID", _REPLICA_ID)
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_TOKEN", _TOKEN)
    monkeypatch.delenv("EXOMEM_EDGE_STAMP_ENFORCE", raising=False)


async def _receive() -> dict:
    return {"type": "http.disconnect"}


def _scope(*, method: str = "POST", path: str = "/mcp", headers: dict[str, str] | None = None) -> dict:
    encoded = [
        (name.encode("ascii"), value.encode("ascii")) for name, value in (headers or {}).items()
    ]
    return {"type": "http", "method": method, "path": path, "headers": encoded}


def _run_middleware(scope: dict) -> tuple[list[dict], bool]:
    app_called = False

    async def inner_app(inner_scope, receive, send) -> None:  # noqa: ANN001
        nonlocal app_called
        app_called = True

    sent: list[dict] = []

    async def capture(message: dict) -> None:
        sent.append(message)

    middleware = edge_ingress.EdgeIngressMiddleware(inner_app)

    async def scenario() -> None:
        await middleware(scope, _receive, capture)

    asyncio.run(scenario())
    return sent, app_called


def test_enforced_refusal_returns_403_envelope_and_does_not_call_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_lease_env(monkeypatch)
    scope = _scope(headers={"cf-ray": "abc123"})

    sent, app_called = _run_middleware(scope)

    assert app_called is False
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 403
    body = json.loads(sent[1]["body"])
    assert body["success"] is False
    assert body["error"]["code"] == "INGRESS_BYPASSED"
    assert body["error"]["message"] == edge_ingress.INGRESS_BYPASSED_MESSAGE
    assert "EXOMEM_EDGE_STAMP_ENFORCE=0" in body["error"]["remediation"]


def test_valid_stamp_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_lease_env(monkeypatch)
    auth = edge_ingress.compute_edge_auth(_TOKEN, _REQUEST_ID)
    scope = _scope(
        headers={
            "cf-ray": "abc123",
            "x-exomem-request-id": _REQUEST_ID,
            "x-exomem-edge-auth": auth,
        }
    )

    sent, app_called = _run_middleware(scope)

    assert app_called is True
    assert sent == []


def test_no_cf_ray_is_exempt(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_lease_env(monkeypatch)
    scope = _scope(headers={})

    sent, app_called = _run_middleware(scope)

    assert app_called is True
    assert sent == []


def test_unstamped_put_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # The transfer upload is the one non-POST mutating route; the worker stamps
    # it, so a bare PUT through Cloudflare is ingress drift like any other.
    _set_lease_env(monkeypatch)
    scope = _scope(method="PUT", path="/api/transfer/upload", headers={"cf-ray": "abc123"})

    sent, app_called = _run_middleware(scope)

    assert app_called is False
    assert sent[0]["status"] == 403
    assert json.loads(sent[1]["body"])["error"]["code"] == "INGRESS_BYPASSED"


def test_stamped_put_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_lease_env(monkeypatch)
    auth = edge_ingress.compute_edge_auth(_TOKEN, _REQUEST_ID)
    scope = _scope(
        method="PUT",
        path="/api/transfer/upload",
        headers={
            "cf-ray": "abc123",
            "x-exomem-request-id": _REQUEST_ID,
            "x-exomem-edge-auth": auth,
        },
    )

    sent, app_called = _run_middleware(scope)

    assert app_called is True
    assert sent == []


def test_get_is_exempt(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_lease_env(monkeypatch)
    scope = _scope(method="GET", headers={"cf-ray": "abc123"})

    sent, app_called = _run_middleware(scope)

    assert app_called is True
    assert sent == []


def test_lease_disabled_serves_unconditionally(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "EXOMEM_WRITER_LEASE_URL",
        "EXOMEM_WRITER_LEASE_VAULT_ID",
        "EXOMEM_WRITER_LEASE_REPLICA_ID",
        "EXOMEM_WRITER_LEASE_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    scope = _scope(headers={"cf-ray": "abc123"})

    sent, app_called = _run_middleware(scope)

    assert app_called is True
    assert sent == []


def test_kill_switch_serves_the_request_but_logs_the_bypass_content_free(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _set_lease_env(monkeypatch)
    monkeypatch.setenv("EXOMEM_EDGE_STAMP_ENFORCE", "0")
    scope = _scope(path="/api/edit_memory", headers={"cf-ray": "abc123"})

    with caplog.at_level(logging.WARNING, logger="exomem.edge_ingress"):
        sent, app_called = _run_middleware(scope)

    assert app_called is True
    assert sent == []
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert "edge_ingress_bypassed" in messages[0]
    assert "/api/edit_memory" in messages[0]
    assert "count=1" in messages[0]


def test_non_http_scope_passes_through_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_lease_env(monkeypatch)
    scope = {"type": "lifespan"}

    sent, app_called = _run_middleware(scope)

    assert app_called is True
    assert sent == []
