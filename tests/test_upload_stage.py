"""The upload stage: bytes copied once before the boundary, then adopted by rename.

The guarantees under test are the ones that a hold which no longer reads the payload still needs:
a stage changed after it was staged publishes nothing, a destination that appears keeps its file,
and a stage whose process died is swept while a live process's stage is not.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from exomem import upload_stage, vault

PAYLOAD = b"upload stage payload " * 4096


def _too_large() -> BaseException:
    return ValueError("too large")


def _binary_write(path: Path, staged: upload_stage.StagedUpload) -> vault.PlannedWrite:
    return vault.PlannedWrite(
        path,
        vault.PreparedBinaryContent(staged.open(), staged.size, staged.sha256, staged=staged),
        create_only=True,
        expected_hash=vault.MISSING_CONTENT_HASH,
    )


def _stage_entries(vault_root: Path) -> list[str]:
    root = upload_stage.stage_root(vault_root)
    return sorted(entry.name for entry in root.iterdir()) if root.exists() else []


def _workspace_residue(destination: Path) -> list[str]:
    # A refused batch removes the folder it created, so a missing parent holds no residue either.
    if not destination.parent.exists():
        return []
    return [entry.name for entry in destination.parent.iterdir() if entry.name.startswith(".exomem-batch-")]


def test_a_staged_upload_is_installed_by_rename_without_copying(tmp_path: Path) -> None:
    destination = tmp_path / "Evidence" / "payload.bin"

    with upload_stage.stage(tmp_path, io.BytesIO(PAYLOAD), limit=len(PAYLOAD), too_large=_too_large) as staged:
        vault.batch_atomic_write([_binary_write(destination, staged)], vault_root=tmp_path)

        # The installed file is the staged inode itself: a copy would have a new one.
        assert destination.stat().st_ino == staged.inode
    assert destination.read_bytes() == PAYLOAD
    assert _stage_entries(tmp_path) == []
    assert _workspace_residue(destination) == []


def test_a_stage_edited_after_staging_publishes_nothing(tmp_path: Path) -> None:
    destination = tmp_path / "Evidence" / "payload.bin"

    with upload_stage.stage(tmp_path, io.BytesIO(PAYLOAD), limit=len(PAYLOAD), too_large=_too_large) as staged:
        before = staged.path.stat()
        edited = bytearray(PAYLOAD)
        edited[0] ^= 0xFF
        with staged.path.open("r+b") as handle:
            handle.write(bytes(edited))
        # The strongest edit keeps the size and puts the mtime back; only the ctime still moves.
        os.utime(staged.path, ns=(before.st_atime_ns, before.st_mtime_ns))

        with pytest.raises(vault.PathGuardError) as refused:
            vault.batch_atomic_write([_binary_write(destination, staged)], vault_root=tmp_path)

    assert refused.value.code == "PATH_GUARD_CONTENT"
    assert not destination.exists()
    assert _workspace_residue(destination) == []
    assert _stage_entries(tmp_path) == []


def test_a_destination_that_appears_before_the_commit_keeps_its_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "Evidence" / "payload.bin"
    intruder = b"another writer's bytes"

    def appears(path: Path, *, restoring: bool) -> None:
        # The destination is free at the planning check and taken just before the install.
        path.write_bytes(intruder)

    monkeypatch.setattr(vault, "_before_batch_destination_published", appears)
    with upload_stage.stage(tmp_path, io.BytesIO(PAYLOAD), limit=len(PAYLOAD), too_large=_too_large) as staged:
        with pytest.raises(vault.PathGuardError):
            vault.batch_atomic_write([_binary_write(destination, staged)], vault_root=tmp_path)

    assert destination.read_bytes() == intruder
    assert _workspace_residue(destination) == []


def test_a_platform_that_cannot_witness_a_change_copies_and_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    destination = tmp_path / "Evidence" / "payload.bin"
    monkeypatch.setattr(vault, "STAT_GENERATION_TRUSTED", False)

    with caplog.at_level(logging.INFO, logger="exomem.vault"):
        with upload_stage.stage(tmp_path, io.BytesIO(PAYLOAD), limit=len(PAYLOAD), too_large=_too_large) as staged:
            vault.batch_atomic_write([_binary_write(destination, staged)], vault_root=tmp_path)
            assert destination.stat().st_ino != staged.inode

    assert destination.read_bytes() == PAYLOAD
    assert "upload stage copied under the boundary" in caplog.text


def test_a_stage_over_its_limit_leaves_nothing_behind(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="too large"):
        with upload_stage.stage(tmp_path, io.BytesIO(PAYLOAD), limit=len(PAYLOAD) - 1, too_large=_too_large):
            pytest.fail("a stage over its limit must not be yielded")

    assert _stage_entries(tmp_path) == []


def test_a_stage_digest_matches_its_bytes(tmp_path: Path) -> None:
    with upload_stage.stage(tmp_path, io.BytesIO(PAYLOAD), limit=len(PAYLOAD), too_large=_too_large) as staged:
        assert staged.sha256 == hashlib.sha256(PAYLOAD).hexdigest()
        assert staged.size == len(PAYLOAD)
        assert staged.path.read_bytes() == PAYLOAD


_HOLD_A_STAGE = textwrap.dedent(
    """
    import io, sys, time
    from pathlib import Path
    from exomem import upload_stage

    with upload_stage.stage(Path(sys.argv[1]), io.BytesIO(b"x" * 1024), limit=4096, too_large=lambda: ValueError()) as staged:
        print(staged.path.name, flush=True)
        time.sleep(600)
    """
)


def _hold_a_stage(vault_root: Path) -> tuple[subprocess.Popen[str], str]:
    process = subprocess.Popen(
        [sys.executable, "-c", _HOLD_A_STAGE, str(vault_root)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    return process, process.stdout.readline().strip()


def test_a_stage_left_by_a_killed_process_is_swept_and_a_live_stage_stays(tmp_path: Path) -> None:
    killed, killed_part = _hold_a_stage(tmp_path)
    live, live_part = _hold_a_stage(tmp_path)
    try:
        killed.send_signal(signal.SIGKILL)
        killed.wait(timeout=30)
        assert killed_part in _stage_entries(tmp_path)

        report = upload_stage.sweep(tmp_path)

        assert (report.removed, report.live) == (1, 1)
        assert killed_part not in _stage_entries(tmp_path)
        assert live_part in _stage_entries(tmp_path)
    finally:
        live.send_signal(signal.SIGKILL)
        live.wait(timeout=30)
        for stream in (killed.stdout, live.stdout):
            if stream is not None:
                stream.close()

    # Once its owner is dead the same stage is proven orphaned and goes.
    assert upload_stage.sweep(tmp_path).removed == 1
    assert _stage_entries(tmp_path) == []


def test_a_part_without_its_lock_file_is_reported_and_left_in_place(tmp_path: Path) -> None:
    root = upload_stage.stage_root(tmp_path)
    root.mkdir(parents=True)
    stray = root / ("a" * 32 + ".part")
    stray.write_bytes(b"bytes of unknown ownership")

    report = upload_stage.sweep(tmp_path)

    assert (report.removed, report.unproven) == (0, 1)
    assert stray.read_bytes() == b"bytes of unknown ownership"
