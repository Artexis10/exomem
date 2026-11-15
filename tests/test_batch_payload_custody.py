"""Native batch copies retain custody across partial writes and refused cleanup."""

from __future__ import annotations

import errno
import hashlib
import io
import json
import os

import pytest

from exomem import held_fs, vault


@pytest.mark.parametrize("rollback", [False, True])
def test_payload_settlement_observes_final_native_bytes_after_cleanup(tmp_path, monkeypatch, rollback):
    root = tmp_path / "vault"
    root.mkdir()
    target = root / "managed.md"
    target.write_bytes(b"before")
    observed = []

    def fail_after_install(path):
        if rollback and path == target:
            raise RuntimeError("parent failed")

    def finished():
        observed.append(target.read_bytes())
        assert not any(path.is_dir() and path.name.startswith(vault.VAULT_SCAN_SKIP_DIR_PREFIXES) for path in root.iterdir())
        if rollback:
            raise held_fs.CustodyAcknowledgementPending("owner retains its reservation")

    monkeypatch.setattr(vault, "_after_batch_destination_published", fail_after_install)
    custody = vault._BatchPayloadCustody(lambda *args: None, lambda *args: None, lambda *args: None, finished)
    if rollback:
        with pytest.raises(RuntimeError, match="parent failed"):
            vault.batch_atomic_write([vault.PlannedWrite(target, "after")], vault_root=root,
                                     post_commit_fanout=False, _payload_custody=custody)
    else:
        vault.batch_atomic_write([vault.PlannedWrite(target, "after")], vault_root=root,
                                 post_commit_fanout=False, _payload_custody=custody)
    assert observed == [b"before" if rollback else b"after"]


@pytest.mark.parametrize("external_edit", [False, True])
def test_custodied_rollback_preserves_native_versions_and_external_edits(vault, external_edit):
    from exomem import source_native_custody, writer_lease
    from exomem import vault as vault_api
    from exomem.governance.principal import owner_principal, request_scope

    root = vault
    target = root / "managed.md"
    target.write_bytes(b"before image")
    before = target.stat()
    bindings = []
    manager = writer_lease.active_manager()
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(root):
        native = source_native_custody.NativeCustody(root)

        def publication(write, artifact, restoring):
            if write.path != target:
                return None
            active = []

            def reserve(name):
                active.append(native.reserve(
                    relative_path=name, expected_sha256=artifact.content_hash,
                    expected_size=artifact.held_file.version().size,
                    destination_relative="managed.md", removable=False,
                ))
                bindings.append((active[-1], restoring))

            def verified(file, digest):
                native.verified(active[-1], file, sha256=digest)

            return vault_api._ArtifactCustody(
                held_fs.TemporaryCustody(reserve, lambda name, file: native.bind(active[-1], file),
                                         lambda name, identity, outcome: None),
                verified, installed=lambda witness: native.record_installation(active[-1], witness),
            )

        def fail_after_install(path):
            if path == target:
                if external_edit:
                    path.write_bytes(b"after image")
                    changed = path.stat().st_mtime_ns + 1_000_000
                    os.utime(path, ns=(changed, changed))
                raise RuntimeError("rollback required")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(vault_api, "_after_batch_destination_published", fail_after_install)
            error = vault_api.BatchWriteError if external_edit else RuntimeError
            message = "BATCH_ROLLBACK_INCOMPLETE" if external_edit else "rollback required"
            with pytest.raises(error, match=message):
                vault_api.batch_atomic_write(
                    [vault_api.PlannedWrite(target, "after image")], vault_root=root,
                    _payload_custody=vault_api._BatchPayloadCustody(lambda *args: None, publication, lambda *args: None),
                    post_commit_fanout=False,
                )
        if external_edit:
            assert not any(restoring for binding, restoring in bindings)
            assert native.observe(bindings[-1][0].binding_id, destination=True).state == "unknown"
        else:
            restored = next(binding for binding, restoring in reversed(bindings) if restoring)
            observed = native.observe(restored.binding_id, destination=True)
            assert observed.state == "installed" and observed.matching is True
            after = target.stat()
            assert (after.st_mode, after.st_mtime_ns, after.st_atime_ns) == (before.st_mode, before.st_mtime_ns, before.st_atime_ns)
    assert target.read_bytes() == (b"after image" if external_edit else b"before image")


def test_partial_workspace_copy_binds_custody_before_source_bytes(tmp_path, monkeypatch):
    """A full-disk stage stays discoverable until its exact native allocation is removed."""
    root = tmp_path / "vault"
    root.mkdir()
    receipt = tmp_path / "custody.json"
    workspace = vault._BatchWorkspace.create(root, vault_root=root)

    def reserve(name):
        assert not (workspace.path / name).exists()
        receipt.write_text(json.dumps({"name": name, "state": "reserved"}))

    def bind(name, file):
        assert file.version().size == 0
        receipt.write_text(json.dumps({"name": name, "state": "initializing", "inode": file.identity.inode}))

    def verified(file, digest):
        pytest.fail("partial Source copy cannot acquire a complete-content witness")

    def release(name, identity, outcome):
        assert not (workspace.path / name).exists()
        receipt.write_text(json.dumps({"name": name, "state": "closed", "outcome": outcome}))

    def run_out_of_space(descriptor, content):
        saved = json.loads(receipt.read_text())
        assert saved["state"] == "initializing"
        assert os.fstat(descriptor).st_ino == saved["inode"]
        os.write(descriptor, b"partial Source text")
        raise OSError(errno.ENOSPC, "stage interrupted")

    custody = vault._ArtifactCustody(held_fs.TemporaryCustody(reserve, bind, release), verified)
    monkeypatch.setattr(vault, "_write_all", run_out_of_space)
    try:
        with pytest.raises(OSError, match="stage interrupted"):
            workspace.create_artifact("stage.tmp", b"complete Source text", custody=custody)
        assert (workspace.path / "stage.tmp").read_bytes() == b"partial Source text"
        assert json.loads(receipt.read_text())["state"] == "initializing"
        assert workspace.cleanup()
        assert json.loads(receipt.read_text()) == {"name": "stage.tmp", "state": "closed", "outcome": "removed"}
    finally:
        workspace.close()


def test_verified_binary_stage_retains_custody_when_native_cleanup_is_refused(tmp_path, monkeypatch):
    """Verified original bytes remain an open obligation when their native unlink fails."""
    root = tmp_path / "vault"
    root.mkdir()
    receipt = tmp_path / "custody.json"
    content = b"\x00\xfforiginal bytes"
    digest = hashlib.sha256(content).hexdigest()
    workspace = vault._BatchWorkspace.create(root, vault_root=root)

    def reserve(name):
        assert not (workspace.path / name).exists()
        receipt.write_text(json.dumps({"name": name, "state": "reserved"}))

    def bind(name, file):
        assert file.version().size == 0
        receipt.write_text(json.dumps({"name": name, "state": "initializing", "inode": file.identity.inode}))

    def verified(file, observed):
        saved = json.loads(receipt.read_text())
        assert saved["inode"] == file.identity.inode
        assert observed == digest and file.version().size == len(content)
        receipt.write_text(json.dumps({**saved, "state": "verified", "sha256": observed}))

    def release(name, identity, outcome):
        pytest.fail("refused unlink cannot acknowledge copy removal")

    custody = vault._ArtifactCustody(held_fs.TemporaryCustody(reserve, bind, release), verified)
    try:
        artifact = workspace.create_stream_artifact(
            "original.tmp", io.BytesIO(content), expected_size=len(content), expected_hash=digest, custody=custody,
        )
        assert json.loads(receipt.read_text())["state"] == "verified"
        monkeypatch.setattr(workspace.held_filesystem, "unlink", lambda file: held_fs.HeldResult(
            error=held_fs.HeldFsError("IO_REFUSED", "native cleanup refused"),
        ))
        assert not workspace.cleanup()
        assert artifact.path.read_bytes() == content
        assert json.loads(receipt.read_text())["state"] == "verified"
    finally:
        workspace.close()


@pytest.mark.parametrize("replacement", [False, True])
def test_publish_temporary_witness_survives_an_exception_after_native_install(tmp_path, monkeypatch, replacement):
    """Recovery can distinguish the installed allocation after a publisher loses its result."""
    root = tmp_path / "vault"
    root.mkdir()
    receipt = tmp_path / "installation.json"
    workspace = vault._BatchWorkspace.create(root, vault_root=root)
    artifact = workspace.create_artifact("stage.tmp", b"Source allocation")

    def reserve(name):
        assert not (root / name).exists()
        receipt.write_text(json.dumps({"name": name, "state": "reserved"}))

    def bind(name, file):
        assert file.version().size == 0
        receipt.write_text(json.dumps({"name": name, "state": "initializing", "inode": file.identity.inode}))

    def verified(file, digest):
        saved = json.loads(receipt.read_text())
        receipt.write_text(json.dumps({**saved, "state": "verified", "sha256": digest, "size": file.version().size}))

    def release(name, identity, outcome):
        saved = json.loads(receipt.read_text())
        assert saved["inode"] == identity.inode and outcome == "installed"

    original = held_fs.publish_bytes

    def lose_result(*args, **kwargs):
        assert original(*args, **kwargs).ok
        raise RuntimeError("installation result lost")

    custody = vault._ArtifactCustody(held_fs.TemporaryCustody(reserve, bind, release), verified)
    monkeypatch.setattr(held_fs, "publish_bytes", lose_result)
    try:
        with pytest.raises(RuntimeError, match="installation result lost"):
            workspace.replace_artifact(artifact, root / "source.md", vault_root=root, custody=custody)
        installed = root / "source.md"
        saved = json.loads(receipt.read_text())
        assert saved["state"] == "verified" and installed.stat().st_ino == saved["inode"]
        assert installed.read_bytes() == b"Source allocation"
        assert hashlib.sha256(installed.read_bytes()).hexdigest() == saved["sha256"]
        assert not (root / saved["name"]).exists()
        if replacement:
            # Equal content in another allocation cannot inherit the lost native result.
            other = root / "other.tmp"
            other.write_bytes(b"Source allocation")
            os.replace(other, installed)
            assert workspace.bind_installed_after_error(artifact, installed) is None
        else:
            assert workspace.bind_installed_after_error(artifact, installed) is not None
            artifact.installation_witness = None
            assert workspace.bind_installed_after_error(artifact, installed) is None
        assert workspace.cleanup()
    finally:
        workspace.close()


def test_inverse_right_revoked_during_copy_cannot_restore_obsolete_bytes(tmp_path, monkeypatch):
    """Rollback rechecks its publication right after a long copy, before replacing current bytes."""
    root = tmp_path / "vault"
    root.mkdir()
    target = root / "source.md"
    target.write_bytes(b"current Source")
    expected = vault._identity(target.name, target.stat())
    workspace = vault._BatchWorkspace.create(root, vault_root=root)
    artifact = workspace.create_artifact("restore.tmp", b"obsolete Source")
    permitted = True
    original = vault._verified_chunks

    def revoke_after_copy(value):
        nonlocal permitted
        yield from original(value)
        permitted = False

    def check_inverse():
        if not permitted:
            raise RuntimeError("inverse publication retired")

    monkeypatch.setattr(vault, "_verified_chunks", revoke_after_copy)
    try:
        with vault._batch_replace_context(root, expected, inverse_recheck=check_inverse):
            with pytest.raises(RuntimeError, match="inverse publication retired"):
                workspace.replace_artifact(artifact, target)
        assert target.read_bytes() == b"current Source"
        assert not any(held_fs.is_publish_temporary(path.name) for path in root.iterdir())
        assert workspace.cleanup()
    finally:
        workspace.close()


def test_new_stage_alias_prevents_a_false_copy_retirement(tmp_path):
    """Removing one name must not acknowledge retirement when another link retains the Source bytes."""
    root = tmp_path / "vault"
    root.mkdir()
    workspace = vault._BatchWorkspace.create(root, vault_root=root)
    evidence = {}

    def reserve(name):
        evidence["state"] = "reserved"

    def bind(name, file):
        evidence["state"] = "initializing"

    def verified(file, digest):
        evidence["state"] = "verified"

    def release(name, identity, outcome):
        pytest.fail("an unaccounted native alias cannot acknowledge copy retirement")

    custody = vault._ArtifactCustody(held_fs.TemporaryCustody(reserve, bind, release), verified)
    try:
        artifact = workspace.create_artifact("source.tmp", b"retained Source bytes", custody=custody)
        alias = root / "retained.tmp"
        os.link(artifact.path, alias)
        assert not workspace.cleanup()
        assert evidence["state"] == "verified"
        assert artifact.path.read_bytes() == alias.read_bytes() == b"retained Source bytes"
    finally:
        workspace.close()
