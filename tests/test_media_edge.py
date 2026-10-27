"""The media edge enforces its cell boundary and keeps uncertain writes open (design D6)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from exomem.media_edge import EdgeConfig, MediaEdge
from exomem.media_edge_b2 import B2
from exomem.media_edge_protocol import (
    AUDIENCE,
    GENERATION_PATH,
    OBJECTS_PATH,
    PENDING_PATH,
    SHA256_HEADER,
    SIZE_HEADER,
)
from tests.fake_b2 import FakeB2

CELL_A = "a" * 16
CELL_B = "b" * 16
BOUND = 100


class Harness:
    def __init__(self, tmp_path: Path, **config):
        self.tmp_path = tmp_path
        tmp_path.mkdir(exist_ok=True)
        self.signing_key = Ed25519PrivateKey.generate()
        self.public_key_file = tmp_path / "edge.pub"
        self.public_key_file.write_bytes(self.signing_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        self.active_set_file = tmp_path / "active.json"
        self.fake = FakeB2(minimum_part=1000)
        self.config = EdgeConfig(
            public_key_file=self.public_key_file, active_set_file=self.active_set_file, bucket=self.fake.bucket,
            b2_key_id="key", b2_application_key="secret", max_object_bytes=config.pop("max_object_bytes", BOUND),
            state_dir=tmp_path / "state", **config)
        self.publish(1, {CELL_A: 1, CELL_B: 1})
        self.edge = self.build()

    def build(self) -> MediaEdge:
        client = httpx.AsyncClient(transport=self.fake.transport())
        return MediaEdge(self.config, B2(client, key_id="key", application_key="secret", bucket=self.fake.bucket))

    def publish(self, generation: int, cells: dict[str, int]) -> None:
        self.active_set_file.write_text(json.dumps({"generation": generation, "cells": cells}))

    def credential(self, cell: str = CELL_A, version: int = 1, **overrides) -> str:
        claims = {"v": 1, "aud": AUDIENCE, "sub": cell, "ver": version, "issued_ms": int(time.time() * 1000)}
        claims.update(overrides)
        return jwt.encode({k: v for k, v in claims.items() if v is not None}, self.signing_key, algorithm="EdDSA")

    def request(self, method: str, path: str, *, cell: str = CELL_A, token: str | None = None,
                headers: dict[str, str] | None = None, content: bytes | None = None, edge: MediaEdge | None = None):
        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=edge or self.edge)
            async with httpx.AsyncClient(transport=transport, base_url="http://edge") as client:
                return await client.request(method, path, content=content, headers={
                    "authorization": f"Bearer {token or self.credential(cell)}", **(headers or {})})

        return asyncio.run(send())

    def put(self, data: bytes, *, name: str | None = None, **kwargs):
        name = name or hashlib.sha256(data).hexdigest()
        return self.request("PUT", f"{OBJECTS_PATH}{name}", content=data, headers=declaration(data), **kwargs)

    def generation(self, edge: MediaEdge | None = None) -> int:
        response = asyncio.run(self._plain(GENERATION_PATH, edge or self.edge))
        assert response.status_code == 200
        return response.json()["generation"]

    async def _plain(self, path: str, edge: MediaEdge) -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=edge), base_url="http://edge") as client:
            return await client.get(path)


def declaration(data: bytes) -> dict[str, str]:
    return {SIZE_HEADER: str(len(data)), SHA256_HEADER: hashlib.sha256(data).hexdigest(),
            "content-length": str(len(data))}


def name_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_the_prefix_comes_from_the_credential_alone(harness: Harness):
    data = b"ciphertext"
    smuggled = {"x-exomem-prefix": f"cells/{CELL_B}/", "x-exomem-cell": CELL_B}
    response = harness.request("PUT", f"{OBJECTS_PATH}{name_of(data)}?prefix=cells/{CELL_B}/&cell={CELL_B}",
                               content=data, headers={**declaration(data), **smuggled})
    assert response.status_code == 201
    assert harness.fake.names() == [f"cells/{CELL_A}/{name_of(data)}"]


def test_a_prefix_in_the_path_reaches_no_object(harness: Harness):
    data = b"ciphertext"
    response = harness.request("PUT", f"{OBJECTS_PATH}cells/{CELL_B}/{name_of(data)}", content=data,
                               headers=declaration(data))
    assert response.status_code == 404
    assert harness.fake.calls == []


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_another_cells_object_is_refused(harness: Harness, method: str):
    data = b"cell a original"
    version = harness.put(data).json()["version"]
    own = harness.request(method, f"{OBJECTS_PATH}{name_of(data)}?version={version}")
    assert own.status_code == 200
    other = harness.request(method, f"{OBJECTS_PATH}{name_of(data)}?version={version}", cell=CELL_B)
    assert other.status_code == 404


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_a_cross_cell_version_id_gives_not_found_for_a_name_both_cells_hold(harness: Harness, method: str):
    data = b"the same ciphertext name"
    version_a = harness.put(data).json()["version"]
    assert harness.put(data, cell=CELL_B).status_code == 201
    response = harness.request(method, f"{OBJECTS_PATH}{name_of(data)}?version={version_a}", cell=CELL_B)
    assert response.status_code == 404


def test_a_rotated_cells_old_credential_is_refused(harness: Harness):
    old = harness.credential(CELL_A, version=1)
    harness.publish(2, {CELL_A: 2, CELL_B: 1})
    assert harness.put(b"data", token=old).status_code == 401
    assert harness.put(b"data", token=harness.credential(CELL_A, version=2)).status_code == 201


def test_a_removed_cells_credential_is_refused_and_others_keep_working(harness: Harness):
    harness.publish(2, {CELL_B: 1})
    assert harness.put(b"data").status_code == 401
    assert harness.put(b"data", cell=CELL_B).status_code == 201


@pytest.mark.parametrize("case", ["wrong key", "wrong audience", "hs256 confusion", "missing claim", "wrong version type"])
def test_an_invalid_credential_reaches_no_provider_call(harness: Harness, case: str):
    public = harness.signing_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    claims = {"v": 1, "aud": AUDIENCE, "sub": CELL_A, "ver": 1, "issued_ms": 1}
    token = {
        "wrong key": lambda: jwt.encode(claims, Ed25519PrivateKey.generate(), algorithm="EdDSA"),
        "wrong audience": lambda: harness.credential(aud="another-service"),
        "hs256 confusion": lambda: jwt.encode(claims, public, algorithm="HS256"),
        "missing claim": lambda: harness.credential(ver=None),
        "wrong version type": lambda: harness.credential(ver=True),
    }[case]()
    assert harness.put(b"data", token=token).status_code == 401
    assert harness.fake.calls == []


def test_an_upload_above_the_bound_is_refused_before_any_byte_reaches_the_provider(harness: Harness):
    data = b"x" * (BOUND + 1)
    response = harness.put(data)
    assert response.status_code == 413
    assert response.json()["bound"] == BOUND
    assert harness.fake.upload_bytes == 0
    assert harness.fake.files == {}


def test_an_upload_exactly_at_the_bound_is_admitted(harness: Harness):
    data = b"x" * BOUND
    assert harness.put(data).status_code == 201
    assert harness.fake.upload_bytes == BOUND


@pytest.mark.parametrize(("method", "path", "status"), [
    ("DELETE", f"{OBJECTS_PATH}{'0' * 64}?version=1", 405),
    ("DELETE", f"{OBJECTS_PATH}{'0' * 64}", 405),
    ("POST", f"{OBJECTS_PATH}{'0' * 64}/hide", 404),
    ("POST", f"{OBJECTS_PATH}{'0' * 64}/delete", 404),
    ("GET", OBJECTS_PATH, 404),
    ("GET", f"{OBJECTS_PATH}?prefix=", 404),
    ("GET", "/v1/list", 404),
])
def test_no_hide_list_or_delete_route_exists(harness: Harness, method: str, path: str, status: int):
    assert harness.request(method, path).status_code == status
    assert harness.fake.calls == []


def test_a_restart_keeps_an_uncertain_write_open_until_the_late_write_lands(
        harness: Harness, caplog: pytest.LogCaptureFixture):
    data = b"a write the provider has not applied yet"
    harness.fake.lose("upload_file", "late", then_outage=True)
    assert harness.put(data).status_code == 503
    pending = asyncio.run(harness._plain(PENDING_PATH, harness.edge))
    assert pending.json() == {"pending": 1}

    harness.publish(2, {CELL_A: 1, CELL_B: 1})
    assert harness.generation() == 1
    restarted = harness.build()
    assert harness.generation(restarted) == 1

    harness.fake.outage = False
    caplog.clear()
    asyncio.run(restarted.recover_pending())
    assert harness.generation(restarted) == 1  # the provider has no such version, which proves nothing
    # No provider error names the cell on this pass; the pending report must.
    assert any(CELL_A in record.getMessage() and CELL_B not in record.getMessage()
               for record in caplog.records if record.levelname == "WARNING")

    harness.fake.land_late()
    asyncio.run(restarted.recover_pending())
    assert harness.generation(restarted) == 2
    assert harness.fake.names() == [f"cells/{CELL_A}/{name_of(data)}"]


def test_a_recovery_head_after_the_provider_authorization_expired_still_settles_the_write(harness: Harness):
    # B2 authorizations expire. A HEAD that gave up on the first 401 would hold the drain open.
    data = b"a write whose reply was lost"
    harness.fake.lose("upload_file", "late")
    assert harness.put(data).status_code == 503
    harness.publish(2, {CELL_A: 1, CELL_B: 1})
    harness.fake.land_late()
    harness.fake.expire_authorization()

    asyncio.run(harness.edge.recover_pending())

    assert harness.generation() == 2
    assert asyncio.run(harness._plain(PENDING_PATH, harness.edge)).json() == {"pending": 0}


def test_a_missing_head_after_a_lost_request_keeps_the_write_open(harness: Harness):
    harness.fake.lose("upload_file", "request")
    assert harness.put(b"a request the provider never received").status_code == 503
    harness.publish(2, {CELL_A: 1, CELL_B: 1})
    assert harness.generation() == 1
    assert harness.fake.files == {}


@pytest.mark.parametrize(("size", "endpoint"), [(40, "upload_file"), (2500, "b2_finish_large_file")])
def test_a_lost_upload_reply_recovers_the_exact_version(tmp_path: Path, size: int, endpoint: str):
    harness = Harness(tmp_path, max_object_bytes=10_000, part_bytes=1000)
    data = b"z" * size
    harness.fake.lose(endpoint, "reply")
    response = harness.put(data)
    assert response.status_code == 201
    assert response.json()["version"] == harness.fake.version_of(f"cells/{CELL_A}/{name_of(data)}")
    assert harness.fake.names() == [f"cells/{CELL_A}/{name_of(data)}"]
    assert harness.generation() == 1
    assert asyncio.run(harness._plain(PENDING_PATH, harness.edge)).json() == {"pending": 0}


def test_an_unsettled_upload_holds_its_name_while_other_cells_keep_working(harness: Harness):
    data = b"held until the provider answers"
    harness.fake.lose("upload_file", "late")
    assert harness.put(data).status_code == 503
    assert harness.put(b"another cell", cell=CELL_B).status_code == 201
    assert harness.put(b"another name").status_code == 201
    assert harness.put(data).status_code == 409
    harness.fake.land_late()
    asyncio.run(harness.edge.recover_pending())
    assert harness.put(data).status_code == 201


def test_a_request_accepted_under_a_generation_holds_it_until_the_request_finishes(harness: Harness):
    data = b"0123456789"
    release = asyncio.Event()

    async def body():
        yield data[:5]
        await release.wait()
        yield data[5:]

    async def scenario() -> tuple[int, int, int]:
        transport = httpx.ASGITransport(app=harness.edge)
        async with httpx.AsyncClient(transport=transport, base_url="http://edge") as client:
            upload = asyncio.create_task(client.put(
                f"{OBJECTS_PATH}{name_of(data)}", content=body(),
                headers={"authorization": f"Bearer {harness.credential()}", **declaration(data)}))
            while not harness.edge.inflight:
                await asyncio.sleep(0)
            harness.publish(2, {CELL_A: 1, CELL_B: 1})
            during = (await client.get(GENERATION_PATH)).json()["generation"]
            release.set()
            status = (await upload).status_code
            after = (await client.get(GENERATION_PATH)).json()["generation"]
            return during, status, after

    assert asyncio.run(scenario()) == (1, 201, 2)


@pytest.mark.parametrize("content", [
    b"not json",
    json.dumps({"generation": 0, "cells": {}}).encode(),
    json.dumps({"generation": 1, "cells": {}}).encode(),
    json.dumps({"generation": 2, "cells": {"not-a-cell-id": 1}}).encode(),
])
def test_a_bad_active_set_keeps_the_previous_one(harness: Harness, content: bytes):
    assert harness.generation() == 1
    harness.active_set_file.write_bytes(content)
    assert harness.generation() == 1
    assert harness.put(b"data").status_code == 201


def test_without_an_active_set_the_edge_answers_unavailable(tmp_path: Path):
    harness = Harness(tmp_path)
    harness.active_set_file.unlink()
    harness.edge = harness.build()
    response = asyncio.run(harness._plain(GENERATION_PATH, harness.edge))
    assert response.status_code == 503
    assert harness.put(b"data").status_code == 503
    assert harness.fake.calls == []
