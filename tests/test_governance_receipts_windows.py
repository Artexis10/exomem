"""Native Windows safety regression coverage for governance receipts."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem.governance import receipts


pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows receipt contract")


def _month(instance_dir: Path) -> Path:
    month = instance_dir / "2026-08.jsonl"
    month.write_bytes(b'{"schema":"receipt/v1"}\n')
    return month


def _junction(path: Path, target: Path) -> None:
    completed = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(path), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        pytest.skip(f"junction creation unavailable: {completed.stderr or completed.stdout}")
    assert path.is_dir()


def _outside_snapshot(root: Path) -> dict[str, tuple[object, ...]]:
    """Record content and identity so a refused receipt cannot touch the target."""
    snapshot: dict[str, tuple[object, ...]] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        relative = "." if path == root else path.relative_to(root).as_posix()
        info = path.stat()
        if path.is_dir():
            snapshot[relative] = ("directory", info.st_dev, info.st_ino, info.st_mtime_ns)
        else:
            snapshot[relative] = ("file", path.read_bytes(), info.st_dev, info.st_ino, info.st_mtime_ns)
    return snapshot


def _outside_target(tmp_path: Path, component: str) -> Path:
    outside = tmp_path / f"outside-{component}"
    outside.mkdir()
    (outside / "sentinel.txt").write_bytes(b"must not be read or changed")
    return outside


def _stub_directory_open(
    monkeypatch: pytest.MonkeyPatch, instance_dir: Path, *, child_open
) -> None:
    """Reach the native fallback child seam without asking the CRT for a directory."""
    original_fstat = receipts.os.fstat
    original_open = receipts.os.open
    original_close = receipts.os.close
    instance_stat = os.lstat(instance_dir)
    directory_fd = 90_001

    def open_path(path, *args, **kwargs):  # noqa: ANN001
        if Path(path) == instance_dir:
            return directory_fd
        return child_open(path, *args, **kwargs)

    def fstat_path(fd: int):
        return instance_stat if fd == directory_fd else original_fstat(fd)

    monkeypatch.setattr(receipts.os, "open", open_path)
    monkeypatch.setattr(receipts.os, "fstat", fstat_path)
    monkeypatch.setattr(receipts.os, "close", lambda fd: None if fd == directory_fd else original_close(fd))
    assert original_open is not None


def test_windows_month_open_never_uses_crt_to_open_retained_directory(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid Windows instance must reach its child without ``os.open(dir)``."""
    instance_dir = vault / "Knowledge Base" / "_Governance" / "events" / ("a" * 32)
    instance_dir.mkdir(parents=True)
    month = _month(instance_dir)
    original_open = receipts.os.open

    def reject_crt_directory(path, *args, **kwargs):  # noqa: ANN001
        if Path(path) == instance_dir:
            pytest.fail("Windows receipt open attempted CRT directory access")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(receipts.os, "open", reject_crt_directory)
    with receipts._open_month_fd(instance_dir, month.name) as fd:
        assert os.read(fd, 1) == b"{"


def test_windows_month_open_normalizes_a_direct_child_open_refusal(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native child-open failures must stay content-free ``ReceiptError`` results."""
    instance_dir = vault / "Knowledge Base" / "_Governance" / "events" / ("b" * 32)
    instance_dir.mkdir(parents=True)
    month = _month(instance_dir)
    detail = r"native child denial C:\\private\\receipt.jsonl"
    original_open = receipts.os.open

    def deny_child(path, *args, **kwargs):  # noqa: ANN001
        if Path(path) == month:
            raise PermissionError(detail)
        return original_open(path, *args, **kwargs)

    _stub_directory_open(monkeypatch, instance_dir, child_open=deny_child)
    with pytest.raises(receipts.ReceiptError, match="evidence path") as exc_info:
        with receipts._open_month_fd(instance_dir, month.name):
            pass
    assert detail not in str(exc_info.value)


def test_windows_month_open_normalizes_child_identity_and_reparse_refusals(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native child identity and reparse failures are content-free receipt refusals."""
    instance_dir = vault / "Knowledge Base" / "_Governance" / "events" / ("d" * 32)
    instance_dir.mkdir(parents=True)
    month = _month(instance_dir)
    original_lstat = receipts.os.lstat
    original_fstat = receipts.os.fstat
    original_open = receipts.os.open
    detail = r"reparse target C:\\private\\outside"

    _stub_directory_open(monkeypatch, instance_dir, child_open=original_open)
    child_fd = original_open(month, os.O_RDONLY)
    try:
        child_stat = original_fstat(child_fd)
    finally:
        os.close(child_fd)

    def changed_fstat(fd: int):
        if fd == 90_001:
            return os.lstat(instance_dir)
        value = original_fstat(fd)
        if fd != 90_001 and stat.S_ISREG(value.st_mode):
            return SimpleNamespace(st_mode=value.st_mode, st_dev=value.st_dev, st_ino=value.st_ino + 1)
        return value

    monkeypatch.setattr(receipts.os, "fstat", changed_fstat)
    with pytest.raises(receipts.ReceiptError, match="evidence path") as identity_error:
        with receipts._open_month_fd(instance_dir, month.name):
            pass
    assert str(child_stat.st_ino) not in str(identity_error.value)

    def reparse_lstat(path):  # noqa: ANN001
        if Path(path) == month:
            return SimpleNamespace(st_mode=stat.S_IFLNK)
        return original_lstat(path)

    monkeypatch.setattr(receipts.os, "lstat", reparse_lstat)
    with pytest.raises(receipts.ReceiptError, match="evidence path") as reparse_error:
        with receipts._open_month_fd(instance_dir, month.name):
            pass
    assert detail not in str(reparse_error.value)


def test_windows_month_open_normalizes_yielded_io_failure_without_path_detail(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller I/O failure cannot leak a native filesystem diagnostic."""
    instance_dir = vault / "Knowledge Base" / "_Governance" / "events" / ("e" * 32)
    instance_dir.mkdir(parents=True)
    month = _month(instance_dir)
    detail = r"I/O error C:\\private\\receipt.jsonl"
    _stub_directory_open(monkeypatch, instance_dir, child_open=receipts.os.open)

    with pytest.raises(receipts.ReceiptError, match="evidence path") as exc_info:
        with receipts._open_month_fd(instance_dir, month.name):
            raise OSError(detail)
    assert detail not in str(exc_info.value)


def test_windows_directory_flush_never_uses_crt_directory_descriptor(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Critical directory durability must use a writable raw Windows handle."""
    directory = vault / "Knowledge Base" / "_Governance" / "events"
    directory.mkdir(parents=True)
    original_open = receipts.os.open

    def reject_crt_directory(path, *args, **kwargs):  # noqa: ANN001
        if Path(path) == directory:
            pytest.fail("Windows receipt fsync attempted CRT directory access")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(receipts.os, "open", reject_crt_directory)
    receipts._fsync_directory(directory)


def test_windows_raw_directory_flush_failure_is_content_free(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raw-handle flush failure remains a content-free durable-directory refusal."""
    directory = vault / "Knowledge Base" / "_Governance" / "events"
    directory.mkdir(parents=True)
    detail = r"FlushFileBuffers C:\\private\\receipt-directory"
    handles: list[int] = []

    def fail_flush(handle: int) -> None:
        handles.append(handle)
        raise OSError(detail)

    monkeypatch.setattr(receipts, "_flush_windows_directory_handle", fail_flush, raising=False)
    with pytest.raises(receipts.ReceiptError, match="durable directory") as exc_info:
        receipts._fsync_directory(directory)

    assert handles
    assert detail not in str(exc_info.value)


def test_windows_directory_flush_uses_only_a_write_capable_exact_leaf(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flush route retains ancestors read-only and flushes only the leaf handle."""
    directory = vault / "Knowledge Base" / "_Governance" / "events"
    directory.mkdir(parents=True)
    retained: list[Path] = []
    opened: list[tuple[Path, dict[str, object]]] = []
    flushed: list[int] = []

    class Retained:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.windows_handle = 401

        def __enter__(self):
            retained.append(self.path)
            return self

        def __exit__(self, *_args) -> None:
            return None

    def open_path(path: Path, **kwargs: object) -> int:
        opened.append((path, kwargs))
        return 409

    monkeypatch.setattr(receipts, "_open_secure_directory", lambda path, **_kwargs: Retained(path), raising=False)
    monkeypatch.setattr(receipts, "_windows_open_path", open_path, raising=False)
    monkeypatch.setattr(receipts, "_windows_child_is_in_directory", lambda *_args: True, raising=False)
    monkeypatch.setattr(receipts, "_windows_handle_identity", lambda handle: (1, 2, handle), raising=False)
    monkeypatch.setattr(receipts, "_windows_close_handle", lambda _handle: None, raising=False)
    monkeypatch.setattr(receipts, "_flush_windows_directory_handle", flushed.append, raising=False)

    receipts._fsync_directory(directory)

    assert retained == [directory.parent]
    assert [path for path, _kwargs in opened] == [directory]
    assert opened[0][1]["directory"] is True
    assert opened[0][1]["access"] == 0x40000000  # GENERIC_WRITE only on the exact leaf
    assert not opened[0][1]["share"] & 0x4  # FILE_SHARE_DELETE
    assert flushed == [409]


@pytest.mark.parametrize("component", ["_Governance", "events", "instance"])
def test_windows_receipt_junction_component_is_refused_without_touching_target(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str
) -> None:
    """A real receipt-path junction never grants access to its target."""
    instance_id = "c" * 32
    events = vault / "Knowledge Base" / "_Governance" / "events"
    governance = events.parent
    outside = _outside_target(tmp_path, component)
    before = _outside_snapshot(outside)
    if component == "_Governance":
        _junction(governance, outside)
    elif component == "events":
        governance.mkdir()
        _junction(events, outside)
    else:
        events.mkdir(parents=True)
        _junction(events / instance_id, outside)
    monkeypatch.setattr(receipts, "_instance_id", lambda _conn: instance_id)

    original_open = receipts.os.open

    def reject_outside_open(path, *args, **kwargs):  # noqa: ANN001
        candidate = Path(path)
        if candidate.resolve(strict=False).is_relative_to(outside.resolve()):
            pytest.fail("receipt opened the reparse target")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(receipts.os, "open", reject_outside_open)
    with pytest.raises(receipts.ReceiptError) as exc_info:
        receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})

    assert str(outside) not in str(exc_info.value)
    assert _outside_snapshot(outside) == before


@pytest.mark.parametrize("component", ["_Governance", "events", "instance"])
def test_windows_first_instance_creation_refuses_reparse_swap_without_touching_target(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str
) -> None:
    """A creation-time junction race must not create an outside receipt tree."""
    knowledge_base = vault / "Knowledge Base"
    governance = knowledge_base / "_Governance"
    events = governance / "events"
    outside = _outside_target(tmp_path, component)
    before = _outside_snapshot(outside)
    original_instance_dir = receipts._instance_dir
    swapped = False

    def swap_after_validation(vault_root: Path, instance_id: str) -> Path:
        nonlocal swapped
        candidate = original_instance_dir(vault_root, instance_id)
        if not swapped:
            if component == "_Governance":
                _junction(governance, outside)
            elif component == "events":
                governance.mkdir(exist_ok=True)
                _junction(events, outside)
            else:
                governance.mkdir(exist_ok=True)
                events.mkdir(exist_ok=True)
                _junction(candidate, outside)
            swapped = True
        return candidate

    monkeypatch.setattr(receipts, "_instance_dir", swap_after_validation)

    original_open = receipts.os.open

    def reject_outside_open(path, *args, **kwargs):  # noqa: ANN001
        candidate = Path(path)
        if candidate.resolve(strict=False).is_relative_to(outside.resolve()):
            pytest.fail("receipt opened the swapped reparse target")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(receipts.os, "open", reject_outside_open)
    with pytest.raises(receipts.ReceiptError) as exc_info:
        receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})

    assert swapped is True
    assert str(outside) not in str(exc_info.value)
    assert _outside_snapshot(outside) == before
