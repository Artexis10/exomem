import hashlib
import io
import json
import os
import shutil
import time
from contextlib import closing, contextmanager
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_media_store import _configure, _offload, selected_store as selected_store
from test_original_fixity import _stored
from test_upload_sessions import _client

from exomem import (
    artifact_lifecycle, held_fs, media_bindings, media_jobs, media_replicas, media_store, original_pool, originals,
    personal_eviction, preserve, state_paths, upload_sessions, upload_stage, vault as vault_files,
)
from exomem.__main__ import _media_replicas_main
from exomem.cli_ops import OpError
from exomem.governance.principal import library_scope


@pytest.fixture
def local_store(vault, selected_store, monkeypatch):
    values, configured = selected_store
    local = state_paths.ensure_vault_state_dir(vault) / "media-store.json"
    local.write_bytes(configured.read_bytes())
    monkeypatch.delenv(originals.CONFIG_ENV)
    root = Path(values["root"])
    identity = media_store.read_store_id(root)
    assert identity is not None
    media_replicas.record_store_id(vault, values["store"], identity)
    return values["store"], local


def test_personal_store_configuration_uses_state_and_honors_the_environment(vault, tmp_path, monkeypatch):
    monkeypatch.delenv(originals.CONFIG_ENV, raising=False)
    monkeypatch.delenv("EXOMEM_CLOUD_CELL", raising=False)
    assert originals.configuration(vault) is None

    local = state_paths.ensure_vault_state_dir(vault) / "media-store.json"
    local.write_text(json.dumps({"vaults": {str(vault): {"store": "local"}}}), encoding="utf-8")
    assert originals.configuration(vault) == {"store": "local"}

    explicit = tmp_path / "explicit.json"
    explicit.write_text(json.dumps({"vaults": {str(vault): {"store": "explicit"}}}), encoding="utf-8")
    monkeypatch.setenv(originals.CONFIG_ENV, str(explicit))
    assert originals.configuration(vault) == {"store": "explicit"}


def test_cloud_without_explicit_store_does_not_read_personal_configuration(vault, monkeypatch):
    monkeypatch.delenv(originals.CONFIG_ENV, raising=False)
    local = state_paths.ensure_vault_state_dir(vault) / "media-store.json"
    local.write_text(json.dumps({"vaults": {str(vault): {"store": "local"}}}), encoding="utf-8")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")

    assert originals.configuration(vault) is None


def test_offload_carries_the_admitted_filesystem_route_through_publication(vault, selected_store, tmp_path, monkeypatch):
    values, config_path = selected_store
    admitted = Path(values["root"])
    replacement = tmp_path / "another-store"
    replacement.mkdir()
    marker = media_store.STORE_MARKER
    (replacement / marker).write_bytes((admitted / marker).read_bytes())
    original_config = config_path.read_bytes()
    open_store = media_store.store_for

    def route_during_a_second_read(root, name=None):
        _configure(selected_store, vault, root=str(replacement))
        try:
            return open_store(root, name)
        finally:
            config_path.write_bytes(original_config)

    original = _stored(vault, "one configuration snapshot")
    with monkeypatch.context() as changed_route:
        changed_route.setattr(media_store, "store_for", route_during_a_second_read)
        reference = _offload(vault, original)
    assert reference is not None
    head = open_store(vault).head(reference.object, reference.version)
    assert head is not None and head.sha256 == reference.ciphertext_sha256
    assert list(replacement.iterdir()) == [replacement / marker]


@pytest.mark.skipif(os.name != "posix", reason="inject a POSIX directory flush failure")
def test_original_stays_local_until_the_store_root_can_make_the_path_durable(vault, selected_store, monkeypatch):
    from exomem._held_fs_posix import PosixHeldFilesystem

    values, _ = selected_store
    folder = Path(values["root"]).stat()
    flush = PosixHeldFilesystem.flush_directory

    def reject_store_root(filesystem, directory):
        if (directory.identity.device, directory.identity.inode) == (folder.st_dev, folder.st_ino):
            return held_fs.HeldResult(error=held_fs.HeldFsError("IO_REFUSED", "store root flush refused"))
        return flush(filesystem, directory)

    original = _stored(vault, "a recording with a durable store path")
    with monkeypatch.context() as pressure:
        pressure.setattr(PosixHeldFilesystem, "flush_directory", reject_store_root)
        with pytest.raises(media_store.MediaStoreError, match="MEDIA_STORE_UNAVAILABLE"):
            media_store.offload(vault, original)
    assert original.is_file() and originals.original_state(vault, original).reference is None

    assert media_store.offload(vault, original) is not None
    media_replicas.confirm_key_export(vault)
    with monkeypatch.context() as pressure:
        pressure.setattr(PosixHeldFilesystem, "flush_directory", reject_store_root)
        assert not media_store.evict(vault, original)
    assert original.is_file()
    assert media_store.evict(vault, original)


@pytest.mark.skipif(os.name != "posix", reason="inject a POSIX directory flush failure")
def test_declaration_flushes_an_existing_marker_before_adopting_it(vault, selected_store, tmp_path, monkeypatch):
    from exomem._held_fs_posix import PosixHeldFilesystem

    values, _ = selected_store
    name = values["store"]
    folder = tmp_path / "second-volume"
    folder.mkdir()
    _configure(selected_store, vault, root=str(folder))
    marker = folder / media_store.STORE_MARKER
    marker_id = "a" * 32
    marker.write_bytes(media_store._store_marker(name, marker_id))
    monkeypatch.setattr(media_store, "_require_distinct_device", lambda *_: None)
    original_flush = PosixHeldFilesystem.flush_directory
    identity = folder.stat()

    def reject_marker_root(filesystem, directory):
        if (directory.identity.device, directory.identity.inode) == (identity.st_dev, identity.st_ino):
            return held_fs.HeldResult(error=held_fs.HeldFsError("IO_REFUSED", "store root flush refused"))
        return original_flush(filesystem, directory)

    with monkeypatch.context() as interrupted:
        interrupted.setattr(PosixHeldFilesystem, "flush_directory", reject_marker_root)
        with pytest.raises(media_store.MediaStoreError, match="MEDIA_STORE_UNAVAILABLE"):
            media_store.declare_store(vault, name)
    assert media_replicas.declared_store_id(vault, name) is None
    assert media_store.declare_store(vault, name) == folder
    assert media_replicas.declared_store_id(vault, name) == marker_id
    assert marker.read_bytes() == media_store._store_marker(name, marker_id)


@pytest.mark.skipif(os.name != "posix", reason="uses a POSIX directory exchange")
def test_eviction_keeps_the_original_if_the_store_moves_during_the_final_probe(vault, selected_store, monkeypatch):
    original = _stored(vault, "file retained after a late store move")
    assert _offload(vault, original) is not None
    media_replicas.confirm_key_export(vault)
    folder = Path(selected_store[0]["root"])
    moved = folder.with_name("moved-store")
    open_version = media_store.FilesystemStore._open_version

    @contextmanager
    def move_during_metadata(store, filesystem, name, version, *, durable=False):
        with open_version(store, filesystem, name, version, durable=durable) as file:
            if not durable:
                folder.rename(moved)
            yield file

    try:
        with monkeypatch.context() as detached:
            detached.setattr(media_store.FilesystemStore, "_open_version", move_during_metadata)
            assert not media_store.evict(vault, original) and original.is_file()
            assert moved.exists()
    finally:
        if moved.exists():
            moved.rename(folder)
    assert media_store.evict(vault, original)


def test_eviction_keeps_the_original_until_its_key_and_publication_are_ready(vault, selected_store):
    _configure(selected_store, vault, key_version="2", eviction_settle_seconds=600)
    original = _stored(vault, "recordings")
    reference = _offload(vault, original)
    assert reference.key_version == "2"
    media_replicas.confirm_key_export(vault, "1")

    assert not media_store.evict(vault, original)
    assert original.is_file()

    media_replicas.confirm_key_export(vault, "2")
    assert not media_store.evict(vault, original)
    assert original.is_file()

    with closing(media_jobs.MediaJobStore(vault)._connect()) as conn, conn:
        conn.execute("UPDATE media_replicas SET verified_at = ? WHERE binary_rel = ?",
                     (time.time() - 1_200, original.relative_to(vault).as_posix()))
    assert media_store.evict(vault, original)
    assert originals.original_state(vault, original).state == "evicted"


def test_a_reused_copy_can_use_the_exact_versions_time_on_another_filing(vault, selected_store):
    media_replicas.confirm_key_export(vault)
    first = _stored(vault, "first")
    second = _stored(vault, "second")
    reference = _offload(vault, first)
    assert _offload(vault, second) == reference
    ledger = media_jobs.MediaJobStore(vault)
    first_rel = first.relative_to(vault).as_posix()
    second_rel = second.relative_to(vault).as_posix()
    with closing(ledger._connect()) as conn, conn:
        assert conn.execute("SELECT verified_at FROM media_replicas WHERE binary_rel = ?", (second_rel,)).fetchone()[0] is None
        conn.execute("UPDATE media_replicas SET verified_at = NULL WHERE binary_rel = ?", (first_rel,))

    assert not media_store.evict(vault, second)
    assert second.is_file()

    with closing(ledger._connect()) as conn, conn:
        conn.execute("UPDATE media_replicas SET verified_at = ? WHERE binary_rel = ?", (time.time() - 60, first_rel))
    assert media_store.evict(vault, second)
    assert first.is_file()
    assert originals.original_state(vault, second).state == "evicted"


@pytest.mark.parametrize("pooled", [False, True])
def test_owner_can_disable_eviction_while_a_verified_removal_is_in_flight(vault, local_store, monkeypatch, capsys, pooled):
    store, _ = local_store
    if pooled:
        monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
        original_pool.enable(vault)
    assert _media_replicas_main([
        "enable-eviction", store, "--vault", str(vault), "--free-bytes", str(2**30),
        "--minimum-idle-seconds", "0", "--eviction-settle-seconds", "0",
    ]) == 0
    assert "with its exported decryption key" in capsys.readouterr().out

    with library_scope():
        filed = preserve.preserve_stream(vault, scope="field", category="samples", filename="recording.txt",
                                        stream=io.BytesIO(b"a recorded measurement"))
    original = vault / filed.path
    assert (originals.original_state(vault, original).local_path is not None) is pooled
    assert _offload(vault, original) is not None
    if pooled:
        assert media_bindings.rebuild(vault)
    head = media_store.FilesystemStore._head_held
    disabled = []

    def during_head(backend, *args, **kwargs):
        proof = head(backend, *args, **kwargs)
        disabled.append(_media_replicas_main(["disable-eviction", "--vault", str(vault)]))
        return proof

    monkeypatch.setattr(media_store.FilesystemStore, "_head_held", during_head)
    assert not media_store.evict(vault, original)
    assert disabled == [0]
    assert originals.original_state(vault, original).state == "cached"
    policy = originals.configuration(vault)
    assert policy["enabled"] is False and policy["eviction_enabled"] is False


@pytest.mark.parametrize("pooled", [False, True])
def test_replacing_the_named_volume_before_unlink_keeps_the_original(
    vault, local_store, monkeypatch, pooled,
):
    store, _ = local_store
    if pooled:
        monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
        original_pool.enable(vault)
    personal_eviction.enable(vault, store, free_bytes=2**30, minimum_idle_seconds=0, eviction_settle_seconds=0)
    with library_scope():
        filed = preserve.preserve_stream(vault, scope="field", category="samples", filename="recording.txt",
                                         stream=io.BytesIO(b"exact retained original"))
    original = vault / filed.path
    pointer = _offload(vault, original)
    assert pointer is not None
    if pooled:
        assert media_bindings.rebuild(vault)
    snapshot = originals.original_state(vault, original)
    head = media_store.FilesystemStore._head_held
    root = Path(originals.configuration(vault)["root"])
    displaced = root.with_name("displaced")

    def exchange_after_hash(backend, *args, **kwargs):
        proof = head(backend, *args, **kwargs)
        root.rename(displaced)
        root.mkdir()
        shutil.copy2(displaced / media_store.STORE_MARKER, root / media_store.STORE_MARKER)
        return proof

    monkeypatch.setattr(media_store.FilesystemStore, "_head_held", exchange_after_hash)
    assert not media_store.evict(vault, original)
    assert displaced.is_dir()
    assert (snapshot.local_path or original).read_bytes() == b"exact retained original"
    assert originals.original_state(vault, original).state == "cached"
    assert not media_replicas.has_quarantined(vault, [pointer])


def test_personal_drain_restores_an_original_that_fits_before_the_rest_can_fit(vault, local_store, monkeypatch):
    store, _ = local_store
    personal_eviction.enable(vault, store, free_bytes=2**30, minimum_idle_seconds=0, eviction_settle_seconds=0)
    small = _stored(vault, "small", b"small")
    large = _stored(vault, "large", b"longer" * 100)
    for original in (small, large):
        assert _offload(vault, original) is not None
        assert media_store.evict(vault, original)
    personal_eviction.disable(vault)

    volumes = shutil.disk_usage(vault)
    config = originals.configuration(vault)
    used = int(config["growth_threshold"] * volumes.total) - int(config["eviction_margin"] * volumes.total) - 10
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: SimpleNamespace(total=volumes.total, used=used, free=volumes.total - used))

    assert media_store.drain_step(vault, claimable=lambda: None) == 1
    assert small.read_bytes() == b"small"
    assert originals.original_state(vault, large).state == "evicted"


@pytest.mark.parametrize("door", ["stage", "preserve", "resumable"])
def test_upload_space_floor_refuses_before_leaving_an_original(vault, selected_store, monkeypatch, door):
    _configure(selected_store, vault, upload_floor_bytes=1_000)
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: SimpleNamespace(total=4_000, used=2_995, free=1_005))
    data = b"a ten byte"  # the configured floor leaves room for five bytes
    with pytest.raises((OpError, upload_sessions.SessionError), match="Retry after eviction frees space") as refused:
        if door == "stage":
            with upload_stage.stage(vault, io.BytesIO(data), limit=100, too_large=ValueError):
                pytest.fail("the staged upload exceeded the vault's room")
        elif door == "preserve":
            with library_scope():
                preserve.preserve_stream(vault, scope="field", category="samples", filename="recording.txt",
                                         stream=io.BytesIO(data))
        else:
            upload_sessions.create(vault, binding="owner", length=len(data), sha256="a" * 64, target={}, max_bytes=100)

    assert refused.value.code == "MEDIA_SPACE_BUSY"
    assert "1,005 free bytes" in str(refused.value)
    assert "1,000-byte floor" in str(refused.value)
    assert not list(upload_stage.stage_root(vault).glob("*.part"))
    assert not list(vault.rglob("recording.txt"))
    assert not list(state_paths.vault_state_dir(vault).rglob("*.part"))


def test_single_request_upload_reports_vault_pressure(vault, selected_store, monkeypatch):
    _configure(selected_store, vault, upload_floor_bytes=1_000)
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: SimpleNamespace(total=4_000, used=2_995, free=1_005))
    client = _client(vault, monkeypatch)
    response = client.request("POST", "/upload", headers={"Authorization": "Bearer sekret"},
                              data={"scope": "field", "category": "samples"},
                              files={"file": ("recording.txt", b"a ten byte")})

    assert response.status_code == 503
    assert response.json()["code"] == "MEDIA_SPACE_BUSY"
    assert "Retry after eviction frees space" in response.json()["reason"]
    assert not list(vault.rglob("recording.txt"))


def test_a_resumable_floor_refusal_keeps_the_earlier_accepted_bytes(vault, selected_store, monkeypatch):
    from exomem import media_capacity

    _configure(selected_store, vault, upload_floor_bytes=1_000)
    block = media_capacity._allocation_unit(vault)
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: SimpleNamespace(total=10 * block, free=5 * block))
    session, secret = upload_sessions.create(
        vault, binding="owner", length=10, sha256="a" * 64,
        target={"lane": "evidence", "filename": "recording.bin"}, max_bytes=100,
    )
    first = upload_sessions.Patch(session, 0, 5)
    first.write(b"first")
    assert first.close() == (5, False)

    # The append fits the allocated block. The stage that filing needs does not.
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: SimpleNamespace(total=10 * block, free=1_003))
    reopened = upload_sessions.open_session(vault, session.id, secret)
    next_part = upload_sessions.Patch(reopened, 5, 5)
    try:
        with pytest.raises(upload_sessions.SessionError) as refused:
            next_part.write(b"again")
        assert refused.value.code == "MEDIA_SPACE_BUSY" and refused.value.status == 503
    finally:
        assert next_part.close() == (5, False)
    assert reopened.part.read_bytes() == b"first"


def test_resumable_commit_retries_space_pressure_without_another_upload_request(vault, selected_store, monkeypatch):
    from exomem import media_capacity, server_transfer

    _configure(selected_store, vault, upload_floor_bytes=1_000)
    unit = media_capacity._allocation_unit(vault)
    free = [8 * unit]
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: SimpleNamespace(total=16 * unit, free=free[0]))
    data = b"A complete original that must stay held until commit can allocate."
    target = {"lane": "evidence", "scope": "field", "category": "samples", "filename": "recording.bin"}
    session, secret = upload_sessions.create(
        vault, binding="owner", length=len(data), sha256=hashlib.sha256(data).hexdigest(), target=target, max_bytes=1024,
    )
    part = upload_sessions.Patch(session, 0, len(data))
    part.write(data)
    assert part.close() == (len(data), True)
    free[0] = 1_000
    upload_sessions.start_commit(session, partial(server_transfer.commit_session, vault))

    limit = time.monotonic() + 15
    while (observed := upload_sessions.open_session(vault, session.id, secret).view())["state"] != upload_sessions.RETRYABLE:
        assert time.monotonic() < limit, observed
        time.sleep(0.02)
    assert observed["code"] == "MEDIA_SPACE_BUSY"
    assert session.part.read_bytes() == data
    free[0] = 8 * unit

    while (observed := upload_sessions.open_session(vault, session.id, secret).view())["state"] != upload_sessions.COMMITTED:
        assert time.monotonic() < limit, observed
        time.sleep(0.02)
    assert (vault / observed["receipt"]["path"]).read_bytes() == data


@pytest.mark.parametrize("code", ["MEDIA_SPACE_BUSY", "SOURCE_DESTINATION_CHANGED"])
def test_an_expired_parked_commit_releases_space_without_another_upload(vault, selected_store, monkeypatch, code):
    from exomem import media_capacity

    _configure(selected_store, vault, upload_floor_bytes=1_000)
    unit = media_capacity._allocation_unit(vault)
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: SimpleNamespace(free=1_000 + 8 * unit))
    monkeypatch.setattr(upload_sessions, "REATTEMPT_INITIAL_SECONDS", 0.02)
    monkeypatch.setattr(upload_sessions, "REATTEMPT_MAX_SECONDS", 0.12)
    data = b"An acknowledged original parks until its session expires."
    target = {"lane": "evidence", "scope": "field", "category": "samples", "filename": "recording.bin"}
    session, _secret = upload_sessions.create(
        vault, binding="owner", length=len(data), sha256=hashlib.sha256(data).hexdigest(), target=target, max_bytes=1024,
    )
    part = upload_sessions.Patch(session, 0, len(data))
    part.write(data)
    monkeypatch.setattr(upload_sessions, "TTL_SECONDS", 1.5)
    assert part.close() == (len(data), True)
    with media_capacity.upload_room(vault) as room:
        assert room.reserved >= unit

    def busy(_part, _record):
        raise upload_sessions.CommitFailed(code, "the original remains held", retryable=True)

    upload_sessions.start_commit(session, busy)
    record = session.store / f"{session.id}.json"
    # A bound, not a latency: the release takes under 2 s locally, but a shared CI runner can stall the timers.
    deadline = time.monotonic() + 30
    while record.exists():
        assert time.monotonic() < deadline
        time.sleep(0.025)
    assert not session.part.exists()
    with media_capacity.upload_room(vault) as room:
        assert room.reserved == 0


def test_a_commit_that_becomes_retryable_does_not_wait_for_its_expiry(vault, monkeypatch):
    monkeypatch.setattr(upload_sessions, "TTL_SECONDS", 5)
    monkeypatch.setattr(upload_sessions, "REATTEMPT_INITIAL_SECONDS", 0.03)
    data = b"A complete original survives a change in the reason its commit waits."
    digest = hashlib.sha256(data).hexdigest()
    target = {"lane": "evidence", "scope": "field", "category": "samples", "filename": "recording.bin"}
    session, secret = upload_sessions.create(
        vault, binding="owner", length=len(data), sha256=digest, target=target, max_bytes=1024,
    )
    part = upload_sessions.Patch(session, 0, len(data))
    part.write(data)
    assert part.close() == (len(data), True)
    failures = iter(("SOURCE_DESTINATION_CHANGED", "MEDIA_SPACE_BUSY"))

    def commit(held, _record):
        if code := next(failures, None):
            raise upload_sessions.CommitFailed(code, "the original remains held", retryable=True)
        return {"hash": hashlib.sha256(held.read_bytes()).hexdigest()}

    upload_sessions.start_commit(session, commit)
    deadline = time.monotonic() + 3
    while upload_sessions._running(session.id).reattempt is None:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    parked = upload_sessions.open_session(vault, session.id, secret)
    assert parked.record["code"] == "SOURCE_DESTINATION_CHANGED"
    upload_sessions.start_commit(parked, commit)
    while (observed := upload_sessions.open_session(vault, session.id, secret).view())["state"] != upload_sessions.COMMITTED:
        assert time.monotonic() < deadline, observed
        time.sleep(0.01)
    assert observed["receipt"] == {"hash": digest}


def test_a_new_owner_floor_applies_to_the_next_chunk_of_an_open_stage(vault, local_store, monkeypatch):
    from exomem import media_capacity

    store, _path = local_store
    floor = 1024**2
    unit = media_capacity._allocation_unit(vault)
    assert media_capacity.upload_floor(vault) is None
    free = [floor * 3]
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: SimpleNamespace(total=8 * floor, free=free[0]))

    class PartInterruptedByPolicy(io.BytesIO):
        def read(self, _length):
            if self.tell() == unit:
                personal_eviction.enable(vault, store, free_bytes=2 * floor, upload_floor_bytes=floor)
                free[0] = floor
            return super().read(unit)

    stream = PartInterruptedByPolicy(b"a" * unit + b"b" * unit)
    with pytest.raises(OpError, match="floor") as refused:
        with upload_stage.stage(vault, stream, limit=2 * unit, too_large=ValueError):
            pytest.fail("the open stage consumed space protected by the newly set floor")
    assert refused.value.code == "MEDIA_SPACE_BUSY"
    assert not list(upload_stage.stage_root(vault).glob("*.part"))


def test_an_old_policy_inventory_failure_keeps_the_current_measurement(vault, local_store, monkeypatch):
    _store, path = local_store
    inventory = media_store._physical_drain_census

    def obsolete_attempt(root, _conn):
        document = json.loads(path.read_bytes())
        document["vaults"][str(vault)]["upload_floor_bytes"] = 0
        path.write_text(json.dumps(document), encoding="utf-8")
        with monkeypatch.context() as current_policy:
            current_policy.setattr(media_store, "_physical_drain_census", inventory)
            assert media_store.drain_census(root, inventory_only=True) == (0, 0)
        raise ValueError("the earlier policy's inventory lost its binding frontier")

    monkeypatch.setattr(media_store, "_physical_drain_census", obsolete_attempt)
    with pytest.raises(media_store.MediaStoreError, match="MEDIA_CENSUS_UNKNOWN"):
        media_store.drain_census(vault, inventory_only=True)
    config = originals.configuration(vault)
    with closing(media_jobs.MediaJobStore(vault)._connect(readonly=True)) as conn:
        status = media_store.personal_eviction_status(conn, config)
    assert status["inventory_state"] == "complete" and status["local"] == {"count": 0, "bytes": 0}


@pytest.mark.parametrize("pooled", [False, True])
def test_a_complete_upload_keeps_its_commit_room_when_an_original_hydrates(vault, selected_store, monkeypatch, pooled):
    from exomem import media_capacity, server_transfer

    if pooled:
        monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
        original_pool.enable(vault)
    with library_scope():
        filed = preserve.preserve_stream(vault, scope="field", category="samples", filename="stored.bin",
                                         stream=io.BytesIO(b"encrypted and recoverable"))
    original = vault / filed.path
    assert _offload(vault, original) is not None
    if pooled:
        assert media_bindings.rebuild(vault)
    media_replicas.confirm_key_export(vault)
    assert media_store.evict(vault, original)

    _configure(selected_store, vault, upload_floor_bytes=1_000)
    unit = media_capacity._allocation_unit(vault)
    free = [8 * unit]
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: SimpleNamespace(total=16 * unit, free=free[0]))
    data = b"The pending upload owns space for its stages."
    target = {"lane": "evidence", "scope": "field", "category": "samples", "filename": "pending.bin"}
    session, secret = upload_sessions.create(
        vault, binding="owner", length=len(data), sha256=hashlib.sha256(data).hexdigest(), target=target, max_bytes=1024,
    )
    part = upload_sessions.Patch(session, 0, len(data))
    part.write(data)
    assert part.close() == (len(data), True)
    free[0] = 1_000 + unit * upload_sessions.commit_copy_count(vault, target)
    with pytest.raises(media_store.MediaStoreError, match="MEDIA_STORE_BUSY"):
        with media_store.open_original(vault, original):
            pytest.fail("hydration spent the upload's commit credit")

    assert originals.original_state(vault, original).state == "evicted"
    upload_sessions.start_commit(session, partial(server_transfer.commit_session, vault))
    limit = time.monotonic() + 15
    while (session.part.exists()
           or upload_sessions.open_session(vault, session.id, secret).view()["state"] != upload_sessions.COMMITTED):
        assert time.monotonic() < limit
        time.sleep(0.02)
    with media_store.open_original(vault, original, physical=True) as reopened:
        assert reopened.stream.read() == b"encrypted and recoverable"


@pytest.mark.parametrize("has_replica", [False, True])
def test_enabling_a_different_store_respects_the_canonical_replica_role(vault, local_store, tmp_path, has_replica):
    primary, path = local_store
    other = tmp_path / "secondary"
    other.mkdir()
    marker = media_store.write_store_marker(other, "secondary")
    media_replicas.record_store_id(vault, "secondary", marker)
    document = json.loads(path.read_bytes())
    config = document["vaults"][str(vault)]
    descriptor = {"failure_domain": "disk", "retrieval_class": originals.IMMEDIATE, "head_recomputes": True}
    config["stores"] = [{"name": primary, **descriptor}, {
        "name": "secondary", "adapter": "filesystem", "root": str(other), **descriptor,
    }]
    config["max_object_bytes"] = 1_000
    path.write_text(json.dumps(document), encoding="utf-8")
    before = path.read_bytes()
    if has_replica:
        original = _stored(vault, "recordings")
        media_store.replicate(vault, original, "secondary")
        with closing(media_jobs.MediaJobStore(vault)._connect()) as conn, conn:
            conn.execute("DELETE FROM media_replicas WHERE store = 'secondary'")
        with pytest.raises(media_store.MediaStoreError, match="already holds a replica"):
            personal_eviction.enable(vault, "secondary", free_bytes=2**30)
        assert path.read_bytes() == before
    else:
        personal_eviction.enable(vault, "secondary", free_bytes=2**30)
        updated = originals.configuration(vault)
        assert updated["store"] == "secondary" and updated["root"] == str(other)
        assert "max_object_bytes" not in updated
        assert originals.store_entry(updated, primary).connection["max_object_bytes"] == 1_000


def test_owner_status_counts_bound_objects_and_reports_the_actual_wait(vault, local_store, monkeypatch):
    store, path = local_store
    document = json.loads(path.read_bytes())
    document["vaults"][str(vault)].update(key_version="2", enabled=False)
    path.write_text(json.dumps(document), encoding="utf-8")
    resident = _stored(vault, "resident", b"resident bytes")
    personal_eviction.enable(vault, store, free_bytes=2**30, minimum_idle_seconds=0, eviction_settle_seconds=0)
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    content = b"one physical copy, two filings"
    with library_scope():
        first = preserve.preserve_stream(vault, scope="field", category="samples", filename="first.txt",
                                         stream=io.BytesIO(content))
        second = preserve.preserve_stream(vault, scope="field", category="samples", filename="second.txt",
                                          stream=io.BytesIO(content))
    for filed in (first, second):
        assert media_store.offload(vault, vault / filed.path) is not None
    assert media_bindings.rebuild(vault)
    media_replicas.rebuild(vault)
    volume = shutil.disk_usage(vault)
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: SimpleNamespace(total=volume.total, used=volume.total - 1, free=1))

    assert media_store.evict_to_target(vault) == 0
    media_store.drain_tick(vault, claimable=lambda: None)

    def no_vault_walk(*_args, **_kwargs):
        pytest.fail("owner status opened the vault inventory")

    monkeypatch.setattr(media_store, "bound_originals", no_vault_walk)
    status = media_replicas.status(vault)["eviction"]
    assert status["unit"] == "physical_objects"
    assert status["local"] == {"count": 2, "bytes": len(content) + resident.stat().st_size}
    assert status["evicted"] == {"count": 0, "bytes": 0}
    assert status["observed_at"] is not None
    assert status["waiting"] == "key_not_exported"


def test_inventory_keeps_its_last_complete_count_when_a_shared_filing_cannot_be_read(
    vault, local_store, monkeypatch,
):
    store, _ = local_store
    personal_eviction.enable(vault, store, free_bytes=2**30, eviction_settle_seconds=0)
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    content = b"two bindings to one recoverable physical original"
    with library_scope():
        first = preserve.preserve_stream(vault, scope="field", category="samples", filename="first.txt",
                                         stream=io.BytesIO(content))
        second = preserve.preserve_stream(vault, scope="field", category="samples", filename="second.txt",
                                          stream=io.BytesIO(content))
    for filed in (first, second):
        assert media_store.offload(vault, vault / filed.path) is not None
    assert media_bindings.rebuild(vault)
    media_replicas.confirm_key_export(vault, "1")
    assert media_store.evict(vault, vault / first.path)
    media_replicas.rebuild(vault)
    assert media_store.drain_census(vault, inventory_only=True) == (1, len(content))
    complete = media_replicas.status(vault)["eviction"]

    companion = vault / second.sidecar_path
    fields, body, _ = vault_files.parse_frontmatter(companion.read_text(encoding="utf-8"), custody=True)
    fields["stored_object"] = "not a stored object"  # the walk can no longer read this filing's custody
    companion.write_text(
        vault_files.render_frontmatter_document(vault_files.serialize_frontmatter(fields), body), encoding="utf-8",
    )
    with pytest.raises(media_store.MediaStoreError, match="MEDIA_CENSUS_UNKNOWN"):
        media_store.drain_census(vault, inventory_only=True)

    failed = media_replicas.status(vault)["eviction"]
    assert failed["inventory_state"] == "failed"
    assert failed["inventory_attempted_at"] > complete["inventory_attempted_at"]
    assert failed["observed_at"] == complete["observed_at"]
    assert failed["local"] == complete["local"] == {"count": 0, "bytes": 0}
    assert failed["evicted"] == complete["evicted"] == {"count": 1, "bytes": len(content)}


def test_first_declaration_cannot_claim_a_replacement_directory(vault, selected_store, monkeypatch):
    values, config = selected_store
    root = Path(values["root"])
    initial = root.with_name("detached-store")
    (root / media_store.STORE_MARKER).unlink()
    # Both test names are on the test device. The subsequent directory exchange is real.
    monkeypatch.setattr(media_store, "_require_distinct_device", lambda *_: None)

    def disconnect(_vault, _store):
        root.rename(initial)
        root.mkdir()
        return False

    monkeypatch.setattr(media_bindings, "store_is_bound", disconnect)
    assert config.is_file()
    with pytest.raises(media_store.MediaStoreError, match="MEDIA_STORE_UNAVAILABLE"):
        media_store.declare_store(vault, values["store"])

    assert initial.is_dir()
    assert not (root / media_store.STORE_MARKER).exists()
    assert media_replicas.declared_store_id(vault, values["store"]) is None
