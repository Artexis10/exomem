from __future__ import annotations

import gzip
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from exomem import cloud_import
from exomem.cloud_import import (
    ARCHIVE_MEMBER_REFUSED,
    ARCHIVE_TOO_LARGE,
    ARCHIVE_UNREADABLE,
    IMPORT_STAGING_UNAVAILABLE,
    STORAGE_ALLOWANCE_EXCEEDED,
    ArchiveRefused,
    unpack_stream,
)

MODULE = Path(cloud_import.__file__)
CANARY = "canary-zq7vx-member-name"
GiB = 1024**3


def _file(name: str, data: bytes = b"note\n", *, mtime: int = 1_700_000_000) -> tuple:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = mtime
    return (info, data)


def _dir(name: str, *, mtime: int = 1_700_000_000) -> tuple:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mtime = mtime
    return (info, None)


def _typed(name: str, member_type: bytes, *, linkname: str = "") -> tuple:
    info = tarfile.TarInfo(name)
    info.type = member_type
    info.linkname = linkname
    return (info, None)


def _archive(*members: tuple, fmt: int = tarfile.PAX_FORMAT) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=fmt) as archive:
        for info, data in members:
            archive.addfile(info, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()


def _unpack(payload: bytes, staging: Path, **limits) -> cloud_import.UnpackResult:
    limits.setdefault("max_bytes", GiB)
    limits.setdefault("reserve_bytes", 0)
    return unpack_stream(io.BytesIO(payload), staging, **limits)


def _refusal(payload: bytes, staging: Path, **limits) -> ArchiveRefused:
    with pytest.raises(ArchiveRefused) as caught:
        _unpack(payload, staging, **limits)
    assert not staging.exists(), "a refused import must leave no staging directory"
    return caught.value


def test_a_clean_vault_archive_unpacks_into_staging(tmp_path: Path) -> None:
    payload = _archive(
        _dir("."),
        _dir("./Knowledge Base", mtime=1_600_000_000),
        _dir("./Knowledge Base/Notes"),
        _file("./Knowledge Base/Notes/første idé.md", b"# First\n", mtime=1_650_000_000),
        _file("./Knowledge Base/index.md", b"index\n"),
        _file("./attachments/diagram.png", bytes(range(256)) * 64),
        _file("./empty.md", b""),
    )
    staging = tmp_path / ".import-1"

    result = _unpack(payload, staging)

    assert result == cloud_import.UnpackResult(
        members=7, files=4, directories=3, bytes=len(b"# First\nindex\n") + 256 * 64
    )
    note = staging / "Knowledge Base" / "Notes" / "første idé.md"
    assert note.read_bytes() == b"# First\n"
    assert (staging / "attachments" / "diagram.png").read_bytes() == bytes(range(256)) * 64
    assert (staging / "empty.md").read_bytes() == b""
    assert int(note.stat().st_mtime) == 1_650_000_000
    assert int((staging / "Knowledge Base").stat().st_mtime) == 1_600_000_000
    assert stat.S_IMODE(note.stat().st_mode) == 0o600
    assert stat.S_IMODE((staging / "Knowledge Base").stat().st_mode) == 0o700
    assert stat.S_IMODE(staging.stat().st_mode) == 0o700


def test_a_gzip_compressed_stream_is_accepted(tmp_path: Path) -> None:
    payload = gzip.compress(_archive(_file("notes/a.md", b"a\n")))

    result = _unpack(payload, tmp_path / "staging")

    assert result.files == 1
    assert (tmp_path / "staging" / "notes" / "a.md").read_bytes() == b"a\n"


@pytest.mark.parametrize(
    ("member", "reason"),
    [
        (_typed("notes/link.md", tarfile.SYMTYPE, linkname="/data/host"), "link"),
        (_typed("notes/hard.md", tarfile.LNKTYPE, linkname="notes/a.md"), "link"),
        (_typed("notes/pipe", tarfile.FIFOTYPE), "special"),
        (_typed("notes/tty", tarfile.CHRTYPE), "special"),
        (_typed("notes/disk", tarfile.BLKTYPE), "special"),
        (_typed("notes/contiguous", tarfile.CONTTYPE), "special"),
        (_typed("notes/sparse", tarfile.GNUTYPE_SPARSE), "special"),
    ],
)
def test_links_and_special_entries_are_refused(tmp_path: Path, member: tuple, reason: str) -> None:
    payload = _archive(_file("notes/a.md"), member, fmt=tarfile.GNU_FORMAT)

    refused = _refusal(payload, tmp_path / "staging")

    assert (refused.code, refused.reason, refused.member_index) == (
        ARCHIVE_MEMBER_REFUSED,
        reason,
        2,
    )


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("/etc/passwd", "absolute"),
        ("../../host/credentials", "parent"),
        ("./../outside.md", "parent"),
        ("notes/../../outside.md", "parent"),
        ("notes/..", "parent"),
        ("notes/line\nbreak.md", "name"),
        ("notes/tab\there.md", "name"),
        ("notes/del\x7f.md", "name"),
        ("notes/" + "x" * 256 + ".md", "name"),
        ("/".join(["d" * 200] * 21), "name"),
        ("notes/bad-\udcff.md", "name"),
    ],
)
def test_unsafe_and_invalid_names_are_refused(tmp_path: Path, name: str, reason: str) -> None:
    payload = _archive_with_raw_name(name)

    refused = _refusal(payload, tmp_path / "staging")

    assert (refused.code, refused.reason, refused.member_index) == (
        ARCHIVE_MEMBER_REFUSED,
        reason,
        2,
    )


def _archive_with_raw_name(name: str) -> bytes:
    """A clean first member, then one whose PAX path is `name` verbatim."""

    buffer = io.BytesIO()
    with tarfile.open(
        fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT, errors="surrogateescape"
    ) as archive:
        first, data = _file("notes/a.md")
        archive.addfile(first, io.BytesIO(data))
        hostile = tarfile.TarInfo("placeholder")
        hostile.size = 1
        hostile.pax_headers = {"path": name}
        archive.addfile(hostile, io.BytesIO(b"x"))
    return buffer.getvalue()


def test_a_path_escape_writes_nothing_outside_staging(tmp_path: Path) -> None:
    """The spec scenario: `../../host/credentials` never lands anywhere."""

    volume = tmp_path / "data"
    (volume / "host").mkdir(parents=True)
    staging = volume / "vault-staging" / ".import-1"
    staging.parent.mkdir()

    _refusal(_archive_with_raw_name("../../host/credentials"), staging)

    assert list((volume / "host").iterdir()) == []
    assert list(staging.parent.iterdir()) == []


def test_a_second_member_at_a_written_path_is_refused(tmp_path: Path) -> None:
    payload = _archive(_file("notes/a.md", b"first"), _file("./notes/a.md", b"second"))

    refused = _refusal(payload, tmp_path / "staging")

    assert (refused.code, refused.reason) == (ARCHIVE_MEMBER_REFUSED, "duplicate")


def test_a_directory_over_a_file_is_refused(tmp_path: Path) -> None:
    payload = _archive(_file("notes/a.md"), _dir("notes/a.md"))

    refused = _refusal(payload, tmp_path / "staging")

    assert (refused.code, refused.reason) == (ARCHIVE_MEMBER_REFUSED, "duplicate")


def test_a_member_beneath_a_file_is_refused(tmp_path: Path) -> None:
    payload = _archive(_file("notes/a.md"), _file("notes/a.md/inner.md"))

    refused = _refusal(payload, tmp_path / "staging")

    assert (refused.code, refused.reason) == (ARCHIVE_MEMBER_REFUSED, "not_directory")


def test_a_file_named_as_the_root_is_refused(tmp_path: Path) -> None:
    refused = _refusal(_archive(_file("./")), tmp_path / "staging")

    assert (refused.code, refused.reason) == (ARCHIVE_MEMBER_REFUSED, "duplicate")


def test_the_member_cap_is_enforced(tmp_path: Path) -> None:
    payload = _archive(*(_file(f"n/{index}.md") for index in range(4)))

    refused = _refusal(payload, tmp_path / "staging", max_members=3)

    assert (refused.code, refused.reason, refused.member_index) == (
        ARCHIVE_TOO_LARGE,
        "members",
        4,
    )


def test_the_byte_cap_is_enforced_before_the_member_is_written(tmp_path: Path) -> None:
    payload = _archive(_file("a.md", b"x" * 60), _file("b.md", b"y" * 60))

    refused = _refusal(payload, tmp_path / "staging", max_bytes=100)

    assert (refused.code, refused.reason, refused.member_index) == (
        ARCHIVE_TOO_LARGE,
        "bytes",
        2,
    )


def test_the_volume_reserve_is_enforced(tmp_path: Path) -> None:
    payload = _archive(_file("a.md", b"x" * 10), _file("big.bin", b"z" * 500))

    refused = _refusal(
        payload,
        tmp_path / "staging",
        reserve_bytes=100,
        free_space=lambda _path: 550,
    )

    assert (refused.code, refused.reason, refused.member_index) == (
        STORAGE_ALLOWANCE_EXCEEDED,
        "space",
        2,
    )


def test_a_truncated_archive_fails_and_leaves_nothing(tmp_path: Path) -> None:
    payload = _archive(_file("a.md", b"a" * 10), _file("b.md", b"b" * 5000))
    truncated = payload[: 512 * 4]  # header + data of a.md, header of b.md, part of b.md

    refused = _refusal(truncated, tmp_path / "staging")

    assert refused.code == ARCHIVE_UNREADABLE


def test_a_stream_that_is_not_an_archive_is_unreadable(tmp_path: Path) -> None:
    refused = _refusal(b"age-encryption.org/v1\n" + os.urandom(4096), tmp_path / "staging")

    assert (refused.code, refused.reason) == (ARCHIVE_UNREADABLE, "format")


def test_a_refusal_after_writes_removes_everything_written(tmp_path: Path) -> None:
    written = [_file(f"notes/{index}.md", b"body\n") for index in range(20)]
    payload = _archive(*written, _typed("notes/z", tarfile.SYMTYPE, linkname="/"))
    staging = tmp_path / "staging"

    refused = _refusal(payload, staging)

    assert refused.member_index == 21
    assert list(tmp_path.iterdir()) == []


def test_an_existing_staging_directory_is_refused_and_left_alone(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "keep.md").write_text("mine")

    with pytest.raises(ArchiveRefused) as caught:
        _unpack(_archive(_file("a.md")), staging)

    assert (caught.value.code, caught.value.reason) == (IMPORT_STAGING_UNAVAILABLE, "staging")
    assert (staging / "keep.md").read_text() == "mine"


def _run_standalone(payload: bytes, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Run the module as the runbook does: one file, isolated interpreter."""

    return subprocess.run(
        [sys.executable, "-I", str(MODULE), "unpack", *args],
        input=payload,
        capture_output=True,
        check=False,
        env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C.UTF-8"},
    )


def test_the_module_runs_standalone_and_reports_counts_only(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    payload = _archive(_dir(CANARY), _file(f"{CANARY}/{CANARY}.md", b"secret body"))

    completed = _run_standalone(
        payload, "--staging", str(staging), "--max-bytes", str(GiB), "--reserve-bytes", "0"
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "ok": True,
        "members": 2,
        "files": 1,
        "directories": 1,
        "bytes": len(b"secret body"),
    }
    assert completed.stderr == b""
    assert (staging / CANARY / f"{CANARY}.md").read_bytes() == b"secret body"


def test_a_standalone_refusal_is_content_free(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    payload = _archive(
        _file(f"{CANARY}.md", b"secret body"),
        _typed(f"{CANARY}-link", tarfile.SYMTYPE, linkname=f"/{CANARY}"),
    )

    completed = _run_standalone(payload, "--staging", str(staging), "--max-bytes", str(GiB))

    assert completed.returncode == 1
    assert json.loads(completed.stdout) == {
        "ok": False,
        "error": ARCHIVE_MEMBER_REFUSED,
        "reason": "link",
        "member_index": 2,
    }
    output = completed.stdout + completed.stderr
    assert CANARY.encode() not in output
    assert b"secret body" not in output
    assert not staging.exists()


def test_a_relative_staging_path_is_refused(tmp_path: Path) -> None:
    completed = _run_standalone(_archive(_file("a.md")), "--staging", "rel", "--max-bytes", "1")

    assert completed.returncode == 1
    assert json.loads(completed.stdout)["error"] == IMPORT_STAGING_UNAVAILABLE


@pytest.mark.parametrize(
    "name",
    ["notes/" + "é" * 128 + ".md", "/".join(["d" * 100] * 41)],
)
def test_length_limits_do_not_depend_on_the_filesystem(name: str) -> None:
    """A filesystem with longer limits than ext4 still gets the cell's limits."""

    with pytest.raises(ArchiveRefused) as caught:
        cloud_import._relative_parts(name, 7)

    assert (caught.value.code, caught.value.reason, caught.value.member_index) == (
        ARCHIVE_MEMBER_REFUSED,
        "name",
        7,
    )


def test_names_at_the_limits_are_accepted() -> None:
    component = "é" * 127 + "x"  # 255 bytes
    assert cloud_import._relative_parts(f"./notes/{component}", 1) == ("notes", component)


def test_an_archive_cut_at_a_member_boundary_is_refused(tmp_path: Path) -> None:
    """`tarfile`'s stream mode reads a missing end-of-archive as a clean end.

    A ciphertext missing its last parts decrypts to exactly this: whole
    members, then nothing. Committing it would import a partial vault.
    """

    first = _archive(_file("a.md", b"a" * 10))
    payload = _archive(_file("a.md", b"a" * 10), _file("b.md", b"b" * 10))
    cut = first[: 512 * 2]  # a.md's header and data block, no end-of-archive
    assert payload.startswith(cut)

    refused = _refusal(cut, tmp_path / "staging")

    assert (refused.code, refused.reason) == (ARCHIVE_UNREADABLE, "truncated")


def test_an_empty_stream_is_refused(tmp_path: Path) -> None:
    refused = _refusal(b"", tmp_path / "staging")

    assert refused.code == ARCHIVE_UNREADABLE


def test_the_whole_stream_is_consumed_so_the_decryptor_can_finish(tmp_path: Path) -> None:
    """age verifies its final chunk only at EOF; stopping early kills it with SIGPIPE."""

    payload = _archive(_file("a.md", b"a"))
    stream = io.BytesIO(payload + bytes(64 * 1024))

    unpack_stream(stream, tmp_path / "staging", max_bytes=GiB, reserve_bytes=0)

    assert stream.read() == b""


@pytest.mark.parametrize(
    ("expect", "reason"),
    [({"expect_files": 3}, "count"), ({"expect_bytes": 11}, "count")],
)
def test_declared_counts_must_match(tmp_path: Path, expect: dict, reason: str) -> None:
    payload = _archive(_file("a.md", b"a" * 5), _file("b.md", b"b" * 5))

    refused = _refusal(payload, tmp_path / "staging", **expect)

    assert (refused.code, refused.reason) == (ARCHIVE_UNREADABLE, reason)


def test_matching_declared_counts_are_accepted(tmp_path: Path) -> None:
    payload = _archive(_file("a.md", b"a" * 5), _file("b.md", b"b" * 5))

    result = _unpack(payload, tmp_path / "staging", expect_files=2, expect_bytes=10)

    assert (result.files, result.bytes) == (2, 10)


def test_success_is_flushed_to_disk_before_it_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller deletes the ciphertext once it commits; a crash must not leave empty files."""

    synced: list[bool] = []
    monkeypatch.setattr(cloud_import.os, "sync", lambda: synced.append(True))

    _unpack(_archive(_file("a.md", b"a")), tmp_path / "staging")
    assert synced == [True]

    _refusal(_archive(_typed("l", tarfile.SYMTYPE, linkname="/")), tmp_path / "refused")
    assert synced == [True]
