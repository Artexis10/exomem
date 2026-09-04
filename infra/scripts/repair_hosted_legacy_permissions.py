#!/usr/bin/env python3
"""Repair fsGroup-only drift on one proven v2 Hosted volume.

The volume root itself is deliberately never changed.  Only the three bound
runtime subtrees are traversed, after all three v2 identity markers and every
entry have passed a no-follow, regular-file-only, bounded preflight.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import NamedTuple

_MARKER = ".exomem-hosted-cell.json"
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_ROOT_NAMES = (("vault", "vault"), ("state", "state"), ("log", "logs"))
_RUNTIME_ROOTS = {
    "vault": "/var/lib/exomem/vault",
    "state": "/var/lib/exomem/state",
    "log": "/var/lib/exomem/logs",
}
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


class RepairRefusal(RuntimeError):
    """A stable, content-free refusal."""


class _Entry(NamedTuple):
    relative: tuple[str, ...]
    signature: tuple[int, int, int, int, int]
    original_mode: int


class _Snapshot(NamedTuple):
    kind: str
    root: Path
    entries: tuple[_Entry, ...]
    content_sha256: str
    total_bytes: int


def _frame(digest: hashlib._Hash, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        value.st_nlink,
        value.st_size,
    )


def _read_all(descriptor: int, *, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(1024 * 1024, limit + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise RepairRefusal("filesystem tree exceeds its bounded limits")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _expected_marker(
    *, kind: str, cell_id: str, vault_id: str, runtime_uid: int, runtime_gid: int
) -> dict[str, object]:
    return {
        "binding_version": 2,
        "cell_id": cell_id,
        "vault_id": vault_id,
        "vault_root": _RUNTIME_ROOTS["vault"],
        "state_root": _RUNTIME_ROOTS["state"],
        "log_root": _RUNTIME_ROOTS["log"],
        "root_kind": kind,
        "runtime_uid": runtime_uid,
        "runtime_gid": runtime_gid,
    }


def _validate_marker(root_descriptor: int, expected: dict[str, object]) -> None:
    try:
        descriptor = os.open(_MARKER, _FILE_FLAGS, dir_fd=root_descriptor)
    except OSError as error:
        raise RepairRefusal("binding marker is invalid") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size < 2
            or before.st_size > 16_384
        ):
            raise RepairRefusal("binding marker is invalid")
        payload = _read_all(descriptor, limit=16_384)
        after = os.fstat(descriptor)
        if (
            _signature(before) != _signature(after)
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
            or len(payload) != before.st_size
        ):
            raise RepairRefusal("binding marker is invalid")
    finally:
        os.close(descriptor)
    try:
        decoded = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise RepairRefusal("binding marker is invalid") from error
    if decoded != expected:
        raise RepairRefusal("binding marker is invalid")


def _validate_absolute_no_symlink_path(path: Path) -> None:
    if not path.is_absolute() or Path(os.path.normpath(str(path))) != path:
        raise RepairRefusal("volume root is invalid")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            value = current.lstat()
        except OSError as error:
            raise RepairRefusal("volume root is invalid") from error
        if stat.S_ISLNK(value.st_mode):
            raise RepairRefusal("volume root is invalid")


def _snapshot_root(
    *,
    root: Path,
    kind: str,
    expected_marker: dict[str, object],
    max_entries: int,
    max_bytes: int,
) -> _Snapshot:
    digest = hashlib.sha256()
    _frame(digest, b"exomem.hosted-permission-repair-content/v1")
    _frame(digest, kind.encode("ascii"))
    entries: list[_Entry] = []
    total_bytes = 0
    try:
        root_descriptor = os.open(root, _DIRECTORY_FLAGS)
    except OSError as error:
        raise RepairRefusal("bound filesystem root is unavailable") from error
    try:
        root_stat = os.fstat(root_descriptor)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise RepairRefusal("unsafe filesystem entry")
        root_device = root_stat.st_dev
        _validate_marker(root_descriptor, expected_marker)

        def add_entry(relative: tuple[str, ...], value: os.stat_result) -> None:
            if value.st_dev != root_device:
                raise RepairRefusal("unsafe filesystem entry")
            entries.append(_Entry(relative, _signature(value), stat.S_IMODE(value.st_mode)))
            if len(entries) > max_entries:
                raise RepairRefusal("filesystem tree exceeds its bounded limits")

        def walk(directory_descriptor: int, relative: tuple[str, ...]) -> None:
            nonlocal total_bytes
            directory = os.fstat(directory_descriptor)
            if not stat.S_ISDIR(directory.st_mode):
                raise RepairRefusal("unsafe filesystem entry")
            add_entry(relative, directory)
            _frame(digest, b"directory")
            _frame(digest, "/".join(relative).encode("utf-8"))
            try:
                names = sorted(os.listdir(directory_descriptor))
            except OSError as error:
                raise RepairRefusal("unsafe filesystem entry") from error
            for name in names:
                if not name or name in {".", ".."} or "/" in name or "\x00" in name:
                    raise RepairRefusal("unsafe filesystem entry")
                child_relative = (*relative, name)
                try:
                    observed = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
                except OSError as error:
                    raise RepairRefusal("unsafe filesystem entry") from error
                flags = _DIRECTORY_FLAGS if stat.S_ISDIR(observed.st_mode) else _FILE_FLAGS
                try:
                    child_descriptor = os.open(name, flags, dir_fd=directory_descriptor)
                except OSError as error:
                    raise RepairRefusal("unsafe filesystem entry") from error
                try:
                    opened = os.fstat(child_descriptor)
                    if _signature(opened) != _signature(observed):
                        raise RepairRefusal("unsafe filesystem entry")
                    if stat.S_ISDIR(opened.st_mode):
                        walk(child_descriptor, child_relative)
                    elif stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1:
                        add_entry(child_relative, opened)
                        total_bytes += opened.st_size
                        if total_bytes > max_bytes:
                            raise RepairRefusal("filesystem tree exceeds its bounded limits")
                        payload = _read_all(
                            child_descriptor, limit=max_bytes - total_bytes + opened.st_size
                        )
                        reread = os.fstat(child_descriptor)
                        if (
                            _signature(opened) != _signature(reread)
                            or opened.st_mtime_ns != reread.st_mtime_ns
                            or opened.st_ctime_ns != reread.st_ctime_ns
                            or len(payload) != opened.st_size
                        ):
                            raise RepairRefusal("unsafe filesystem entry")
                        _frame(digest, b"file")
                        _frame(digest, "/".join(child_relative).encode("utf-8"))
                        _frame(digest, payload)
                    else:
                        raise RepairRefusal("unsafe filesystem entry")
                finally:
                    os.close(child_descriptor)

        walk(root_descriptor, ())
    finally:
        os.close(root_descriptor)
    return _Snapshot(kind, root, tuple(entries), digest.hexdigest(), total_bytes)


def _converge_snapshot(snapshot: _Snapshot, *, runtime_uid: int, runtime_gid: int) -> None:
    expected = {entry.relative: entry for entry in snapshot.entries}
    visited: set[tuple[str, ...]] = set()
    try:
        root_descriptor = os.open(snapshot.root, _DIRECTORY_FLAGS)
    except OSError as error:
        raise RepairRefusal("bound filesystem root is unavailable") from error

    def converge(descriptor: int, relative: tuple[str, ...]) -> None:
        current = os.fstat(descriptor)
        recorded = expected.get(relative)
        if recorded is None or _signature(current) != recorded.signature:
            raise RepairRefusal("filesystem tree changed during repair")
        visited.add(relative)
        desired_mode = (
            (0o700 if not relative else (recorded.original_mode & 0o700) | 0o100)
            if stat.S_ISDIR(current.st_mode)
            else recorded.original_mode & 0o700
        )
        try:
            os.fchown(descriptor, runtime_uid, runtime_gid)
            os.fchmod(descriptor, desired_mode)
            os.fsync(descriptor)
        except OSError as error:
            raise RepairRefusal("filesystem metadata could not be repaired") from error
        repaired = os.fstat(descriptor)
        if (
            _signature(repaired) != recorded.signature
            or repaired.st_uid != runtime_uid
            or repaired.st_gid != runtime_gid
            or stat.S_IMODE(repaired.st_mode) != desired_mode
        ):
            raise RepairRefusal("filesystem metadata could not be repaired")

    def walk(directory_descriptor: int, relative: tuple[str, ...]) -> None:
        converge(directory_descriptor, relative)
        try:
            names = sorted(os.listdir(directory_descriptor))
        except OSError as error:
            raise RepairRefusal("filesystem tree changed during repair") from error
        for name in names:
            child_relative = (*relative, name)
            try:
                observed = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            except OSError as error:
                raise RepairRefusal("filesystem tree changed during repair") from error
            flags = _DIRECTORY_FLAGS if stat.S_ISDIR(observed.st_mode) else _FILE_FLAGS
            try:
                child_descriptor = os.open(name, flags, dir_fd=directory_descriptor)
            except OSError as error:
                raise RepairRefusal("filesystem tree changed during repair") from error
            try:
                if stat.S_ISDIR(observed.st_mode):
                    walk(child_descriptor, child_relative)
                elif stat.S_ISREG(observed.st_mode) and observed.st_nlink == 1:
                    converge(child_descriptor, child_relative)
                else:
                    raise RepairRefusal("filesystem tree changed during repair")
            finally:
                os.close(child_descriptor)

    try:
        walk(root_descriptor, ())
    finally:
        os.close(root_descriptor)
    if visited != set(expected):
        raise RepairRefusal("filesystem tree changed during repair")


def _combined_content_sha256(snapshots: tuple[_Snapshot, ...]) -> str:
    digest = hashlib.sha256()
    _frame(digest, b"exomem.hosted-permission-repair-volume/v1")
    for snapshot in snapshots:
        _frame(digest, snapshot.kind.encode("ascii"))
        _frame(digest, snapshot.content_sha256.encode("ascii"))
    return digest.hexdigest()


def repair_volume_permissions(
    *,
    volume_root: Path | str,
    cell_id: str,
    vault_id: str,
    runtime_uid: int,
    runtime_gid: int,
    max_entries: int = 100_000,
    max_bytes: int = 10 * 1024 * 1024 * 1024,
) -> dict[str, object]:
    """Converge metadata only after proving one exact v2 volume identity."""

    if (
        _IDENTITY.fullmatch(cell_id) is None
        or _IDENTITY.fullmatch(vault_id) is None
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in (runtime_uid, runtime_gid, max_entries, max_bytes)
        )
    ):
        raise RepairRefusal("repair request is invalid")
    volume = Path(volume_root)
    _validate_absolute_no_symlink_path(volume)
    volume_stat = volume.lstat()
    if not stat.S_ISDIR(volume_stat.st_mode):
        raise RepairRefusal("volume root is invalid")

    snapshots = tuple(
        _snapshot_root(
            root=volume / directory,
            kind=kind,
            expected_marker=_expected_marker(
                kind=kind,
                cell_id=cell_id,
                vault_id=vault_id,
                runtime_uid=runtime_uid,
                runtime_gid=runtime_gid,
            ),
            max_entries=max_entries,
            max_bytes=max_bytes,
        )
        for kind, directory in _ROOT_NAMES
    )
    entry_count = sum(len(snapshot.entries) for snapshot in snapshots)
    total_bytes = sum(snapshot.total_bytes for snapshot in snapshots)
    if entry_count > max_entries or total_bytes > max_bytes:
        raise RepairRefusal("filesystem tree exceeds its bounded limits")
    before = _combined_content_sha256(snapshots)

    for snapshot in snapshots:
        _converge_snapshot(snapshot, runtime_uid=runtime_uid, runtime_gid=runtime_gid)

    after_snapshots = tuple(
        _snapshot_root(
            root=volume / directory,
            kind=kind,
            expected_marker=_expected_marker(
                kind=kind,
                cell_id=cell_id,
                vault_id=vault_id,
                runtime_uid=runtime_uid,
                runtime_gid=runtime_gid,
            ),
            max_entries=max_entries,
            max_bytes=max_bytes,
        )
        for kind, directory in _ROOT_NAMES
    )
    after = _combined_content_sha256(after_snapshots)
    if (
        after != before
        or sum(len(snapshot.entries) for snapshot in after_snapshots) != entry_count
        or sum(snapshot.total_bytes for snapshot in after_snapshots) != total_bytes
    ):
        raise RepairRefusal("filesystem content changed during repair")
    unchanged_volume = volume.lstat()
    if (
        _signature(unchanged_volume) != _signature(volume_stat)
        or unchanged_volume.st_uid != volume_stat.st_uid
        or unchanged_volume.st_gid != volume_stat.st_gid
        or stat.S_IMODE(unchanged_volume.st_mode) != stat.S_IMODE(volume_stat.st_mode)
    ):
        raise RepairRefusal("volume root changed during repair")
    return {
        "status": "repaired",
        "content_sha256_before": before,
        "content_sha256_after": after,
        "root_count": 3,
        "entry_count": entry_count,
        "total_bytes": total_bytes,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Repair one exact legacy Hosted v2 volume")
    parser.add_argument("--volume-root", type=Path, required=True)
    parser.add_argument("--cell-id", required=True)
    parser.add_argument("--vault-id", required=True)
    parser.add_argument("--runtime-uid", type=int, required=True)
    parser.add_argument("--runtime-gid", type=int, required=True)
    parser.add_argument("--max-entries", type=int, default=100_000)
    parser.add_argument("--max-bytes", type=int, default=10 * 1024 * 1024 * 1024)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        result = repair_volume_permissions(
            volume_root=arguments.volume_root,
            cell_id=arguments.cell_id,
            vault_id=arguments.vault_id,
            runtime_uid=arguments.runtime_uid,
            runtime_gid=arguments.runtime_gid,
            max_entries=arguments.max_entries,
            max_bytes=arguments.max_bytes,
        )
    except RepairRefusal as error:
        print(json.dumps({"status": "refused", "refusal": str(error)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
