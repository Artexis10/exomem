from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from exomem.lease_coordinator import create_app


@pytest.mark.anyio
async def test_state_atomic_put_and_list_keys_require_bearer(tmp_path: Path) -> None:
    app = create_app(database=tmp_path / "coordinator.sqlite", bearer_token="secret")
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="https://coordinator.example") as client:
        denied = await client.post(
            "/v1/state/main/list-keys", json={"collection": "auth"}
        )
        assert denied.status_code == 401

        headers = {"Authorization": "Bearer secret"}
        first = await client.post(
            "/v1/state/main/put-if-absent",
            json={
                "collection": "auth",
                "key": "generation",
                "value": {"__encrypted_data__": "ciphertext"},
                "ttl": None,
            },
            headers=headers,
        )
        second = await client.post(
            "/v1/state/main/put-if-absent",
            json={
                "collection": "auth",
                "key": "generation",
                "value": {"__encrypted_data__": "replacement"},
                "ttl": None,
            },
            headers=headers,
        )
        listed = await client.post(
            "/v1/state/main/list-keys", json={"collection": "auth"}, headers=headers
        )

    assert first.json() == {"result": True}
    assert second.json() == {"result": False}
    assert listed.json() == {"result": ["generation"]}
    assert "ciphertext" not in listed.text
    assert "replacement" not in listed.text
