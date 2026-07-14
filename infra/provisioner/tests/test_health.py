from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import pytest

from exomem_provisioner.app import create_app
from exomem_provisioner.config import PROVISIONER_PROTOCOL, ProvisionerSettings


def _settings() -> ProvisionerSettings:
    return ProvisionerSettings(
        bearer="b" * 32,
        envelope_key="k" * 32,
        database_url="sqlite+aiosqlite:///:memory:",
        database_schema="exomem_provisioner",
        database_role="exomem_provisioner_runtime",
    )


def _probe(value: bool) -> Callable[[], Awaitable[bool]]:
    async def probe() -> bool:
        return value

    return probe


@pytest.mark.asyncio
async def test_health_endpoints_are_content_free_and_readiness_checks_database() -> None:
    settings = _settings()
    live_app = create_app(settings=settings, readiness_probe=_probe(True))
    failed_app = create_app(settings=settings, readiness_probe=_probe(False))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=live_app), base_url="https://provisioner.test"
    ) as client:
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=failed_app), base_url="https://provisioner.test"
    ) as client:
        unavailable = await client.get("/health/ready")

    assert live.status_code == 200
    assert live.json() == {"protocol": PROVISIONER_PROTOCOL, "status": "live"}
    assert ready.status_code == 200
    assert ready.json() == {"protocol": PROVISIONER_PROTOCOL, "status": "ready"}
    assert unavailable.status_code == 503
    assert unavailable.json() == {
        "code": "PROVISIONER_UNAVAILABLE",
        "retryable": True,
    }


def test_settings_repr_redacts_startup_secrets() -> None:
    rendered = repr(_settings())

    assert "b" * 32 not in rendered
    assert "k" * 32 not in rendered
