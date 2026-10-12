"""The encrypted archive-members envelope shared by the CLI and stopped-cell Job."""
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import BinaryIO, Literal

from pydantic import BaseModel, ConfigDict

from . import archive_members, preserve
from .cloud_import import ArchiveRefused

# Schema v1 fixes these transport member names, not tenant vocabulary.
METADATA_NAME = "import.json"
ARCHIVE_NAME = "archive.zip"
METADATA_MAX_BYTES = 8192


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal[1] = 1
    scope: str
    category: str
    filename: str

    def encoded(self) -> bytes:
        for field in ("scope", "category"):
            if preserve.destination_segment_refusal(getattr(self, field), field=field):
                raise ArchiveRefused("ARCHIVE_MEMBER_REFUSED", "destination")
        if (not archive_members.valid_component(self.filename)
                or preserve._sanitize_filename(self.filename) != self.filename or not self.filename
                or len((archive_members._manifest_name(self.filename, "0" * 64) + ".md").encode("utf-8")) > 255):
            # The generated RAW manifest and companion add their fixed suffixes.
            raise ArchiveRefused("ARCHIVE_MEMBER_REFUSED", "filename")
        encoded = self.model_dump_json().encode("utf-8")
        if len(encoded) > METADATA_MAX_BYTES:
            raise ArchiveRefused("ARCHIVE_TOO_LARGE", "envelope")
        return encoded


def _unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate metadata key")
        result[name] = value
    return result


def read(staging: Path) -> Envelope:
    if {p.name for p in staging.iterdir()} != {METADATA_NAME, ARCHIVE_NAME}:
        raise ArchiveRefused("ARCHIVE_MEMBER_REFUSED", "envelope")
    metadata, archive = staging / METADATA_NAME, staging / ARCHIVE_NAME
    if not metadata.is_file() or not archive.is_file() or metadata.stat().st_size > METADATA_MAX_BYTES:
        raise ArchiveRefused("ARCHIVE_MEMBER_REFUSED", "envelope")
    try:
        document = json.loads(metadata.read_bytes().decode("utf-8"), object_pairs_hook=_unique)
        if type(document) is not dict or set(document) != set(Envelope.model_fields) or type(document["schema_version"]) is not int:
            raise ValueError("invalid envelope shape")
        envelope = Envelope.model_validate(document)
        envelope.encoded()
    except (ValueError, UnicodeError):
        raise ArchiveRefused("ARCHIVE_MEMBER_REFUSED", "envelope") from None
    return envelope


def write(stream: BinaryIO, archive: Path, envelope: Envelope) -> None:
    encoded = envelope.encoded()
    with tarfile.open(fileobj=stream, mode="w|") as tar:
        metadata = tarfile.TarInfo(METADATA_NAME)
        metadata.size = len(encoded)
        tar.addfile(metadata, io.BytesIO(encoded))
        entry = tarfile.TarInfo(ARCHIVE_NAME)
        entry.size = archive.stat().st_size
        with archive.open("rb") as source:
            tar.addfile(entry, source)
