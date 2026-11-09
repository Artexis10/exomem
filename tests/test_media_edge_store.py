"""The cell's edge store maps edge answers onto store errors and round-trips originals."""

from __future__ import annotations

import hashlib
import io
import json
import socket
from pathlib import Path

import httpx
import pytest

from exomem import media_store, originals
from exomem.media_edge_protocol import (
    COLLECTOR_AUDIENCE,
    OBJECTS_PATH,
    STATUS_MISMATCH,
    STATUS_TOO_LARGE,
)
from exomem.media_edge_store import CREDENTIAL_FILE_ENV, URL_ENV, EdgeStore
from exomem.media_store import MediaStoreError, ObjectTooLarge
from tests.test_media_edge import CELL_A, Harness
from tests.test_media_store import _offload, _preserved, selected_store  # noqa: F401
from tests.test_upload_sessions import _listening

DATA = b"ciphertext bytes"
SHA256 = "0" * 64


def _stub(admit: tuple[int, dict], put: tuple[int, dict]):
    async def app(scope, receive, send):
        status, body = admit if scope["path"].endswith("/admit") else put
        payload = b"" if status == 204 else json.dumps(body).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]})
        await send({"type": "http.response.body", "body": payload})

    return app


def _store(host: str, port: int, tmp_path: Path) -> EdgeStore:
    credential = tmp_path / "credential"
    credential.write_text("token")
    return EdgeStore(f"http://{host}:{port}", credential, connect_seconds=2, read_seconds=5)


def _upload(store: EdgeStore) -> str:
    return store.upload("1" * 64, io.BytesIO(DATA), size=len(DATA), sha256=hashlib.sha256(DATA).hexdigest())


@pytest.mark.parametrize(("admit", "put", "code"), [
    ((204, {}), (STATUS_MISMATCH, {"code": "MEDIA_UPLOAD_MISMATCH"}), "MEDIA_UPLOAD_MISMATCH"),
    ((STATUS_TOO_LARGE, {"bound": 7}), (201, {"version": "v"}), "MEDIA_OBJECT_TOO_LARGE"),
    ((STATUS_TOO_LARGE, {}), (201, {"version": "v"}), "MEDIA_STORE_UNAVAILABLE"),
    ((204, {}), (500, {}), "MEDIA_STORE_UNAVAILABLE"),
    ((503, {}), (201, {"version": "v"}), "MEDIA_STORE_UNAVAILABLE"),
    ((401, {}), (201, {"version": "v"}), "MEDIA_STORE_UNAVAILABLE"),
    ((204, {}), (201, {}), "MEDIA_STORE_UNAVAILABLE"),
])
def test_an_edge_refusal_maps_to_the_stores_own_error(tmp_path: Path, admit, put, code):
    with _listening(_stub(admit, put)) as (host, port):
        with pytest.raises(MediaStoreError) as refused:
            _upload(_store(host, port, tmp_path))
    assert refused.value.code == code
    assert isinstance(refused.value, ObjectTooLarge) == (code == "MEDIA_OBJECT_TOO_LARGE")


def test_an_oversized_refusal_states_the_edges_bound(tmp_path: Path):
    with _listening(_stub((STATUS_TOO_LARGE, {"bound": 7}), (201, {"version": "v"}))) as (host, port):
        with pytest.raises(ObjectTooLarge, match=r"per-object bound of 7"):
            _upload(_store(host, port, tmp_path))


def test_an_unreachable_edge_is_an_outage(tmp_path: Path):
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
    with pytest.raises(MediaStoreError) as outage:
        _upload(_store("127.0.0.1", port, tmp_path))
    assert outage.value.code == "MEDIA_STORE_UNAVAILABLE"


def test_offload_evict_and_hydrate_round_trip_through_the_edge(vault: Path, selected_store, tmp_path: Path, monkeypatch):  # noqa: F811
    values, config = selected_store
    harness = Harness(tmp_path / "edge", max_object_bytes=10_000_000)
    credential = tmp_path / "credential"
    credential.write_text(harness.credential(CELL_A))
    rewritten = {key: value for key, value in values.items() if key != "root"} | {"adapter": "media-edge"}
    config.write_text(json.dumps({"vaults": {str(vault): rewritten}}))
    monkeypatch.setenv(CREDENTIAL_FILE_ENV, str(credential))
    with _listening(harness.edge) as (host, port):
        monkeypatch.setenv(URL_ENV, f"http://{host}:{port}")
        path = _preserved(vault)
        source = path.read_bytes()
        reference = _offload(vault, path)
        stored = harness.fake.files[reference.version]
        assert stored["name"] == f"cells/{CELL_A}/{reference.object}"
        assert stored["data"].startswith(b"age-encryption.org/v1\n")
        assert media_store.evict(vault, path)
        with media_store.open_original(vault, path) as original:
            assert original.stream.read() == source


def test_a_condemned_version_is_never_evicted_on_or_reused_and_its_bytes_go_up_again(
        vault: Path, selected_store, tmp_path: Path, monkeypatch):  # noqa: F811
    """A restored or exported cell can hold an original whose name a later hold condemned. Evicting on that version,
    or reusing it for a duplicate, would leave the bytes only in a version that the bucket rule deletes."""
    values, config = selected_store
    harness = Harness(tmp_path / "edge", max_object_bytes=10_000_000)
    credential = tmp_path / "credential"
    credential.write_text(harness.credential(CELL_A))
    rewritten = {key: value for key, value in values.items() if key != "root"} | {"adapter": "media-edge"}
    config.write_text(json.dumps({"vaults": {str(vault): rewritten}}))
    monkeypatch.setenv(CREDENTIAL_FILE_ENV, str(credential))
    with _listening(harness.edge) as (host, port):
        monkeypatch.setenv(URL_ENV, f"http://{host}:{port}")
        path = _preserved(vault, category="restored")
        source = path.read_bytes()
        condemned = _offload(vault, path)
        hold = httpx.post(f"http://{host}:{port}{OBJECTS_PATH}{condemned.object}/condemn", headers={
            "authorization": f"Bearer {harness.credential(CELL_A, aud=COLLECTOR_AUDIENCE)}"}, trust_env=False)
        assert hold.json() == {"hidden": True}

        duplicate = _preserved(vault, category="duplicate")
        assert _offload(vault, duplicate).object != condemned.object

        assert not media_store.evict(vault, path)
        assert path.read_bytes() == source
        assert originals.original_state(vault, path).reference is None
        again = _offload(vault, path)
        assert again.object != condemned.object

        assert condemned.version in harness.fake.lifecycle_tick(harness.fake.hiding_days)
        for original in (path, duplicate):
            assert media_store.evict(vault, original)
            with media_store.open_original(vault, original) as hydrated:
                assert hydrated.stream.read() == source


@pytest.mark.parametrize("status", [None, "unknown"])
def test_a_head_without_a_known_status_is_an_outage(tmp_path: Path, status):
    """A rolled-back edge answers no status; read as current, it would let eviction trust a condemned version."""
    async def app(scope, receive, send):
        headers = [(b"x-exomem-size", b"16"), (b"x-exomem-sha256", SHA256.encode()), (b"content-length", b"0")]
        if status is not None:
            headers.append((b"x-exomem-status", status.encode()))
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": b""})

    with _listening(app) as (host, port):
        with pytest.raises(MediaStoreError) as outage:
            _store(host, port, tmp_path).head("1" * 64, "version")
    assert outage.value.code == "MEDIA_STORE_UNAVAILABLE"
