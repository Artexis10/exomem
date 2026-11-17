"""Shared original custody: immutable bytes stay private and survive publication failures."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from exomem import artifact_lifecycle, held_fs, original_pool, reserved_paths, state_migration


def test_staged_original_releases_coordination_and_never_exposes_a_generic_alias(tmp_path: Path) -> None:
    """A slow upload must not monopolize metadata or expose its private stage through a new alias."""
    content = b"an immutable original"
    target = tmp_path / "Knowledge Base" / reserved_paths.ORIGINAL_POOL_DIRNAME / "sha256" / "ab" / "original.blob"
    copied = []

    def chunks():
        assert not reserved_paths._identity_coordination_active(tmp_path, "original-pool")
        copied.append(len(content))
        yield content

    with reserved_paths._subsystem_authority_scope("original_pool"):
        with reserved_paths._stage_owner_chunks(tmp_path, target, "original-pool", held_fs.Chunks(chunks),
                expected_sha256=hashlib.sha256(content).hexdigest(), expected_size=len(content)) as staged:
            assert copied == [len(content)]
            reserved_paths._install_staged_owner(staged)
            with reserved_paths._open_owner_binary(tmp_path, target, "original-pool") as source:
                assert not reserved_paths._identity_coordination_active(tmp_path, "original-pool")
                assert source.stream.read() == content
                source.require_current()
            with pytest.raises(reserved_paths.ReservedPathLeafError):
                with reserved_paths.open_generic_binary(tmp_path, target.relative_to(tmp_path).as_posix()):
                    pytest.fail("a private original became a generic file")
    assert list(target.parent.iterdir()) == [target]


def test_streamed_original_rejects_exchanged_ancestor(tmp_path: Path) -> None:
    """Renaming a grandparent must not let a held original validate against a detached directory."""
    content = b"the original"
    pool = tmp_path / "Knowledge Base" / reserved_paths.ORIGINAL_POOL_DIRNAME
    target = pool / "sha256" / "ab" / "original.blob"
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    with reserved_paths._subsystem_authority_scope("original_pool"):
        with pytest.raises(held_fs.HeldFsError, match="private parent changed"):
            with reserved_paths._open_owner_binary(tmp_path, target, "original-pool") as source:
                (pool / "sha256").rename(pool / "aside")
                target.parent.mkdir(parents=True)
                target.write_bytes(content)
                source.require_current()


def test_staged_original_refuses_replaced_input_before_publication(tmp_path: Path) -> None:
    """A replaced stage must never be accepted as the verified uploaded original."""
    content = b"the verified upload"
    target = tmp_path / "Knowledge Base" / reserved_paths.ORIGINAL_POOL_DIRNAME / "original.blob"
    with reserved_paths._subsystem_authority_scope("original_pool"):
        with pytest.raises(held_fs.HeldFsError, match="private stage changed"):
            with reserved_paths._stage_owner_chunks(tmp_path, target, "original-pool",
                    held_fs.Chunks(lambda: [content]), expected_sha256=hashlib.sha256(content).hexdigest(),
                    expected_size=len(content)) as staged:
                temporary = target.with_name(staged.temporary)
                temporary.rename(temporary.with_suffix(".aside"))
                temporary.write_bytes(b"unverified replacement")
                reserved_paths._install_staged_owner(staged)
    assert not target.exists()


def test_original_directory_flush_failure_prevents_a_binding_commit(tmp_path: Path, monkeypatch) -> None:
    """Acknowledging a filing before its object directory is durable can lose that filing after a crash."""
    content = b"durable original"
    target = tmp_path / "Knowledge Base" / reserved_paths.ORIGINAL_POOL_DIRNAME / "original.blob"
    with held_fs.acquire(tmp_path).require() as filesystem:
        filesystem_type = type(filesystem)

    def failed_flush(self, directory):
        return held_fs.HeldResult(error=held_fs.HeldFsError("IO_REFUSED", "directory flush failed"))

    monkeypatch.setattr(filesystem_type, "flush_directory", failed_flush)
    committed = []
    with reserved_paths._subsystem_authority_scope("original_pool"):
        with pytest.raises(held_fs.HeldFsError, match="directory flush failed"):
            with reserved_paths._stage_owner_chunks(tmp_path, target, "original-pool",
                    held_fs.Chunks(lambda: [content]), expected_sha256=hashlib.sha256(content).hexdigest(),
                    expected_size=len(content)) as staged:
                reserved_paths._install_staged_owner(staged)
                committed.append(target)
    assert committed == []
    assert target.read_bytes() == content


def test_pool_activation_requires_launcher_custody_before_any_canonical_marker(vault, monkeypatch) -> None:
    """An old launcher must not admit a layout that its rollback path cannot recognize."""
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset())
    with pytest.raises(state_migration.StateMigrationOfflineRequired):
        original_pool.enable(vault)
    assert not original_pool.pool_root(vault).exists()
    assert state_migration.SHARED_ORIGINALS_COMPATIBILITY_ID not in state_migration.recorded_descriptor_ids(vault)
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    assert original_pool.enabled(vault)
    assert state_migration.SHARED_ORIGINALS_COMPATIBILITY_ID in state_migration.recorded_descriptor_ids(vault)


def test_concurrent_identical_uploads_publish_one_verified_original(vault, monkeypatch) -> None:
    """Two prepared uploads can race without replacing an original or losing the second filing's bytes."""
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    content = b"one immutable original for independent filings"
    digest = hashlib.sha256(content).hexdigest()
    with original_pool.prepare(vault, digest, len(content), held_fs.Chunks(lambda: [content])) as first:
        with original_pool.prepare(vault, digest, len(content), held_fs.Chunks(lambda: [content])) as second:
            first.publish()
            with pytest.raises(original_pool.PublicationRace):
                second.publish()
            assert second.confirm_existing()
            second.publish()
            first.require_current()
            second.require_current()
    assert list((original_pool.pool_root(vault) / "sha256").rglob("*.blob")) == [original_pool.object_path(vault, digest)]
    with original_pool.open_object(vault, digest, len(content)) as source:
        assert source.stream.read() == content


def test_pool_hit_still_stages_all_supplied_bytes_before_filing(vault, monkeypatch) -> None:
    """A hidden match cannot bypass the uploaded-byte allocation and integrity checks."""
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    content = b"the same complete upload"
    digest = hashlib.sha256(content).hexdigest()
    with original_pool.prepare(vault, digest, len(content), held_fs.Chunks(lambda: [content])) as first:
        first.publish()
    copied = []

    def chunks():
        copied.append(len(content))
        yield content

    with original_pool.prepare(vault, digest, len(content), held_fs.Chunks(chunks)) as second:
        assert copied == [len(content)]
        second.publish()
    original_pool.object_path(vault, digest).write_bytes(b"corrupted stored bytes")
    with pytest.raises(ValueError, match="differs from its canonical binding"):
        with original_pool.prepare(vault, digest, len(content), held_fs.Chunks(chunks)):
            pytest.fail("a corrupted shared original was accepted")
    assert copied == [len(content), len(content)]


def test_pool_eviction_refuses_a_replaced_physical_version(vault, monkeypatch) -> None:
    """An eviction confirmed for old bytes must not unlink a replacement at the same digest path."""
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    content = b"the confirmed physical original"
    digest = hashlib.sha256(content).hexdigest()
    with original_pool.prepare(vault, digest, len(content), held_fs.Chunks(lambda: [content])) as prepared:
        prepared.publish()
    with original_pool.open_object(vault, digest, len(content)) as source:
        version = source.version
    target = original_pool.object_path(vault, digest)
    target.rename(target.with_suffix(".aside"))
    target.write_bytes(b"a replacement must survive")
    with pytest.raises(OSError, match="target changed"):
        original_pool.evict_object(vault, digest, expected_version=version)
    assert target.read_bytes() == b"a replacement must survive"


def test_interrupted_marker_publication_retains_compatibility_and_resumes(vault, monkeypatch) -> None:
    """A durable format requirement must survive an unacknowledged marker publication."""
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    real_flush = held_fs.flush_directory_path

    def interrupted(path):
        return held_fs.HeldResult(error=held_fs.HeldFsError("IO_REFUSED", "marker flush interrupted"))

    monkeypatch.setattr(held_fs, "flush_directory_path", interrupted)
    with pytest.raises(held_fs.HeldFsError, match="marker flush interrupted"):
        original_pool.enable(vault)
    assert state_migration.SHARED_ORIGINALS_COMPATIBILITY_ID in state_migration.recorded_descriptor_ids(vault)
    monkeypatch.setattr(held_fs, "flush_directory_path", real_flush)
    original_pool.enable(vault)
    assert original_pool.enabled(vault)


def test_independent_dataset_filings_resolve_one_object_for_queries_and_range_reads(vault, monkeypatch) -> None:
    """Retiring logical binaries must preserve each exact companion, dataset answer and retained download."""
    from exomem import artifact_recovery, commands, media_store, originals, preserve, writer_lease
    from exomem.governance.principal import library_scope, local_owner_principal

    content = b"item,value\nsample,7\n"
    with library_scope():
        public = preserve.preserve_stream(vault, scope="Archive", category="datasets", filename="table.csv",
                                         stream=io.BytesIO(content))
        protected = preserve.preserve_stream(vault, scope="Archive", category="datasets", filename="private-table.csv",
                                            stream=io.BytesIO(content), raw_protection=True)
    sidecars = {item.path: (vault / item.sidecar_path).read_bytes() for item in (public, protected)}
    assert not original_pool.enabled(vault)
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    digest = hashlib.sha256(content).hexdigest()
    with original_pool.prepare(vault, digest, len(content), held_fs.Chunks(lambda: [content])) as staged:
        with writer_lease.get_manager().mutation_guard(vault, operation="shared_original_test_publication"):
            staged.publish()
            for item in (public, protected):
                (vault / item.path).unlink()
            staged.require_current()
    with library_scope():
        for item in (public, protected):
            snapshot = originals.original_state(vault, item.path)
            assert snapshot.path == vault / item.path
            assert snapshot.local_path == original_pool.object_path(vault, digest)
            assert snapshot.present and not snapshot.offloadable
            assert originals.resolve_original_relative(vault, item.path) == item.path
            assert commands.op_query_dataset(vault, path=item.path)["rows"] == [{"item": "sample", "value": "7"}]
            with artifact_recovery.stage(vault, item.path, local_owner_principal(surface="mcp")) as (held, descriptor):
                assert reserved_paths.read_held(held, 5, 8) == content[5:13]
                assert descriptor["size"] == len(content)
            assert media_store.read_original_bytes(vault, item.path).data == content
            assert (vault / item.sidecar_path).read_bytes() == sidecars[item.path]
    assert len(list((original_pool.pool_root(vault) / "sha256").rglob("*.blob"))) == 1
    from exomem.vault import parse_frontmatter, render_frontmatter_document, serialize_frontmatter

    fields, body, _marker = parse_frontmatter(sidecars[protected.path].decode(), custody=True)
    fields["binary_sha256"] = "0" * 64
    (vault / protected.sidecar_path).write_text(render_frontmatter_document(serialize_frontmatter(fields), body))
    with library_scope():
        with pytest.raises(ValueError, match="NOT_FOUND"):
            commands.op_query_dataset(vault, path=protected.path)
        assert media_store.read_original_bytes(vault, public.path).data == content


def test_frame_sampling_reads_the_pooled_stream_and_keeps_the_logical_video_path(vault, monkeypatch) -> None:
    """A frame decoder must receive retained original bytes after the filing's binary path is retired."""
    from test_video_frames import _img, _patch_decode

    from exomem import embeddings, preserve, video_frames
    from exomem.governance.principal import library_scope

    content = b"synthetic video container"
    with library_scope():
        saved = preserve.preserve_stream(vault, scope="Archive", category="videos", filename="clip.mp4",
                                        stream=io.BytesIO(content))
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    with original_pool.prepare(vault, hashlib.sha256(content).hexdigest(), len(content),
                               held_fs.Chunks(lambda: [content])) as prepared:
        prepared.publish()
        (vault / saved.path).unlink()
    _patch_decode(monkeypatch, duration=20.0)

    def decode(stream, timestamps):
        stream.seek(0)
        assert stream.read() == content
        return [_img(index) for index, _timestamp in enumerate(timestamps)]

    monkeypatch.setattr(embeddings, "_decode_frames_at", decode)
    result = video_frames.get_frames(vault, saved.path, max_frames=2)
    assert result.path == saved.path
    assert len(result.frames) == 2
    assert all(frame.jpeg == b"\xff\xd8fake" for frame in result.frames)


def test_identical_captures_in_a_pooled_archive_share_one_original_and_keep_their_own_filings(
        vault, tmp_path, monkeypatch) -> None:
    """Two Sources of the same bytes keep separate pages and paths; one verified object holds the bytes."""
    import datetime as dt

    from exomem import add as add_module
    from exomem import media_store, originals, schema

    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    data = b"Speaker A: one recording, filed twice."
    filed = []
    for title in ("Morning briefing", "Evening briefing"):
        stage = tmp_path / title.replace(" ", "-")
        stage.mkdir()
        (stage / "briefing.txt").write_bytes(data)
        filed.append(add_module.add(
            vault, schema.load_source_schema(vault), content="", title=title, source_type="session",
            artifact=add_module.SourceArtifact(staged_path=stage / "briefing.txt", filename="briefing.txt"),
            today=dt.date(2026, 11, 15),
        ))
    digest = hashlib.sha256(data).hexdigest()
    assert list((original_pool.pool_root(vault) / "sha256").rglob("*.blob")) == [original_pool.object_path(vault, digest)]
    assert filed[0].artifact_path != filed[1].artifact_path
    for added in filed:
        assert not (vault / added.artifact_path).exists()
        assert originals.original_state(vault, added.artifact_path).local_path == original_pool.object_path(vault, digest)
        assert media_store.read_original_bytes(vault, added.artifact_path).data == data
        assert f"evidence_file: {added.artifact_path}" in (vault / added.path).read_text(encoding="utf-8")


def test_a_refused_capture_publishes_no_original(vault, tmp_path, monkeypatch) -> None:
    """Bytes publish only at the commit point; a write refused before it leaves the pool untouched."""
    import datetime as dt

    from exomem import add as add_module
    from exomem import schema
    from exomem import vault as vault_module

    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "notes.txt").write_bytes(b"never stored")

    def refuse(*args, **kwargs):
        raise vault_module.PathGuardError("PATH_GUARD_CHANGED", "a planned write moved")

    monkeypatch.setattr(vault_module, "recheck_path_guards", refuse)
    with pytest.raises((vault_module.PathGuardError, add_module.AddError, ValueError)):
        add_module.add(vault, schema.load_source_schema(vault), content="", title="Refused", source_type="session",
                       artifact=add_module.SourceArtifact(staged_path=stage / "notes.txt", filename="notes.txt"),
                       today=dt.date(2026, 11, 15))
    assert not list((original_pool.pool_root(vault) / "sha256").rglob("*.blob"))


def _pooled_capture(vault, tmp_path, monkeypatch, name: str, data: bytes):
    import datetime as dt

    from exomem import add as add_module
    from exomem import schema

    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    if not original_pool.enabled(vault):
        original_pool.enable(vault)
    stage = tmp_path / f"stage-{name}"
    stage.mkdir()
    (stage / name).write_bytes(data)
    return add_module.add(vault, schema.load_source_schema(vault), content="", title=f"Capture {name}",
                          source_type="session", today=dt.date(2026, 11, 15),
                          artifact=add_module.SourceArtifact(staged_path=stage / name, filename=name))


def test_a_pooled_original_is_available_although_its_logical_path_holds_no_file(vault, tmp_path, monkeypatch) -> None:
    """Sweeps that ask whether an original is gone must not read a pooled filing as deleted."""
    from exomem import originals

    added = _pooled_capture(vault, tmp_path, monkeypatch, "minutes.txt", b"minutes of the meeting")
    assert not (vault / added.artifact_path).exists()
    assert originals.available(vault, added.artifact_path)
    assert originals.available(vault, vault / added.artifact_path)
    assert not originals.available(vault, "Knowledge Base/Sources/never-filed.txt")


def test_a_direct_read_of_a_pooled_text_original_returns_its_text(vault, tmp_path, monkeypatch) -> None:
    """Catches a direct read of a pooled filing's path that answers NOT_FOUND because no file sits there, where the
    same original kept as an ordinary file returns its text; an agent then reports the filing as gone."""
    from exomem import get_page

    data = b"Speaker A: the minutes, kept once.\n"
    added = _pooled_capture(vault, tmp_path, monkeypatch, "minutes.txt", data)
    assert not (vault / added.artifact_path).exists()

    read = get_page.get_page(vault, path=added.artifact_path)

    assert read.content == data.decode()
    assert read.content_hash == hashlib.sha256(data).hexdigest()


def test_a_decoder_view_keeps_the_logical_filename_and_disappears_after_use(vault, tmp_path, monkeypatch) -> None:
    """Decoders dispatch on the filename; a pooled object's digest name would select no decoder."""
    from exomem import media_store

    data = b"WEBVTT\n\n00:00.000 --> 00:01.000\nhello"
    added = _pooled_capture(vault, tmp_path, monkeypatch, "talk.vtt", data)
    with media_store.logical_view(vault, added.artifact_path) as view:
        assert view.name == Path(added.artifact_path).name and view.suffix == ".vtt"
        assert view.read_bytes() == data
        assert not view.is_relative_to(vault / "Knowledge Base" / "Sources")
    assert not view.exists()
    assert not (vault / added.artifact_path).exists()


def test_preserved_evidence_pools_its_binary_but_keeps_a_dataset_resident(vault, monkeypatch) -> None:
    """Evidence uploads share bytes like Sources; a dataset stays a file its queries open in place."""
    import datetime as dt

    from exomem import originals, preserve

    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    photo = b"\x89PNG\r\n\x1a\n" + b"receipt photo bytes" * 64
    first = preserve.preserve_bytes(vault, scope="work", category="receipts", filename="receipt.png",
                                    data=photo, today=dt.date(2026, 11, 16))
    second = preserve.preserve_bytes(vault, scope="work", category="receipts", filename="receipt-copy.png",
                                     data=photo, today=dt.date(2026, 11, 16))
    for result in (first, second):
        assert not (vault / result.path).exists()
        assert (vault / result.sidecar_path).is_file()
        assert originals.pooled(vault, result.path) is not None
    assert originals.pooled(vault, first.path).local_path == originals.pooled(vault, second.path).local_path
    table = preserve.preserve_bytes(vault, scope="work", category="exports", filename="ledger.csv",
                                    data=b"date,amount\n2026-11-16,12.50\n", today=dt.date(2026, 11, 16))
    assert (vault / table.path).is_file()
    assert originals.pooled(vault, table.path) is None


def test_derived_state_cleanup_and_rebuilds_leave_pooled_originals_intact(vault, tmp_path, monkeypatch) -> None:
    """An owner who adopts external state and rebuilds derived rows must not lose canonical original bytes.

    A cleanup that sorted the pool with machine-local state would delete bytes that no index can rebuild.
    """
    from exomem import commands, lexstore, media_store, state_migration
    from exomem import reconcile as reconcile_module
    from exomem.kbdir import kb_dirname

    data = b"Speaker A: the only copy of this recording."
    added = _pooled_capture(vault, tmp_path, monkeypatch, "briefing.txt", data)
    orphan = b"published before a crash, never filed"
    with original_pool.prepare(vault, hashlib.sha256(orphan).hexdigest(), len(orphan),
                               held_fs.Chunks(lambda: [orphan])) as prepared:
        prepared.publish()
    pool = original_pool.pool_root(vault)
    before = {path.relative_to(vault): path.read_bytes() for path in pool.rglob("*") if path.is_file()}
    assert len(before) == 3  # the marker and two objects
    stray = vault / kb_dirname() / ".graph-sync.json"
    stray.write_bytes(b'{"epoch": "left by an older release"}')

    state_migration.reset_state_resolution_cache_for_tests()
    state_migration.migrate_vault_state_offline(
        vault, authority=state_migration.assert_offline_migration_authority(source="pool cleanup test"),
        adopt="external")
    assert not stray.exists()  # the cleanup really ran over this vault
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "fts5")
    assert lexstore.get_store(vault).rebuild_atomic() is True
    reconcile_module.reconcile(vault, dry_run=False, rebuild_graph=True)
    commands.op_maintain_memory(vault, mode="reconcile")

    assert {path.relative_to(vault): path.read_bytes() for path in pool.rglob("*") if path.is_file()} == before
    assert original_pool.enabled(vault)
    assert media_store.read_original_bytes(vault, added.artifact_path).data == data
