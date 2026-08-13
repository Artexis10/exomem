"""Feedback 6: public POSIX capability-custody contract."""

from __future__ import annotations

import fcntl
import os
import socket
from pathlib import Path

import pytest


def _fd_snapshot() -> set[str]:
    return set(os.listdir("/proc/self/fd"))


def test_feedback6_custody_holds_edges_proves_proc_and_validates_components(
    tmp_path: Path,
) -> None:
    from protocol.custody import CustodyError, hold_directory

    run = hold_directory(tmp_path / "run", create=True, logical_ref=Path("."))
    session = run.mkdir("session-0001", logical_ref=Path("work/session-0001"))
    work = session.mkdir("work", logical_ref=Path("work/session-0001/work"))
    try:
        work.prove_supported()
        for held in (run, session, work):
            status = os.fstat(held.fd)
            assert (status.st_dev, status.st_ino) == (held.device, held.inode)
            assert held.parent_fd >= 0 and held.name and held.fd >= 0
            assert fcntl.fcntl(held.fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
            reopened = os.open(held.capability_path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                reopened_status = os.fstat(reopened)
                assert (reopened_status.st_dev, reopened_status.st_ino) == (
                    held.device,
                    held.inode,
                )
            finally:
                os.close(reopened)
        before = sorted((tmp_path / "run").rglob("*"))
        for component in ("", ".", "..", "a/b", "a\\b", "/absolute"):
            with pytest.raises(CustodyError):
                work.mkdir(component, logical_ref=Path("invalid"))
        assert sorted((tmp_path / "run").rglob("*")) == before
        outside = tmp_path / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_bytes(b"outside")
        ancestor = tmp_path / "symlinked-ancestor"
        ancestor.symlink_to(outside, target_is_directory=True)
        with pytest.raises(CustodyError):
            hold_directory(ancestor, logical_ref=Path("ancestor"))
        assert sentinel.read_bytes() == b"outside"
    finally:
        work.close()
        session.close()
        run.close()


def test_feedback6_moved_binding_recursive_retirement_never_touches_replacement(
    tmp_path: Path,
) -> None:
    from protocol.custody import CustodyBindingLost, hold_directory

    outside = tmp_path / "outside"
    outside.mkdir()
    outside_sentinel = outside / "sentinel"
    outside_sentinel.write_bytes(b"outside")
    session = hold_directory(tmp_path / "session", create=True, logical_ref=Path("session"))
    work = session.mkdir("work", logical_ref=Path("session/work"))
    sock = socket.socket(socket.AF_UNIX)
    try:
        (work.capability_path / "before").write_bytes(b"held")
        displaced = tmp_path / "displaced-session"
        (tmp_path / "session").rename(displaced)
        replacement = tmp_path / "session"
        replacement.mkdir()
        replacement_sentinel = replacement / "sentinel"
        replacement_sentinel.write_bytes(b"replacement")
        (work.capability_path / "after").write_bytes(b"held-again")
        nested = work.capability_path / "nested"
        nested.mkdir()
        (nested / "payload").write_bytes(b"payload")
        (work.capability_path / "outside-link").symlink_to(outside)
        os.mkfifo(work.capability_path / "pipe")
        sock.bind(str(work.capability_path / "socket"))
        with pytest.raises(CustodyBindingLost):
            session.assert_bound()
        work.empty_recursive(max_entries=32, max_depth=8)
        assert list(work.capability_path.iterdir()) == []
        assert work.retire(max_entries=32, max_depth=8) is False
        assert replacement_sentinel.read_bytes() == b"replacement"
        assert outside_sentinel.read_bytes() == b"outside"
    finally:
        sock.close()
        work.close()
        session.close()


def test_feedback6_publish_is_exclusive_bounded_nonblocking_and_resource_clean(
    tmp_path: Path,
) -> None:
    from protocol.custody import CustodyError, CustodyLimitExceeded, hold_directory

    baseline = _fd_snapshot()
    held = hold_directory(tmp_path / "evidence", create=True, logical_ref=Path("evidence"))
    fifo = held.capability_path / "pipe"
    sock = socket.socket(socket.AF_UNIX)
    try:
        published = held.publish_exclusive("observation.json", b"payload", max_bytes=64)
        assert published.payload == b"payload"
        assert published.sha256 == "239f59ed55e737c77147cf55ad0c1b030b6d7ee748a7426952f9b852d5a935e5"
        assert held.read_regular_bounded("observation.json", max_bytes=64) == b"payload"

        (held.capability_path / "occupied.json").write_bytes(b"racer-owned")
        with pytest.raises(CustodyError):
            held.publish_exclusive("occupied.json", b"ours", max_bytes=64)
        assert (held.capability_path / "occupied.json").read_bytes() == b"racer-owned"

        with (held.capability_path / "large.json").open("wb") as handle:
            handle.truncate(65)
        with pytest.raises(CustodyLimitExceeded):
            held.read_regular_bounded("large.json", max_bytes=64)
        os.mkfifo(fifo)
        sock.bind(str(held.capability_path / "socket"))
        (held.capability_path / "directory").mkdir()
        (held.capability_path / "link").symlink_to("observation.json")
        for name in ("pipe", "socket", "directory", "link"):
            with pytest.raises(CustodyError):
                held.read_regular_bounded(name, max_bytes=64)
        assert not any(entry.name.startswith(".observation.json.tmp") for entry in held.capability_path.iterdir())

        nested = held.mkdir("nested", logical_ref=Path("evidence/nested"))
        try:
            displaced = tmp_path / "displaced-evidence"
            (tmp_path / "evidence").rename(displaced)
            replacement = tmp_path / "evidence"
            replacement.mkdir()
            replacement_sentinel = replacement / "replacement-sentinel"
            replacement_sentinel.write_bytes(b"replacement")
            with pytest.raises(CustodyError):
                nested.publish_exclusive("after-swap.json", b"held", max_bytes=64)
            assert not (displaced / "nested" / "after-swap.json").exists()
            assert replacement_sentinel.read_bytes() == b"replacement"
            with pytest.raises(CustodyError):
                held.assert_bound()
        finally:
            nested.close()
    finally:
        sock.close()
        held.close()
    assert _fd_snapshot() == baseline


def test_feedback6_recursive_retirement_enforces_entry_and_depth_limits(tmp_path: Path) -> None:
    from protocol.custody import CustodyLimitExceeded, hold_directory

    held = hold_directory(tmp_path / "bounded", create=True, logical_ref=Path("bounded"))
    try:
        (held.capability_path / "one").write_bytes(b"1")
        (held.capability_path / "two").write_bytes(b"2")
        with pytest.raises(CustodyLimitExceeded):
            held.empty_recursive(max_entries=1, max_depth=8)
        nested = held.capability_path / "nested" / "deeper"
        nested.mkdir(parents=True)
        (nested / "payload").write_bytes(b"payload")
        with pytest.raises(CustodyLimitExceeded):
            held.empty_recursive(max_entries=32, max_depth=0)
        held.empty_recursive(max_entries=32, max_depth=8)
        assert list(held.capability_path.iterdir()) == []
    finally:
        held.close()


def test_feedback6_review_capability_probe_never_removes_a_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from protocol.custody import CustodyUnsupported, HeldDirectory, hold_directory

    root_path = tmp_path / "root"
    root = hold_directory(root_path, create=True, logical_ref=Path("root"))
    real_publish = HeldDirectory.publish_exclusive
    replacement: Path | None = None

    def replace_probe(held, *args, **kwargs):
        nonlocal replacement
        published = real_publish(held, *args, **kwargs)
        if held.name.startswith(".custody-proof-"):
            (root_path / held.name).rename(tmp_path / "displaced-proof")
            replacement = root_path / held.name
            replacement.mkdir()
        return published

    monkeypatch.setattr(HeldDirectory, "publish_exclusive", replace_probe)
    try:
        with pytest.raises(CustodyUnsupported):
            root.prove_supported()
        assert replacement is not None and replacement.is_dir()
    finally:
        root.close()


def test_feedback6_review_failed_child_binding_closes_attempt_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from protocol.custody import CustodyBindingLost, HeldDirectory, hold_directory

    root = hold_directory(tmp_path / "root", create=True, logical_ref=Path("root"))
    (tmp_path / "root" / "child").mkdir()
    baseline = _fd_snapshot()
    real_assert_bound = HeldDirectory.assert_bound

    def reject_child(held):
        if held.name == "child":
            raise CustodyBindingLost("test child binding loss")
        return real_assert_bound(held)

    monkeypatch.setattr(HeldDirectory, "assert_bound", reject_child)
    try:
        with pytest.raises(CustodyBindingLost):
            root.open_dir("child", logical_ref=Path("root/child"))
        assert _fd_snapshot() == baseline
    finally:
        root.close()


@pytest.mark.parametrize("operation", ("open_dir", "mkdir"))
def test_feedback7_failed_child_construction_closes_all_attempt_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    import protocol.custody as custody_module
    from protocol.custody import CustodyError, hold_directory

    root = hold_directory(tmp_path / "root", create=True, logical_ref=Path("root"))
    if operation == "open_dir":
        (tmp_path / "root" / "child").mkdir()
    child_identity: os.stat_result | None = None
    real_open = custody_module.os.open

    def observe_child(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal child_identity
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == "child" and dir_fd == root.fd:
            child_identity = os.fstat(descriptor)
        return descriptor
    real_dup = custody_module.os.dup

    def fail_duplicate(_descriptor: int) -> int:
        raise OSError("injected duplicate failure")

    monkeypatch.setattr(custody_module.os, "open", observe_child)
    monkeypatch.setattr(custody_module.os, "dup", fail_duplicate)
    try:
        with pytest.raises(OSError):
            if operation == "open_dir":
                root.open_dir("child", logical_ref=Path("root/child"))
            else:
                root.mkdir("child", logical_ref=Path("root/child"))
        assert child_identity is not None
        held_identities = []
        for descriptor in Path("/proc/self/fd").iterdir():
            try:
                held_identities.append(os.stat(descriptor))
            except FileNotFoundError:
                pass
        assert not any(
            (status.st_dev, status.st_ino) == (child_identity.st_dev, child_identity.st_ino)
            for status in held_identities
        )
    finally:
        monkeypatch.setattr(custody_module.os, "open", real_open)
        monkeypatch.setattr(custody_module.os, "dup", real_dup)
        root.close()


def test_feedback7_retirement_leaves_replacement_inserted_before_rmdir_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import protocol.custody as custody_module
    from protocol.custody import CustodyBindingLost, hold_directory

    parent = hold_directory(tmp_path / "parent", create=True, logical_ref=Path("parent"))
    held = parent.mkdir("child", logical_ref=Path("parent/child"))
    replacement = tmp_path / "parent" / "child"
    displaced = tmp_path / "displaced-child"
    real_rename = custody_module.os.rename
    swapped = False

    def replace_before_quarantine(source, destination, *args, **kwargs):
        nonlocal swapped
        if source == "child" and not swapped:
            swapped = True
            real_rename("child", "displaced", src_dir_fd=parent.fd, dst_dir_fd=parent.fd)
            replacement.mkdir()
            (replacement / "sentinel").write_bytes(b"replacement")
        return real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(custody_module.os, "rename", replace_before_quarantine)
    try:
        assert not held.retire(max_entries=8, max_depth=2)
        assert (replacement / "sentinel").read_bytes() == b"replacement"
    finally:
        held.close()
        parent.close()


def test_feedback8_recursive_retirement_refuses_a_replacement_at_the_final_name_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import protocol.custody as custody_module
    from protocol.custody import CustodyBindingLost, hold_directory

    root = hold_directory(tmp_path / "root", create=True, logical_ref=Path("root"))
    try:
        for kind, swap_on_call in (("file", 1), ("directory", 1)):
            case = root.mkdir(kind, logical_ref=Path("root") / kind)
            target = case.capability_path / "target"
            displaced = case.capability_path / "displaced"
            if kind == "file":
                target.write_bytes(b"held")
            else:
                target.mkdir()
            original_stat = custody_module.os.stat
            calls = 0

            def replace_after_identity(name, *args, **kwargs):
                nonlocal calls
                status = original_stat(name, *args, **kwargs)
                if name == "target" and kwargs.get("dir_fd") == case.fd:
                    calls += 1
                    if calls == swap_on_call:
                        target.rename(displaced)
                        if kind == "file":
                            target.write_bytes(b"replacement")
                        else:
                            target.mkdir()
                            (target / "sentinel").write_bytes(b"replacement")
                return status

            monkeypatch.setattr(custody_module.os, "stat", replace_after_identity)
            try:
                with pytest.raises(CustodyBindingLost):
                    case.empty_recursive(max_entries=8, max_depth=2)
                if kind == "file":
                    assert target.read_bytes() == b"replacement"
                else:
                    assert (target / "sentinel").read_bytes() == b"replacement"
            finally:
                monkeypatch.setattr(custody_module.os, "stat", original_stat)
                case.close()
    finally:
        root.close()


@pytest.mark.parametrize("kind", ("file", "directory"))
def test_feedback9_recursive_retirement_quarantines_the_inspected_entry_before_final_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    import protocol.custody as custody_module
    from protocol.custody import CustodyBindingLost, hold_directory

    root = hold_directory(tmp_path / "root", create=True, logical_ref=Path("root"))
    target = root.capability_path / "target"
    displaced = root.capability_path / "displaced"
    if kind == "file":
        target.write_bytes(b"held")
    else:
        target.mkdir()
        (target / "held").write_bytes(b"held")
    original_rename = custody_module.os.rename
    swapped = False

    def replace_before_quarantine(source, destination, *args, **kwargs):
        nonlocal swapped
        if source == "target" and not swapped:
            swapped = True
            original_rename("target", "displaced", src_dir_fd=root.fd, dst_dir_fd=root.fd)
            if kind == "file":
                target.write_bytes(b"replacement")
            else:
                target.mkdir()
                (target / "sentinel").write_bytes(b"replacement")
        return original_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(custody_module.os, "rename", replace_before_quarantine)
    try:
        with pytest.raises(CustodyBindingLost):
            root.empty_recursive(max_entries=8, max_depth=2)
        assert swapped
        if kind == "file":
            assert target.read_bytes() == b"replacement"
        else:
            assert (target / "sentinel").read_bytes() == b"replacement"
    finally:
        root.close()


def test_feedback10_final_removal_uses_private_quarantine_and_preserves_public_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import protocol.custody as custody_module
    from protocol.custody import hold_directory

    root = hold_directory(tmp_path / "root", create=True, logical_ref=Path("root"))
    target = root.mkdir("target", logical_ref=Path("root/target"))
    old_public_boundary = root.capability_path / ".custody-retire-public-boundary"
    old_public_boundary.write_bytes(b"replacement")
    original_uuid4 = custody_module.uuid.uuid4
    original_rename = custody_module.os.rename
    moves: list[tuple[object, object, int | None, int | None]] = []

    class _FixedUuid:
        hex = "public-boundary"

    def capture_move(source, destination, *args, **kwargs):
        moves.append((source, destination, kwargs.get("src_dir_fd"), kwargs.get("dst_dir_fd")))
        return original_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(custody_module.uuid, "uuid4", lambda: _FixedUuid())
    monkeypatch.setattr(custody_module.os, "rename", capture_move)
    try:
        assert target.retire(max_entries=8, max_depth=2)
        assert not (root.capability_path / "target").exists()
        assert old_public_boundary.read_bytes() == b"replacement"
        final_moves = [move for move in moves if move[0] == "target"]
        assert len(final_moves) == 1
        assert final_moves[0][3] != root.fd
    finally:
        monkeypatch.setattr(custody_module.uuid, "uuid4", original_uuid4)
        monkeypatch.setattr(custody_module.os, "rename", original_rename)
        target.close()
        root.close()


def test_feedback11_probe_cleanup_uses_private_quarantine_not_a_raw_rmdir_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import protocol.custody as custody_module
    from protocol.custody import CustodyUnsupported, HeldDirectory, hold_directory

    root_path = tmp_path / "root"
    root = hold_directory(root_path, create=True, logical_ref=Path("root"))
    original_retire = HeldDirectory.retire
    original_rmdir = custody_module.os.rmdir
    removals: list[tuple[object, int | None]] = []

    def leave_probe_behind(held, **kwargs):
        if held.name.startswith(".custody-proof-"):
            return False
        return original_retire(held, **kwargs)

    def record_removal(name, *args, **kwargs):
        removals.append((name, kwargs.get("dir_fd")))
        return original_rmdir(name, *args, **kwargs)

    monkeypatch.setattr(HeldDirectory, "retire", leave_probe_behind)
    monkeypatch.setattr(custody_module.os, "rmdir", record_removal)
    try:
        with pytest.raises(CustodyUnsupported):
            root.prove_supported()
        assert not any(
            isinstance(name, str)
            and name.startswith(".custody-proof-")
            and descriptor == root.fd
            for name, descriptor in removals
        )
    finally:
        root.close()
