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


@pytest.mark.anyio
async def test_schema_fence_cas_revokes_the_old_holder_and_rejects_legacy_acquire(
    tmp_path: Path,
) -> None:
    app = create_app(
        database=tmp_path / "coordinator.sqlite",
        bearer_token="lease-secret",
        operator_token="operator-secret",
    )
    transport = httpx.ASGITransport(app=app)
    lease_headers = {"Authorization": "Bearer lease-secret"}
    operator_headers = {"Authorization": "Bearer operator-secret"}

    async with httpx.AsyncClient(
        transport=transport, base_url="https://coordinator.example"
    ) as client:
        legacy = await client.post(
            "/v1/vaults/main/lease/acquire",
            json={"replica_id": "old-v3", "ttl_seconds": 30},
            headers=lease_headers,
        )
        assert legacy.json()["granted"] is True

        fenced = await client.put(
            "/v1/vaults/main/schema-fence",
            json={"expected_generation": 0, "schema_version": 4},
            headers=operator_headers,
        )
        assert fenced.status_code == 200
        assert fenced.json() == {
            "governance_enrolled": True,
            "schema_version": 4,
            "generation": 1,
        }

        rejected = await client.post(
            "/v1/vaults/main/lease/acquire",
            json={"replica_id": "old-v3", "ttl_seconds": 30},
            headers=lease_headers,
        )
        deployment_rejected = await client.post(
            "/v1/vaults/main/schema-fence/admit",
            json={"replica_id": "old-v3", "schema_version": 3},
            headers=operator_headers,
        )
        admitted = await client.post(
            "/v1/vaults/main/lease/acquire",
            json={
                "replica_id": "current-v4",
                "ttl_seconds": 30,
                "schema_version": 4,
            },
            headers=lease_headers,
        )

    assert rejected.status_code == 200
    assert rejected.json()["granted"] is False
    assert rejected.json()["required_schema_version"] == 4
    assert rejected.json()["schema_fence_generation"] == 1
    assert deployment_rejected.json() == {
        "admitted": False,
        "governance_enrolled": True,
        "required_schema_version": 4,
        "schema_fence_generation": 1,
    }
    assert admitted.json()["granted"] is True
    assert admitted.json()["holder"] == "current-v4"
    assert admitted.json()["fencing_token"] > legacy.json()["fencing_token"]


@pytest.mark.anyio
async def test_schema_fence_is_operator_only_monotonic_and_rollback_reopens_v3(
    tmp_path: Path,
) -> None:
    app = create_app(
        database=tmp_path / "coordinator.sqlite",
        bearer_token="lease-secret",
        operator_token="operator-secret",
    )
    transport = httpx.ASGITransport(app=app)
    lease_headers = {"Authorization": "Bearer lease-secret"}
    operator_headers = {"Authorization": "Bearer operator-secret"}

    async with httpx.AsyncClient(
        transport=transport, base_url="https://coordinator.example"
    ) as client:
        denied = await client.put(
            "/v1/vaults/main/schema-fence",
            json={"expected_generation": 0, "schema_version": 4},
            headers=lease_headers,
        )
        assert denied.status_code == 401
        admission_denied = await client.post(
            "/v1/vaults/main/schema-fence/admit",
            json={"replica_id": "old-v3", "schema_version": 3},
            headers=lease_headers,
        )
        assert admission_denied.status_code == 401

        first = await client.put(
            "/v1/vaults/main/schema-fence",
            json={"expected_generation": 0, "schema_version": 4},
            headers=operator_headers,
        )
        stale = await client.put(
            "/v1/vaults/main/schema-fence",
            json={"expected_generation": 0, "schema_version": 3},
            headers=operator_headers,
        )
        rollback = await client.put(
            "/v1/vaults/main/schema-fence",
            json={"expected_generation": 1, "schema_version": 3},
            headers=operator_headers,
        )
        legacy = await client.post(
            "/v1/vaults/main/lease/acquire",
            json={"replica_id": "old-v3", "ttl_seconds": 30},
            headers=lease_headers,
        )
        deployment_admitted = await client.post(
            "/v1/vaults/main/schema-fence/admit",
            json={"replica_id": "old-v3", "schema_version": 3},
            headers=operator_headers,
        )

    assert first.json()["generation"] == 1
    assert stale.status_code == 409
    assert rollback.json() == {
        "governance_enrolled": True,
        "schema_version": 3,
        "generation": 2,
    }
    assert legacy.json()["granted"] is True
    assert deployment_admitted.json() == {
        "admitted": True,
        "governance_enrolled": True,
        "required_schema_version": 3,
        "schema_fence_generation": 2,
    }


def test_schema_fence_rejects_reusing_the_normal_lease_bearer(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="operator token must differ"):
        create_app(
            database=tmp_path / "coordinator.sqlite",
            bearer_token="shared-secret",
            operator_token="shared-secret",
        )
