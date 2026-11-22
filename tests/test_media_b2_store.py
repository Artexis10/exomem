"""Native B2 replica custody over loopback HTTP with real age encryption."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import time
from pathlib import Path

import httpx
import pytest

from exomem import (
    media_b2_store,
    media_edge_b2,
    media_replicas,
    media_store,
    original_fixity,
    originals,
)
from exomem.media_b2_store import B2ReplicaStore
from tests.fake_b2 import FakeB2
from tests.test_media_store import _offload
from tests.test_media_store import selected_store as selected_store
from tests.test_original_fixity import DAMAGED, TRANSCRIPT, _fixity_on, _stored  # noqa: F401
from tests.test_upload_sessions import _listening

MIRROR = "vault-replica"
PREFIX = "custody/archive/"
OBJECT = "1" * 64
DATA = b"small encrypted test object"
SHA256 = hashlib.sha256(DATA).hexdigest()


@pytest.fixture
def b2(tmp_path: Path, monkeypatch):
    fake = FakeB2(minimum_part=128)
    fake.name_prefix = PREFIX
    faults = {}
    credential = tmp_path / "b2-credential.json"
    credential.write_text(json.dumps({"key_id": "fake-key", "application_key": "fake-only"}))
    config = {"name": MIRROR, "adapter": "b2", "bucket": fake.bucket, "bucket_id": fake.bucket_id,
              "prefix": PREFIX, "credential_file": str(credential), "failure_domain": "independent",
              "retrieval_class": "immediate", "head_recomputes": False}

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return
            body.extend(message["body"])
            if not message.get("more_body"):
                break
        path = scope["raw_path"]
        if scope["query_string"]:
            path += b"?" + scope["query_string"]
        url = httpx.URL(fake.api_url).copy_with(raw_path=path)
        request = httpx.Request(scope["method"], url, headers=scope["headers"], content=bytes(body))
        try:
            reply = await fake._handle(request)
        except httpx.TransportError:
            # A gateway lost the reply after dispatch. The client cannot tell if the provider acted.
            reply = httpx.Response(503, json={"code": "upstream_reply_lost"})
        if fake.calls and (fault := faults.get(fake.calls[-1])) is not None:
            reply = await fault(request, reply)
        data = await reply.aread()
        await send({"type": "http.response.start", "status": reply.status_code, "headers": reply.headers.raw})
        await send({"type": "http.response.body", "body": b"" if request.method == "HEAD" else data})

    with _listening(app) as (host, port):
        fake.api_url = fake.download_url = fake.upload_url = f"http://{host}:{port}"
        monkeypatch.setattr(media_edge_b2, "AUTHORIZE_URL", f"{fake.api_url}/b2api/v4/b2_authorize_account")
        yield fake, config, faults


def _declare(vault: Path, selected_store, config) -> None:
    values, path = selected_store
    values["stores"] = [config]
    path.write_text(json.dumps({"vaults": {str(vault): values}}))


def _upload(store: B2ReplicaStore, data: bytes = DATA) -> str:
    return store.upload(OBJECT, io.BytesIO(data), size=len(data), sha256=hashlib.sha256(data).hexdigest())


@pytest.mark.parametrize("multipart", [False, True])
def test_the_worker_restores_the_recorded_version_after_an_interrupted_upload(
    vault: Path, selected_store, b2, monkeypatch, multipart: bool,
):
    fake, config, _ = b2
    _declare(vault, selected_store, config)
    # Select the multipart branch with a real encrypted original and tiny native parts.
    monkeypatch.setattr(media_edge_b2, "DEFAULT_PART_BYTES", 128 if multipart else 1024)
    path = _stored(vault, "recording")
    primary = _offload(vault, path)
    if multipart:
        fake.lose("upload_part", "reply")
        fake.lose("b2_finish_large_file", "reply")
    else:
        fake.lose("upload_file", "reply")

    media_replicas.request(vault, MIRROR)
    while media_replicas.work(vault):
        pass
    assert media_replicas.progress(vault, MIRROR)["copied"] == 1
    [replica] = originals.original_state(vault, path).replicas
    stored = fake.files[replica.version]
    assert stored["name"] == PREFIX + replica.object
    assert stored["data"].startswith(b"age-encryption.org/v1\n")
    assert TRANSCRIPT not in stored["data"]
    assert (len(stored["data"]), hashlib.sha256(stored["data"]).hexdigest()) == (
        replica.ciphertext_size, replica.ciphertext_sha256,
    )
    if multipart:
        assert fake.parts_sent[:3] == [(replica.version, 1), (replica.version, 1), (replica.version, 2)]
        assert fake.calls.count("b2_finish_large_file") == 1
    else:
        assert fake.calls.count("upload_file") == 1
        assert fake.calls.count("head_name") == 1
    assert "b2_cancel_large_file" not in fake.calls

    # A later version of the same provider name has different bytes.
    store = media_store.store_for(vault, MIRROR)
    latest = store.upload(replica.object, io.BytesIO(DATA), size=len(DATA), sha256=SHA256)
    assert latest != replica.version
    assert fake.current(stored["name"]) is fake.files[latest]
    original_fixity.walk(vault)
    original_fixity.run_pass(vault)
    path.write_bytes(DAMAGED)
    (Path(selected_store[0]["root"]) / primary.object / primary.version).write_bytes(b"damaged")
    assert original_fixity.run_pass(vault, budget=0).outcomes == {"restored": 1}
    assert path.read_bytes() == TRANSCRIPT
    assert "download" in fake.calls


@pytest.mark.parametrize("mismatch", ["bucket", "bucket_id", "prefix", "read", "write", "delete"])
def test_wrong_scope_refuses_a_copy_before_opening_the_original(vault: Path, selected_store, b2, monkeypatch, mismatch):
    fake, config, _ = b2
    if mismatch in ("bucket", "bucket_id", "prefix"):
        config[mismatch] = "wrong"
    elif mismatch == "read":
        fake.capabilities.remove("readFiles")
    elif mismatch == "write":
        fake.capabilities.remove("writeFiles")
    else:
        fake.capabilities.append("deleteFiles")
    _declare(vault, selected_store, config)
    path = _stored(vault, "recording")
    monkeypatch.setattr(media_store, "_open_local_original", lambda *args: pytest.fail("the original was opened"))
    with pytest.raises(media_store.CopyStoreUnavailable):
        media_store.replicate(vault, path, MIRROR)
    assert fake.calls == ["authorize"]
    assert originals.original_state(vault, path).replicas == ()


def test_a_lost_small_request_does_not_reuse_another_attempts_version(b2):
    fake, config, _ = b2
    store = B2ReplicaStore.from_configuration(config)
    older = _upload(store)
    fake.lose("upload_file", "request")
    with pytest.raises(media_store.MediaStoreError, match="MEDIA_STORE_UNAVAILABLE"):
        _upload(store)
    assert list(fake.files) == [older]
    assert fake.calls.count("head_name") == 1


def test_an_unfinished_or_mismatched_large_file_never_becomes_a_replica(b2, monkeypatch):
    fake, config, _ = b2
    monkeypatch.setattr(media_edge_b2, "DEFAULT_PART_BYTES", 128)
    store = B2ReplicaStore.from_configuration(config)
    large = DATA * 12
    fake.lose("b2_finish_large_file", "request")
    with pytest.raises(media_store.MediaStoreError, match="MEDIA_STORE_UNAVAILABLE"):
        _upload(store, large)
    with pytest.raises(media_store.MediaStoreError, match="MEDIA_UPLOAD_MISMATCH"):
        store.upload(OBJECT, io.BytesIO(large), size=len(large), sha256=SHA256)
    assert len(fake.names("start")) == 2
    assert not fake.names()
    assert fake.calls.count("b2_finish_large_file") == 1
    assert "b2_cancel_large_file" not in fake.calls


@pytest.mark.parametrize(("endpoint", "field", "size"), [
    ("b2_get_upload_url", "bucketId", 100),
    ("b2_start_large_file", "fileName", 400),
    ("b2_get_upload_part_url", "fileId", 400),
])
def test_a_wrong_upload_target_never_receives_replica_bytes(b2, monkeypatch, endpoint, field, size):
    fake, config, faults = b2
    monkeypatch.setattr(media_edge_b2, "DEFAULT_PART_BYTES", 128)

    async def wrong_target(request, reply):
        return httpx.Response(200, json=reply.json() | {field: "wrong"})

    faults[endpoint] = wrong_target
    with pytest.raises(media_store.MediaStoreError, match="MEDIA_STORE_UNAVAILABLE"):
        _upload(B2ReplicaStore.from_configuration(config), b"x" * size)
    assert fake.upload_bytes == 0


def test_a_large_upload_rounds_the_minimum_part_up_without_exceeding_the_cap(b2, monkeypatch):
    fake, config, _ = b2
    monkeypatch.setattr(media_edge_b2, "DEFAULT_PART_BYTES", 128)
    monkeypatch.setattr(media_edge_b2, "MAX_PARTS", 3)
    monkeypatch.setattr(media_b2_store, "_MAX_PART_BYTES", 256)
    store = B2ReplicaStore.from_configuration(config)
    data = b"x" * 721
    version = _upload(store, data)
    assert [len(part) for part in fake.files[version]["parts"].values()] == [241, 241, 239]
    assert fake.files[version]["data"] == data
    fake.calls.clear()
    with pytest.raises(media_store.ObjectTooLarge):
        _upload(store, b"x" * 769)
    assert fake.calls == []


def test_only_the_native_absence_envelope_proves_a_version_missing(b2):
    fake, config, faults = b2
    store = B2ReplicaStore.from_configuration(config)
    missing = "4_zmissing"
    assert store.head(OBJECT, missing) is None
    with pytest.raises(media_store.ReplicaAbsent), store.open(OBJECT, missing):
        pytest.fail("a missing version returned a stream")

    async def intermediary(request, reply):
        return httpx.Response(404, text="Not Found")

    faults["b2_get_file_info"] = faults["download"] = intermediary
    with pytest.raises(media_store.MediaStoreError, match="MEDIA_STORE_UNAVAILABLE") as head:
        store.head(OBJECT, missing)
    with pytest.raises(media_store.MediaStoreError, match="MEDIA_STORE_UNAVAILABLE") as read, store.open(OBJECT, missing):
        pytest.fail("an intermediary's 404 returned a stream")
    assert not isinstance(head.value, media_store.ReplicaAbsent)
    assert not isinstance(read.value, media_store.ReplicaAbsent)
    assert fake.calls.count("b2_get_file_info") == 2


def test_an_authorization_error_cannot_prove_a_version_missing(b2):
    fake, config, faults = b2
    store = B2ReplicaStore.from_configuration(config)

    async def failed_authorization(request, reply):
        return httpx.Response(404, json={"status": 404, "code": "not_found"})

    faults["authorize"] = failed_authorization
    with pytest.raises(media_store.MediaStoreError, match="MEDIA_STORE_UNAVAILABLE"):
        store.head(OBJECT, "4_zmissing")
    assert fake.calls == ["authorize"]


def test_reauthorization_rechecks_the_bucket_scope(b2):
    fake, config, _ = b2
    store = B2ReplicaStore.from_configuration(config)
    version = _upload(store)
    fake.expire_authorization()
    fake.name_prefix = "another-prefix/"
    fake.calls.clear()
    with pytest.raises(media_store.MediaStoreError, match="MEDIA_STORE_UNAVAILABLE"):
        store.head(OBJECT, version)
    assert fake.calls == ["unauthorized", "authorize"]


@pytest.mark.parametrize("field", ["bucketId", "fileId", "fileName", "action"])
def test_metadata_needs_the_exact_completed_version(b2, field: str):
    _, config, faults = b2
    store = B2ReplicaStore.from_configuration(config)
    version = _upload(store)

    async def wrong_identity(request, reply):
        return httpx.Response(200, json=reply.json() | {field: "wrong"})

    faults["b2_get_file_info"] = wrong_identity
    with pytest.raises(media_store.MediaStoreError, match="exact completed version"):
        store.head(OBJECT, version)


@pytest.mark.parametrize("fault", ["full", "shifted", "file-id", "name", "length"])
def test_a_proper_range_refuses_incompatible_responses_before_yielding_bytes(b2, fault: str):
    fake, config, faults = b2
    store = B2ReplicaStore.from_configuration(config)
    version = _upload(store)
    with store.open_range(OBJECT, version, size=len(DATA), start=1, end=4) as source:
        assert source.read() == DATA[1:5]
    with store.open_range(OBJECT, version, size=len(DATA), start=0, end=len(DATA) - 1) as source:
        assert source.read() == DATA

    async def change(request, reply):
        if fault == "full":
            headers = dict(reply.headers)
            del headers["content-range"]
            headers["content-length"] = str(len(DATA))
            return httpx.Response(200, headers=headers, content=DATA)
        changed = {
            "shifted": ("content-range", f"bytes 2-5/{len(DATA)}"),
            "file-id": ("x-bz-file-id", "wrong"),
            "name": ("x-bz-file-name", "wrong"),
            "length": ("content-length", "3"),
        }
        header, value = changed[fault]
        reply.headers[header] = value
        if fault == "length":
            return httpx.Response(206, headers=reply.headers, content=DATA[1:4])
        return reply

    faults["download"] = change
    with (pytest.raises(media_store.MediaStoreError, match="MEDIA_STORE_UNAVAILABLE"),
          store.open_range(OBJECT, version, size=len(DATA), start=1, end=4)):
        pytest.fail("a wrong range returned a stream")
    assert fake.calls.count("download") == 3


def test_a_truncated_body_with_correct_headers_fails_the_download(b2):
    _, config, faults = b2
    store = B2ReplicaStore.from_configuration(config)
    version = _upload(store)

    async def truncate(request, reply):
        return httpx.Response(200, headers=reply.headers, content=DATA[:-1])

    faults["download"] = truncate
    with store.open(OBJECT, version, expected_size=len(DATA)) as source:
        with pytest.raises(media_store.MediaStoreError, match="MEDIA_STORE_UNAVAILABLE"):
            source.read()


@pytest.mark.parametrize("operation", ["head", "open"])
def test_a_delayed_provider_answer_obeys_the_deadline(b2, operation: str):
    _, config, faults = b2
    store = B2ReplicaStore.from_configuration(config)
    version = _upload(store)

    async def delay(request, reply):
        await asyncio.sleep(1.0)
        return reply

    faults["b2_get_file_info"] = faults["download"] = delay
    started = time.monotonic()
    with pytest.raises(media_store.MediaStoreError, match="MEDIA_STORE_UNAVAILABLE"):
        if operation == "head":
            store.head(OBJECT, version, deadline=started + 0.15)
        else:
            with store.open(OBJECT, version, deadline=started + 0.15):
                pytest.fail("a late provider returned a stream")
    assert time.monotonic() - started < 0.75


def test_a_worker_round_uses_one_authorization_per_credential_and_one_exact_read_per_row(
    vault: Path, selected_store, b2,
):
    fake, config, _ = b2
    _declare(vault, selected_store, config)
    references = [media_store.replicate(vault, _stored(vault, f"recording-{index}"), MIRROR) for index in range(3)]
    assert all(reference.version in fake.files for reference in references)
    # The scheduled worker builds a fresh adapter for each row after the credential rotates.
    Path(config["credential_file"]).write_text(json.dumps({"key_id": "rotated-key", "application_key": "fake-only"}))
    fake.calls.clear()
    assert media_replicas.check(vault) == len(references)
    assert fake.calls == ["authorize", *["b2_get_file_info"] * len(references)]


def test_a_content_sample_detects_damage_that_a_presence_check_cannot(
    vault: Path, selected_store, b2,
):
    fake, config, _ = b2
    _declare(vault, selected_store, config)
    path = _stored(vault, "recording")
    replica = media_store.replicate(vault, path, MIRROR)
    item = fake.files[replica.version]
    item["data"] = item["data"][:-1] + bytes([item["data"][-1] ^ 1])
    assert media_replicas.check(vault) == 1
    relative = path.relative_to(vault).as_posix()
    [presence] = media_replicas.original_view(vault, relative)["replicas"]
    assert presence["last_check"]["level"] == media_replicas.PRESENCE

    assert media_replicas.sample(vault) == 1
    assert media_replicas.has_quarantined(vault, (replica,))
