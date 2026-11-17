"""Interrupted shared downloads retain only their proven private-stage custody."""

from __future__ import annotations

import hashlib
import io
import json
from contextlib import closing

import pytest
from test_media_store import selected_store  # noqa: F401

from exomem import (
    artifact_lifecycle,
    held_fs,
    media_bindings,
    media_jobs,
    media_store,
    original_pool,
    preserve,
    reserved_paths,
)
from exomem.governance.principal import library_scope


def test_a_shared_hydration_stage_collision_preserves_the_file_and_releases_its_unused_reservation(
    vault, selected_store, monkeypatch,  # noqa: F811
):
    """An exclusive-create failure owns no file and must neither download nor retain a false cleanup charge."""
    content = b"the original awaits its first shared hydration"
    with library_scope():
        filing = preserve.preserve_stream(vault, scope="Archive", category="documents", filename="sample.txt",
                                         stream=io.BytesIO(content))
    assert media_store.offload(vault, vault / filing.path) is not None
    assert media_store.evict(vault, vault / filing.path)
    sidecar = (vault / filing.sidecar_path).read_bytes()
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    assert media_bindings.rebuild(vault)
    target = original_pool.object_path(vault, hashlib.sha256(content).hexdigest())
    target.parent.mkdir(parents=True)
    temporary = target.with_name(reserved_paths._owner_stage_name())
    temporary.write_bytes(b"an existing file outside this reservation")
    stage_name = reserved_paths._owner_stage_name
    monkeypatch.setattr(reserved_paths, "_owner_stage_name", lambda name=None: stage_name(name or temporary.name))
    monkeypatch.setattr(media_store.FilesystemStore, "open", lambda *args: pytest.fail("a stage collision must not fetch"))
    with pytest.raises(held_fs.HeldFsError, match="DESTINATION_EXISTS"):
        with media_store.open_original(vault, filing.path):
            pytest.fail("a collision cannot publish an original")
    assert temporary.read_bytes() == b"an existing file outside this reservation"
    assert not target.exists() and not (vault / filing.path).exists()
    assert (vault / filing.sidecar_path).read_bytes() == sidecar
    with closing(media_jobs.MediaJobStore(vault)._connect(readonly=True)) as conn:
        assert conn.execute("SELECT count(*) FROM media_access").fetchone()[0] == 0


@pytest.mark.parametrize("custody", ["recorded", "unrecorded", "replaced", "malformed", "flush failure"])
def test_restart_cleanup_preserves_unknown_stages_and_releases_only_durable_owned_cleanup(
    vault, selected_store, monkeypatch, custody,  # noqa: F811
):
    """A restart must not delete a collision or replacement, or lose a failed cleanup's byte charge."""
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    digest = hashlib.sha256(b"the complete original").hexdigest()
    target = original_pool.object_path(vault, digest)
    target.parent.mkdir(parents=True)
    temporary = target.with_name(reserved_paths._owner_stage_name())
    temporary.write_bytes(b"an interrupted plaintext prefix")
    with held_fs.acquire(vault).require() as filesystem:
        filesystem_type = type(filesystem)
        with filesystem.parent(temporary.parent.relative_to(vault).as_posix()).require() as parent:
            parent_identity = parent.identity
            with filesystem.file(parent, temporary.name).require() as file:
                identity = file.identity
    encoded = json.dumps((identity.device, identity.inode, identity.kind, identity.link_count))
    if custody == "unrecorded":
        encoded = None  # A crash between creation and the identity commit supplies no deletion authority.
    elif custody == "malformed":
        encoded = "{}"
    elif custody == "replaced":
        temporary.rename(temporary.with_suffix(".aside"))
        temporary.write_bytes(b"a replacement outside this reservation")
    expected = temporary.read_bytes()
    ledger = media_jobs.MediaJobStore(vault)
    with closing(ledger._connect()) as conn, conn:
        conn.execute("INSERT INTO media_access(token, binary_rel, pid, temporary_bytes, hydrating, "
                     "temporary_name, temporary_identity, hydration_key) VALUES ('stopped', ?, -1, 100, 1, ?, ?, ?)",
                     ("Archive/document.txt", temporary.name, encoded, target.relative_to(vault).as_posix()))
    actual_flush = filesystem_type.flush_directory
    refuse_flush = custody == "flush failure"

    def flush(self, directory):
        if refuse_flush and directory.identity == parent_identity:
            return held_fs.HeldResult(error=held_fs.HeldFsError("IO_REFUSED", "stage cleanup flush refused"))
        return actual_flush(self, directory)

    monkeypatch.setattr(filesystem_type, "flush_directory", flush)
    assert media_store.clear_dead_reservations(vault) == (1 if custody == "recorded" else 0)
    with closing(ledger._connect(readonly=True)) as conn:
        row = conn.execute("SELECT * FROM media_access WHERE token = 'stopped'").fetchone()
    if custody == "recorded":
        assert row is None and not temporary.exists()
    else:
        assert row["temporary_bytes"] == 100 and row["hydrating"] == 0
        assert row["temporary_name"] == temporary.name
        assert row["temporary_identity"] == encoded
        assert row["hydration_key"] == target.relative_to(vault).as_posix()
        if custody == "flush failure":
            assert not temporary.exists()
            refuse_flush = False
            assert media_store.clear_dead_reservations(vault) == 1  # Missing still needs its successful parent flush.
        else:
            assert temporary.read_bytes() == expected
            assert media_store.clear_dead_reservations(vault) == 0
            assert temporary.read_bytes() == expected
    assert not target.exists()
