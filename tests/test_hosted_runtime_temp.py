from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from exomem.hosted_runtime_temp import (
    HostedRuntimeTempAuthority,
    HostedRuntimeTempUnavailable,
    prepare_hosted_runtime_temp,
)
from exomem.hosted_transfer import TRANSFER_RUNTIME_TEMP_QUOTA_BYTES


def test_locked_startup_clears_runtime_temp_without_following_symlinks(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    runtime_root = state_root / "tmp" / "runtime"
    nested = runtime_root / "nested"
    nested.mkdir(parents=True)
    (nested / "multipart.tmp").write_bytes(b"stale")
    victim = tmp_path / "victim"
    victim.write_bytes(b"must survive")
    link = runtime_root / "diarizer-output.tmp"
    try:
        link.symlink_to(victim)
    except OSError:
        pytest.skip("symlinks unavailable")

    prepared = prepare_hosted_runtime_temp(
        state_root,
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )

    assert prepared == runtime_root
    assert list(runtime_root.iterdir()) == []
    assert victim.read_bytes() == b"must survive"
    root_stat = runtime_root.lstat()
    assert (root_stat.st_uid, root_stat.st_gid) == (os.geteuid(), os.getegid())
    assert stat.S_IMODE(root_stat.st_mode) == 0o700


def test_runtime_temp_authority_counts_disk_and_active_reservations(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    runtime_root = prepare_hosted_runtime_temp(
        state_root,
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )
    authority = HostedRuntimeTempAuthority(
        runtime_root,
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )
    existing = runtime_root / "existing.tmp"
    existing.write_bytes(b"x" * (TRANSFER_RUNTIME_TEMP_QUOTA_BYTES - 1024))
    existing.chmod(0o600)

    with pytest.raises(HostedRuntimeTempUnavailable):
        with authority.reserve(2048):
            pass

    existing.unlink()
    with authority.reserve(TRANSFER_RUNTIME_TEMP_QUOTA_BYTES - 1024):
        with pytest.raises(HostedRuntimeTempUnavailable):
            with authority.reserve(2048):
                pass

    with authority.reserve(2048) as reserved_root:
        assert reserved_root == runtime_root


def test_runtime_temp_authority_rejects_unsafe_or_overlinked_entries(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    runtime_root = prepare_hosted_runtime_temp(
        state_root,
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )
    authority = HostedRuntimeTempAuthority(
        runtime_root,
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )
    first = runtime_root / "first.tmp"
    second = runtime_root / "second.tmp"
    first.write_bytes(b"unsafe")
    first.chmod(0o600)
    try:
        os.link(first, second)
    except OSError:
        pytest.skip("hard links unavailable")

    with pytest.raises(HostedRuntimeTempUnavailable):
        with authority.reserve(1):
            pass
