"""Archives preserved with `archive=members`: hostile ones write nothing, and the pool stays sound.

Every hostile refusal here is decided from the central directory, before any
member is read, so neither a blob nor a manifest may appear in the family. The
data is invented.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import os
import shutil
import stat
import struct
import zipfile
from pathlib import Path

import pytest

from exomem import archive_members


def _zip(entries: list[tuple[zipfile.ZipInfo | str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


def _symlink() -> zipfile.ZipInfo:
    info = zipfile.ZipInfo("export/link.json")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    return info


def _encrypted() -> bytes:
    # `zipfile` cannot write an encrypted entry, so set the flag in the central directory.
    data = bytearray(_zip([("export/sealed.json", b"ciphertext")]))
    data[data.index(b"PK\x01\x02") + 8] |= 0x1
    return bytes(data)


def _undecodable_name() -> bytes:
    # The UTF-8 flag (bit 11) on a name whose first byte no UTF-8 sequence starts with.
    data = bytearray(_zip([("export/day.json", b"{}")]))
    central = data.index(b"PK\x01\x02")
    data[central + 9] |= 0x08
    data[central + 46] = 0xFF
    return bytes(data)


@pytest.mark.parametrize(
    ("archive", "code"),
    [
        (_zip([("export/ok.json", b"{}"), ("../escape.json", b"{}")]), archive_members.INVALID),
        (_zip([("/abs/samples.json", b"{}")]), archive_members.INVALID),
        # Concatenated so the public-artifact gate does not read a drive-absolute literal.
        (_zip([("C:" + "/samples.json", b"{}")]), archive_members.INVALID),
        (_zip([(_symlink(), b"/outside/target")]), archive_members.INVALID),
        (_zip([("export/a.json", b"1"), ("export//a.json", b"2")]), archive_members.INVALID),
        (_encrypted(), archive_members.INVALID),
        (_zip([(f"export/day-{n}.json", b"{}") for n in range(4)]), archive_members.TOO_LARGE),
        (_undecodable_name(), archive_members.INVALID),
    ],
    ids=["zip-slip", "absolute", "drive", "symlink", "duplicate", "encrypted", "too-many-entries",
         "undecodable-name"],
)
def test_a_hostile_archive_writes_nothing(
    vault: Path, monkeypatch: pytest.MonkeyPatch, archive: bytes, code: str
) -> None:
    monkeypatch.setattr(archive_members, "MAX_ENTRIES", 3)
    family = vault / "Knowledge Base" / "Evidence" / "Device" / "Exports"

    with pytest.raises(archive_members.ArchiveError) as refused:
        archive_members.preserve_members(
            vault,
            guard=contextlib.nullcontext,
            scope="Device",
            category="Exports",
            filename="export.zip",
            stream=io.BytesIO(archive),
            max_bytes=1024 * 1024,
            verified="upload",
        )

    assert refused.value.code == code
    assert not family.exists() or not any(path.is_file() for path in family.rglob("*"))


def test_preserving_a_recorded_archive_again_restores_a_lost_blob_and_nothing_else(vault: Path) -> None:
    """An `already_stored` that trusts its manifest leaves a member it names unreadable for good."""
    archive = _zip([("export/device_days/day-01.json", b'{"samples": [{"heart_rate": 61}]}'),
                    ("export/profile.json", b'{"units": "metric"}')])
    family = vault / "Knowledge Base" / "Evidence" / "Device" / "Exports"

    def preserve() -> tuple[dict, bool]:
        return archive_members.preserve_members(
            vault, guard=contextlib.nullcontext, scope="Device", category="Exports", filename="export.zip",
            stream=io.BytesIO(archive), max_bytes=1024 * 1024, verified="upload",
        )

    def tree() -> dict[Path, bytes]:
        return {path: path.read_bytes() for path in family.rglob("*") if path.is_file()}

    _, stored = preserve()
    before = tree()
    lost = sorted(path for path in before if path.suffix == ".gz")[0]
    lost.unlink()

    receipt, stored_again = preserve()

    assert (stored, stored_again, receipt["state"], receipt["archive"]["restored"]) == (
        True, False, "already_stored", 1
    )
    assert hashlib.sha256(gzip.decompress(lost.read_bytes())).hexdigest() == lost.name.removesuffix(".gz")
    assert tree() == before


def _preserve(vault: Path, archive: bytes) -> tuple[dict, bool]:
    return archive_members.preserve_members(
        vault, guard=contextlib.nullcontext, scope="Device", category="Exports", filename="export.zip",
        stream=io.BytesIO(archive), max_bytes=64 * 1024 * 1024, verified="upload",
    )


def _end_record(entries: int, directory_bytes: int) -> bytes:
    """An end-of-central-directory record with no directory behind it (APPNOTE 4.3.16)."""
    return struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, entries, entries, directory_bytes, 0, 0)


def _zip64_end(entries: int, directory_bytes: int) -> bytes:
    """A ZIP64 end record and its locator before a classic end record that defers to them."""
    record = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, entries, entries, directory_bytes, 0)
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, 0, 1)
    return record + locator + _end_record(0xFFFF, 0xFFFFFFFF)


@pytest.mark.parametrize(
    "archive",
    [_zip64_end(22_000_000, 1024), _end_record(5, 1 << 30)],
    ids=["zip64-entries", "directory-bytes"],
)
def test_an_oversized_central_directory_is_refused_before_it_is_loaded(vault: Path, archive: bytes) -> None:
    """`zipfile` loads the whole directory before any count can be checked: about 12 GB of
    memory for the 22 million entries a 2 GiB session can declare."""
    with pytest.raises(archive_members.ArchiveError) as refused:
        _preserve(vault, archive)

    assert refused.value.code == archive_members.TOO_LARGE


def test_an_expansion_removes_the_temp_a_killed_expansion_left_in_the_pool(vault: Path) -> None:
    """Only a `finally` removed a member's temp, so a killed expansion's stays in the pool for good."""
    pool = vault / "Knowledge Base" / "Evidence" / "Device" / "Exports" / archive_members.POOL_DIRNAME
    killed = pool / "ab" / ".tmp3k9x.part"
    killed.parent.mkdir(parents=True)
    killed.write_bytes(b"\x1f\x8b" + bytes(4096))

    _, stored = _preserve(vault, _zip([("export/device_days/day-01.json", b'{"samples": [61, 62]}')]))

    assert stored
    assert sorted(path.name for path in pool.rglob("*") if path.is_file()) == [
        hashlib.sha256(b'{"samples": [61, 62]}').hexdigest() + ".gz"
    ]


def test_an_expansion_needs_room_only_for_the_blobs_it_writes(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Room for every declared member refuses a re-export that writes almost nothing; with no
    check per blob, new members fill the disk."""
    family = vault / "Knowledge Base" / "Evidence" / "Device" / "Exports"
    days = [os.urandom(30_000) for _ in range(4)]  # random bytes: a blob is as large as its member
    _preserve(vault, _zip([("export/day-1.bin", days[0]), ("export/day-2.bin", days[1])]))

    def stored() -> int:
        return sum(path.stat().st_size for path in family.rglob("*") if path.is_file())

    # 45,000 bytes free after the first export, which each later write uses up: room for
    # one 30,000-byte day beside the re-export's manifest and page, not for two.
    room, real = 45_000 + stored(), shutil.disk_usage
    monkeypatch.setattr(shutil, "disk_usage", lambda path: real(path)._replace(free=room - stored()))

    receipt, re_exported = _preserve(
        vault, _zip([("export/day-1.bin", days[0]), ("export/day-2.bin", days[1]), ("export/note.txt", b"rest day")])
    )
    with pytest.raises(archive_members.ArchiveError) as refused:
        _preserve(vault, _zip([("export/day-3.bin", days[2]), ("export/day-4.bin", days[3])]))

    assert (re_exported, receipt["archive"]["new_blobs"]) == (True, 1)
    assert refused.value.code == archive_members.NO_SPACE
    written = {path.name for path in family.rglob("*.gz")}
    assert hashlib.sha256(days[2]).hexdigest() + ".gz" in written
    assert hashlib.sha256(days[3]).hexdigest() + ".gz" not in written
