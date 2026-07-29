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


@pytest.mark.anyio
async def test_release_endpoint_accepts_any_replica_id_in_the_body(tmp_path: Path) -> None:
    """The lease CLI's cross-device `release_holder(holder_replica_id, ...)`
    (R6) needs no coordinator change: `/release` already keys on the
    request BODY's `replica_id`, not the caller's own bearer-token identity
    — the bearer is a single shared HA-cell secret, not a per-replica
    credential."""
    app = create_app(database=tmp_path / "coordinator.sqlite", bearer_token="secret")
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer secret"}

    async with httpx.AsyncClient(transport=transport, base_url="https://coordinator.example") as client:
        acquired = await client.post(
            "/v1/vaults/main/lease/acquire",
            json={"replica_id": "laptop", "ttl_seconds": 30},
            headers=headers,
        )
        assert acquired.json()["holder"] == "laptop"
        token = acquired.json()["fencing_token"]

        # An operator's release request names "laptop" explicitly — it is
        # not the coordinator's own identity, proving the endpoint accepts
        # release-on-behalf-of without any server-side change.
        released = await client.post(
            "/v1/vaults/main/lease/release",
            json={"replica_id": "laptop", "fencing_token": token},
            headers=headers,
        )
        assert released.json()["granted"] is True
        assert released.json()["holder"] is None

        status = await client.get("/v1/vaults/main/lease", headers=headers)
    assert status.json()["holder"] is None


@pytest.mark.anyio
async def test_release_endpoint_is_a_no_op_when_unheld_or_mismatched(tmp_path: Path) -> None:
    app = create_app(database=tmp_path / "coordinator.sqlite", bearer_token="secret")
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer secret"}

    async with httpx.AsyncClient(transport=transport, base_url="https://coordinator.example") as client:
        released = await client.post(
            "/v1/vaults/main/lease/release",
            json={"replica_id": "nobody", "fencing_token": 1},
            headers=headers,
        )
    assert released.json()["granted"] is False
