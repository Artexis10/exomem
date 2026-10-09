from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from exomem.lease_coordinator import SQLiteLeaseStore, create_app

STORE_ID = "a9d3d366-2509-46ca-bc29-3f318ef9d78d"
STORE_HEAD = {
    "store_id": STORE_ID,
    "instance_id": "6044a9ed-b2d4-4c50-b65f-af08c7504dbb",
    "commit_seq": 1,
    "head_hash": "a" * 64,
}
STORE_CAPABILITY = "collections-store-v1"


@pytest.mark.anyio
async def test_renew_rejects_malformed_store_head_instead_of_ignoring_it(tmp_path: Path) -> None:
    """A reported head must not receive a successful but non-persisting acknowledgement."""
    app = create_app(database=tmp_path / "coordinator.sqlite")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://coordinator.example"
    ) as client:
        acquired = await client.post(
            "/v1/vaults/main/lease/acquire", json={"replica_id": "writer", "ttl_seconds": 30}
        )
        response = await client.post(
            "/v1/vaults/main/lease/renew",
            json={
                "replica_id": "writer",
                "ttl_seconds": 30,
                "fencing_token": acquired.json()["fencing_token"],
                "collection_store_head": {**STORE_HEAD, "commit_seq": True},
            },
        )
    assert response.status_code == 400


@pytest.mark.anyio
async def test_store_fence_proves_support_and_atomically_cuts_legacy_authority(
    tmp_path: Path,
) -> None:
    """Explicit operator enrollment must revoke old tokens without changing file-only admission."""
    app = create_app(
        database=tmp_path / "coordinator.sqlite", bearer_token="lease", operator_token="operator"
    )
    fence_path = "/v1/vaults/main/collection-store-fence"
    target = {"capability": STORE_CAPABILITY, "store_id": STORE_ID, "expected_generation": 0}
    operator = {"Authorization": "Bearer operator"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://coordinator.example",
        headers={"Authorization": "Bearer lease"},
    ) as client:
        # The writer bearer reads the fence a serving store opens against; only the operator moves it.
        assert (await client.put(fence_path, json=target)).status_code == 401
        assert (await client.get(fence_path, headers={"Authorization": "Bearer other"})).status_code == 401
        probe = await client.get(fence_path)
        assert probe.json() == {
            "capability": STORE_CAPABILITY,
            "enrolled": False,
            "store_id": None,
            "generation": 0,
        }
        legacy = {"replica_id": "writer", "ttl_seconds": 30}
        old = (await client.post("/v1/vaults/main/lease/acquire", json=legacy)).json()
        assert old["granted"] is True
        assert "collection_store_head" not in old
        conflict = await client.put(
            fence_path, headers=operator, json={**target, "expected_generation": 2}
        )
        assert conflict.status_code == 409
        enrolled = await client.put(fence_path, headers=operator, json=target)
        assert enrolled.status_code == 200
        assert enrolled.json() == {
            **probe.json(),
            "enrolled": True,
            "store_id": STORE_ID,
            "generation": 1,
        }
        cut = (await client.get("/v1/vaults/main/lease")).json()
        assert cut["holder"] is None
        assert cut["fencing_token"] == old["fencing_token"] + 1
        assert cut["collection_store_head"] is None
        for expected in (0, 1):
            replay = await client.put(
                fence_path, headers=operator, json={**target, "expected_generation": expected}
            )
            assert replay.json() == enrolled.json()
        assert (await client.get("/v1/vaults/main/lease")).json() == cut
        for changed in ({"expected_generation": 2}, {"store_id": STORE_HEAD["instance_id"]}):
            conflict = await client.put(fence_path, headers=operator, json={**target, **changed})
            assert conflict.status_code == 409
        for capability in (None, "unknown"):
            refused = await client.post(
                "/v1/vaults/main/lease/acquire",
                json={**legacy, "collection_store_capability": capability},
            )
            assert refused.json()["granted"] is False
        stale = await client.post(
            "/v1/vaults/main/lease/renew",
            json={
                **legacy,
                "fencing_token": old["fencing_token"],
                "collection_store_capability": STORE_CAPABILITY,
                "collection_store_head": STORE_HEAD,
            },
        )
        assert stale.json()["granted"] is False
        assert stale.json()["collection_store_head"] is None
        plain = (await client.post("/v1/vaults/files/lease/acquire", json=legacy)).json()
        assert plain["granted"] is True
        assert "required_collection_store_capability" not in plain


@pytest.mark.anyio
async def test_store_and_governance_fences_both_apply_to_acquire_and_renew(tmp_path: Path) -> None:
    """Meeting either one of two independent fences cannot grant a writer."""
    app = create_app(database=tmp_path / "coordinator.sqlite", operator_token="operator")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://coordinator.example"
    ) as client:
        for route, body in (
            ("schema-fence", {"schema_version": 4}),
            ("collection-store-fence", {"capability": STORE_CAPABILITY, "store_id": STORE_ID}),
        ):
            result = await client.put(
                f"/v1/vaults/main/{route}",
                headers={"Authorization": "Bearer operator"},
                json={"expected_generation": 0, **body},
            )
            assert result.status_code == 200
        valid = {
            "replica_id": "writer",
            "ttl_seconds": 30,
            "schema_version": 4,
            "collection_store_capability": STORE_CAPABILITY,
        }
        acquired = (await client.post("/v1/vaults/main/lease/acquire", json=valid)).json()
        assert acquired["granted"] is True
        assert acquired["required_schema_version"] == 4
        assert acquired["required_collection_store_capability"] == STORE_CAPABILITY
        for operation in ("acquire", "renew"):
            for missing in ("schema_version", "collection_store_capability"):
                body = {**valid, "fencing_token": acquired["fencing_token"]}
                del body[missing]
                refused = await client.post(f"/v1/vaults/main/lease/{operation}", json=body)
                assert refused.json()["granted"] is False


@pytest.mark.anyio
async def test_store_replacement_cas_does_not_repeat_a_cut_or_clear_a_new_head(
    tmp_path: Path,
) -> None:
    """Lost acknowledgements and ABA retries cannot revoke or erase a successor's authority."""
    app = create_app(database=tmp_path / "coordinator.sqlite", operator_token="operator")
    fence_path = "/v1/vaults/main/collection-store-fence"
    operator = {"Authorization": "Bearer operator"}
    target = {"capability": STORE_CAPABILITY, "store_id": STORE_ID, "expected_generation": 0}
    lease = {
        "replica_id": "writer",
        "ttl_seconds": 30,
        "collection_store_capability": STORE_CAPABILITY,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://coordinator.example"
    ) as client:
        assert (await client.put(fence_path, json=target, headers=operator)).status_code == 200
        acquired = (await client.post("/v1/vaults/main/lease/acquire", json=lease)).json()
        lease["fencing_token"] = acquired["fencing_token"]
        await client.post(
            "/v1/vaults/main/lease/renew", json={**lease, "collection_store_head": STORE_HEAD}
        )
        replacement = {**target, "store_id": STORE_HEAD["instance_id"], "expected_generation": 1}
        replaced = await client.put(fence_path, json=replacement, headers=operator)
        assert replaced.status_code == 200
        assert replaced.json()["generation"] == 2
        cut = (await client.get("/v1/vaults/main/lease")).json()
        assert cut["holder"] is None
        assert cut["fencing_token"] == acquired["fencing_token"] + 1
        assert cut["collection_store_head"] is None
        acquired = (await client.post("/v1/vaults/main/lease/acquire", json=lease)).json()
        old = await client.post(
            "/v1/vaults/main/lease/renew", json={**lease, "collection_store_head": STORE_HEAD}
        )
        assert old.status_code == 400
        lease["fencing_token"] = acquired["fencing_token"]
        new_head = {**STORE_HEAD, "store_id": replacement["store_id"]}
        renewed = (
            await client.post(
                "/v1/vaults/main/lease/renew", json={**lease, "collection_store_head": new_head}
            )
        ).json()
        assert renewed["collection_store_head"] == new_head
        replay = await client.put(fence_path, json=replacement, headers=operator)
        assert replay.json() == replaced.json()
        status = (await client.get("/v1/vaults/main/lease")).json()
        assert status == {**renewed, "granted": False}
        competing = await client.put(
            fence_path, json={**target, "expected_generation": 1}, headers=operator
        )
        assert competing.status_code == 409
        back = await client.put(
            fence_path, json={**target, "expected_generation": 2}, headers=operator
        )
        assert back.json()["generation"] == 3
        aba = await client.put(fence_path, json=target, headers=operator)
        assert aba.status_code == 409


@pytest.mark.anyio
async def test_store_head_survives_handoff_expiry_and_restart(tmp_path: Path) -> None:
    """Only a live holder can report a bound head; every lease-clearing path must retain it."""
    now = [100.0]
    database = tmp_path / "coordinator.sqlite"
    app = create_app(database=database, operator_token="operator", clock=lambda: now[0])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://coordinator.example"
    ) as client:
        enrolled = await client.put(
            "/v1/vaults/main/collection-store-fence",
            headers={"Authorization": "Bearer operator"},
            json={"capability": STORE_CAPABILITY, "store_id": STORE_ID, "expected_generation": 0},
        )
        assert enrolled.status_code == 200
        body = {
            "replica_id": "writer",
            "ttl_seconds": 30,
            "collection_store_capability": STORE_CAPABILITY,
        }
        acquired = (await client.post("/v1/vaults/main/lease/acquire", json=body)).json()
        body["fencing_token"] = acquired["fencing_token"]
        renewed = await client.post(
            "/v1/vaults/main/lease/renew", json={**body, "collection_store_head": STORE_HEAD}
        )
        assert renewed.json()["collection_store_head"] == STORE_HEAD
        wrong_store = await client.post(
            "/v1/vaults/main/lease/renew",
            json={
                **body,
                "collection_store_head": {**STORE_HEAD, "store_id": STORE_HEAD["instance_id"]},
            },
        )
        assert wrong_store.status_code == 400
        stale = await client.post(
            "/v1/vaults/main/lease/release",
            json={
                **body,
                "fencing_token": 0,
                "collection_store_head": {**STORE_HEAD, "commit_seq": 2},
            },
        )
        assert stale.json()["granted"] is False
        assert stale.json()["collection_store_head"] == STORE_HEAD
        # A lower sequence is valid: a future explicit local adoption can fork.
        genesis = {**STORE_HEAD, "commit_seq": 0, "head_hash": None}
        released = await client.post(
            "/v1/vaults/main/lease/release", json={**body, "collection_store_head": genesis}
        )
        assert released.json()["granted"] is True
        assert released.json()["holder"] is None
        assert released.json()["collection_store_head"] == genesis
        acquired = (await client.post("/v1/vaults/main/lease/acquire", json=body)).json()
        assert acquired["collection_store_head"] == genesis
        body["fencing_token"] = acquired["fencing_token"]
        now[0] += 31
        for operation in ("renew", "release"):
            expired = await client.post(
                f"/v1/vaults/main/lease/{operation}",
                json={**body, "collection_store_head": STORE_HEAD},
            )
            assert expired.json()["granted"] is False
            assert expired.json()["collection_store_head"] == genesis
        status = (await client.get("/v1/vaults/main/lease")).json()
        assert status["holder"] is None
        assert status["collection_store_head"] == genesis
        acquired = (await client.post("/v1/vaults/main/lease/acquire", json=body)).json()
        operator_release = await client.post(
            "/v1/vaults/main/lease/release",
            json={"replica_id": "writer", "fencing_token": acquired["fencing_token"]},
        )
        assert operator_release.json()["collection_store_head"] == genesis
    reopened = create_app(database=database, clock=lambda: now[0])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=reopened), base_url="https://coordinator.example"
    ) as client:
        status = (await client.get("/v1/vaults/main/lease")).json()
        assert status["collection_store_head"] == genesis
        assert status["collection_store_fence_generation"] == 1


def test_renew_waiting_past_expiry_preserves_the_committed_store_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queued renewal cannot replace the head after its holder's lease expires."""
    now = [100.0]
    database = tmp_path / "coordinator.sqlite"
    store = SQLiteLeaseStore(database, clock=lambda: now[0])
    store.transition_collection_store_fence(
        "main", expected_generation=0, capability=STORE_CAPABILITY, store_id=STORE_ID
    )
    acquired = store.acquire("main", "writer", 30, collection_store_capability=STORE_CAPABILITY)
    lease = {
        "vault_id": "main",
        "replica_id": "writer",
        "fencing_token": acquired["fencing_token"],
        "ttl_seconds": 30,
        "collection_store_capability": STORE_CAPABILITY,
    }
    assert store.renew(**lease, collection_store_head=STORE_HEAD)["granted"] is True
    attempted = threading.Event()
    connect = store._connect

    def traced_connect() -> sqlite3.Connection:
        conn = connect()
        conn.set_trace_callback(
            lambda sql: attempted.set() if sql == "BEGIN IMMEDIATE" else None
        )
        return conn

    monkeypatch.setattr(store, "_connect", traced_connect)
    with ThreadPoolExecutor(max_workers=1) as executor:
        blocker = sqlite3.connect(database, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            renewal = executor.submit(
                store.renew, **lease, collection_store_head={**STORE_HEAD, "commit_seq": 2}
            )
            assert attempted.wait(5), "renewal did not reach the held transaction"
            with pytest.raises(TimeoutError):
                renewal.result(timeout=0.05)
            now[0] = 131.0
        finally:
            blocker.close()
        result = renewal.result(timeout=5)

    assert (result["granted"], result["collection_store_head"]) == (False, STORE_HEAD)
    assert store.status("main")["collection_store_head"] == STORE_HEAD


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


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path", ["/v1/state/main/list-keys", "/v1/vaults/main/schema-fence/admit"], ids=["lease", "operator"]
)
async def test_non_ascii_bearer_is_refused_not_a_server_error(tmp_path: Path, path: str) -> None:
    app = create_app(
        database=tmp_path / "coordinator.sqlite",
        bearer_token="lease-secret",
        operator_token="operator-secret",
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="https://coordinator.example") as client:
        response = await client.post(
            path, json={"collection": "auth"}, headers=[(b"authorization", b"Bearer \xe9abc")]
        )

    assert response.status_code in (401, 403), response.text


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="Linux descriptor accounting")
def test_coordinator_operations_keep_bounded_descriptors_and_rollback(tmp_path):
    """Coordinator traffic must not consume descriptors until cyclic GC happens."""
    import resource

    from exomem.lease_coordinator import SQLiteStateStore

    leases = SQLiteLeaseStore(tmp_path / "leases.sqlite")
    now = [0.0]
    state = SQLiteStateStore(tmp_path / "state.sqlite", clock=lambda: now[0])
    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_bytes(b"not a SQLite database")
    before = len(list(Path("/proc/self/fd").iterdir()))
    limits = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(limits[0], before + 64), limits[1]))
        for index in range(128):
            acquired = leases.acquire("vault", "writer", 30)
            assert acquired["granted"]
            assert leases.status("vault")["holder"] == "writer"
            leases.release("vault", "writer", acquired["fencing_token"])
            state.put("tenant", "tokens", "key", {"index": index}, 1)
            assert state.get("tenant", "tokens", "key")[0] == {"index": index}
            now[0] += 2
            # Deleting an expired token then failing serialization must roll back both changes.
            with pytest.raises(TypeError):
                state.put_if_absent("tenant", "tokens", "key", {"invalid": set()}, None)
            now[0] -= 2
            assert state.get("tenant", "tokens", "key")[0] == {"index": index}
            for constructor in (SQLiteLeaseStore, SQLiteStateStore):
                with pytest.raises(sqlite3.DatabaseError):
                    constructor(corrupt)
            assert len(list(Path("/proc/self/fd").iterdir())) <= before + 4
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, limits)
