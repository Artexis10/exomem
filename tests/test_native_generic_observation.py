"""Real native admission cuts, closing observations and bounded lifetimes."""

from __future__ import annotations

import errno
import gc
import os
import sys
import weakref
from contextvars import copy_context
from pathlib import Path

import pytest

from exomem import held_fs, reserved_paths, runtime_resources

native = pytest.importorskip("exomem._held_fs_linux_native")


def _no_open_files(root: Path) -> None:
    for descriptor in Path("/proc/self/fd").iterdir():
        try:
            target = Path(os.readlink(descriptor))
        except FileNotFoundError:
            continue
        assert not target.is_relative_to(root)


def test_private_parent_admission_precedes_native_read_and_preserves_record_locks(tmp_path):
    """A refused private parent must not let a child close release another owner's record lock."""
    import fcntl
    import subprocess

    path = tmp_path / "ordinary.bin"
    path.write_bytes(b"record")
    identity = reserved_paths._lstat_identity(tmp_path)
    private = reserved_paths.IdentityCatalogue({(identity.device, identity.inode, identity.kind): "embeddings-store"})
    with path.open("r+b") as owner:
        fcntl.lockf(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(reserved_paths.ReservedPathLeafError, match="RESERVED_PATH"):
            with held_fs.read_version_probe(tmp_path, ".", path.name,
                                            admit_identity=lambda value: reserved_paths._refuse_private_identity(value, private)):
                pytest.fail("a private parent must not admit a leaf")
        competing = subprocess.run([
            sys.executable, "-c", "import fcntl, sys\nwith open(sys.argv[1], 'r+b') as file:\n"
            "    try: fcntl.lockf(file, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "    except BlockingIOError: sys.exit(0)\n    sys.exit(1)\n", str(path),
        ], capture_output=True, timeout=5, check=False)
        assert competing.returncode == 0, competing.stderr.decode()
    _no_open_files(tmp_path)


@pytest.mark.parametrize("denied", ["ancestor", "leaf"])
def test_native_observation_requires_current_ordinary_read_access(tmp_path, denied):
    """O_PATH alone must not certify an object whose canonical read is refused."""
    parent = tmp_path / "notes"
    parent.mkdir()
    path = parent / "ordinary.bin"
    path.write_bytes(b"ordinary")
    target = parent if denied == "ancestor" else path
    mode = target.stat().st_mode & 0o777
    target.chmod(0o111 if denied == "ancestor" else 0)
    try:
        with pytest.raises(held_fs.HeldFsError) as refused:
            with held_fs.read_version_probe(tmp_path, "notes", path.name, admit_identity=lambda _value: None):
                pytest.fail("read admission must fail")
        assert refused.value.cause.errno == errno.EACCES
    finally:
        target.chmod(mode)
    _no_open_files(tmp_path)


def test_native_closing_parent_admission_precedes_comparison_and_child_acquisition(tmp_path):
    """An exchanged parent must receive current private admission before any child is opened."""
    parent = tmp_path / "notes"
    parent.mkdir()
    path = parent / "ordinary.bin"
    path.write_bytes(b"ordinary")
    with held_fs.read_version_probe(tmp_path, "notes", path.name, admit_identity=lambda _value: None) as observed:
        parent.rename(tmp_path / "moved")
        parent.mkdir()
        identity = reserved_paths._lstat_identity(parent)
        private = reserved_paths.IdentityCatalogue({(identity.device, identity.inode, identity.kind): "embeddings-store"})
        # There is no replacement leaf: a premature comparison/open would raise a different error.
        with pytest.raises(reserved_paths.ReservedPathLeafError, match="RESERVED_PATH"):
            observed.require_current(admit_identity=lambda value: reserved_paths._refuse_private_identity(value, private))
    _no_open_files(tmp_path)


def test_native_closing_rejects_a_parent_symlink_even_when_the_leaf_is_unchanged(tmp_path):
    """Retaining the same inode through a new symlink cannot certify the rooted name."""
    parent = tmp_path / "notes"
    parent.mkdir()
    (parent / "ordinary.bin").write_bytes(b"ordinary")
    with held_fs.read_version_probe(tmp_path, "notes", "ordinary.bin", admit_identity=lambda _value: None) as observed:
        parent.rename(tmp_path / "moved")
        parent.symlink_to(tmp_path / "moved", target_is_directory=True)
        with pytest.raises(held_fs.HeldFsError, match="UNSAFE_PATH"):
            observed.require_current(admit_identity=lambda _value: None)
    _no_open_files(tmp_path)


def test_native_closing_leaf_admission_precedes_version_comparison(tmp_path):
    """A replacement leaf's private identity must reach the fresh callback before its version is compared."""
    path = tmp_path / "ordinary.bin"
    path.write_bytes(b"ordinary")
    with held_fs.read_version_probe(tmp_path, ".", path.name, admit_identity=lambda _value: None) as observed:
        path.unlink()
        path.write_bytes(b"replacement")
        identity = reserved_paths._lstat_identity(path)
        private = reserved_paths.IdentityCatalogue({(identity.device, identity.inode, identity.kind): "embeddings-store"})
        with pytest.raises(reserved_paths.ReservedPathLeafError, match="RESERVED_PATH"):
            observed.require_current(admit_identity=lambda value: reserved_paths._refuse_private_identity(value, private))
    _no_open_files(tmp_path)


def test_native_version_rejects_restored_bytes_and_mtime_after_a_write(tmp_path):
    """Closing must use the current integer change witness, even after content and mtime return to A."""
    path = tmp_path / "ordinary.bin"
    path.write_bytes(b"first")
    before = path.stat()
    with held_fs.read_version_probe(tmp_path, ".", path.name, admit_identity=lambda _value: None) as observed:
        observed.require_current(admit_identity=lambda _value: None)
        path.write_bytes(b"other")
        path.write_bytes(b"first")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with pytest.raises(held_fs.HeldFsError, match="IDENTITY_CHANGED"):
            observed.require_current(admit_identity=lambda _value: None)
    _no_open_files(tmp_path)


def test_native_descriptor_qualification_rejects_procfs_without_reading_content():
    """A regular file on an unsupported kernel filesystem cannot acquire an ext witness."""
    # The writable capability probe cannot run on procfs; exercise descriptor qualification itself.
    with pytest.raises(OSError) as unsupported:
        native.open_version(os.fsencode(f"/proc/{os.getpid()}"), b".", b"status", lambda _value: None, None)
    assert unsupported.value.errno == errno.EOPNOTSUPP


def test_native_read_traversal_refuses_a_real_mount_crossing():
    """The fused traversal must retain openat2's no-cross-mount boundary."""
    with pytest.raises(OSError) as refused:
        native.open_version(b"/", os.fsencode(f"proc/{os.getpid()}"), b"status", lambda _value: None, None)
    assert refused.value.errno == errno.EXDEV


def test_native_recent_file_uses_the_full_reader_and_binds_its_actual_image(tmp_path):
    """A successful metadata acquisition must not turn a recent file into a trusted aged fact."""
    path = tmp_path / "ordinary.md"
    raw = b"---\ntitle: Public specimen\n---\nCurrent body.\n"
    path.write_bytes(raw)
    assert reserved_paths.inspect_generic_version(tmp_path, path.name) is None
    snapshot = reserved_paths.read_generic_bytes(tmp_path, path.name, version_witness=True)
    assert snapshot.data == raw and snapshot.version_witness.version is None


def test_native_workspace_stays_inside_the_existing_parent_budget(tmp_path):
    """A native child must not escape a small row's limit into the global preparation allowance."""
    (tmp_path / "ordinary.bin").write_bytes(b"ordinary")
    before = runtime_resources.semantic_preparation_status()["reserved_bytes"]
    with runtime_resources.parser_preparation(0, limit_bytes=8192) as parent:
        parent.reserve(1024)
        with pytest.raises(runtime_resources.PreparationBudgetExceeded):
            with held_fs.read_version_probe(tmp_path, ".", "ordinary.bin", admit_identity=lambda _value: None):
                pytest.fail("the native workspace exceeds the remaining row budget")
        assert runtime_resources.semantic_preparation_status()["reserved_bytes"] == before + 1024
    assert runtime_resources.semantic_preparation_status()["reserved_bytes"] == before
    _no_open_files(tmp_path)


@pytest.mark.parametrize("retention", ["dead", "inactive", "keep_open"])
def test_copied_context_cannot_reuse_a_closed_preparation(tmp_path, retention):
    """Copied request context must neither retain dead private buffers nor detach a native child from its closed owner."""
    (tmp_path / "ordinary.bin").write_bytes(b"ordinary")
    with runtime_resources.parser_preparation(0) as outer:
        with runtime_resources.parser_preparation(0, owner=outer, keep_open=retention == "keep_open") as child:
            copied = copy_context()
            reference = weakref.ref(child)
        if retention == "dead":
            del child
            gc.collect()
            assert reference() is None
        def observe():
            with held_fs.read_version_probe(tmp_path, ".", "ordinary.bin", admit_identity=lambda _value: None):
                pytest.fail("a closed copied context cannot admit native work")
        with pytest.raises(runtime_resources.PreparationBudgetExceeded, match="scope is closed"):
            copied.run(observe)
    _no_open_files(tmp_path)


@pytest.mark.parametrize("failure", [TimeoutError, MemoryError])
def test_native_callback_failure_releases_descriptors_and_child_reservation(tmp_path, failure):
    """Cancellation and fatal callback errors must unwind a native operation before it transfers its descriptors."""
    (tmp_path / "ordinary.bin").write_bytes(b"ordinary")
    before = runtime_resources.semantic_preparation_status()["reserved_bytes"]
    with runtime_resources.parser_preparation(0) as parent:
        parent.reserve(1024)
        def refuse(value):
            if value.kind == "file":
                raise failure("request ended at leaf admission")
        with pytest.raises(failure):
            with held_fs.read_version_probe(tmp_path, ".", "ordinary.bin", admit_identity=refuse):
                pytest.fail("the failing callback must prevent transfer")
        assert runtime_resources.semantic_preparation_status()["reserved_bytes"] == before + 1024
    _no_open_files(tmp_path)


def test_parent_scope_ending_at_native_admission_cancels_the_child(tmp_path):
    """The check after the real parent admission cut must reject a closed owner and release its child."""
    (tmp_path / "ordinary.bin").write_bytes(b"ordinary")
    before = runtime_resources.semantic_preparation_status()["reserved_bytes"]
    with runtime_resources.parser_preparation(0) as parent:
        parent.reserve(1024)
        with pytest.raises(runtime_resources.PreparationBudgetExceeded, match="scope is closed"):
            with held_fs.read_version_probe(tmp_path, ".", "ordinary.bin", admit_identity=lambda _value: parent.release()):
                pytest.fail("the native check must reject the closed parent")
    assert runtime_resources.semantic_preparation_status()["reserved_bytes"] == before
    _no_open_files(tmp_path)


def test_native_closing_preserves_the_parent_deadline_and_does_not_retain_its_owner(tmp_path):
    """A checked row deadline must still cancel closing, and a retained closed probe must not keep its row alive."""
    (tmp_path / "ordinary.bin").write_bytes(b"ordinary")
    ended = False
    def deadline():
        if ended:
            raise TimeoutError("row deadline ended")
    before = runtime_resources.semantic_preparation_status()["reserved_bytes"]
    with runtime_resources.parser_preparation(0, check=deadline) as parent:
        reference = weakref.ref(parent)
        with held_fs.read_version_probe(tmp_path, ".", "ordinary.bin", admit_identity=lambda _value: None) as observed:
            ended = True
            with pytest.raises(TimeoutError, match="row deadline ended"):
                observed.require_current(admit_identity=lambda _value: None)
    del parent
    gc.collect()
    assert reference() is None
    assert runtime_resources.semantic_preparation_status()["reserved_bytes"] == before
    _no_open_files(tmp_path)
