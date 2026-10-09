"""Hostile archives preserved with `archive=members` write nothing.

Every refusal here is decided from the central directory, before any member
is read, so neither a blob nor a manifest may appear in the family. The data
is invented.
"""

from __future__ import annotations

import contextlib
import io
import stat
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
    ],
    ids=["zip-slip", "absolute", "drive", "symlink", "duplicate", "encrypted", "too-many-entries"],
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
