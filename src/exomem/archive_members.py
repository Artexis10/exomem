"""Zip archives: the shared member checks, and preservation as content-addressed members.

Adoption staging and `archive=members` preservation both read untrusted zips, so
the member-name, symlink and bounded-extraction checks live here once and raise
one neutral `ArchiveError`; each caller maps it onto its own refusal codes.

`archive=members` keeps a zip as its members instead of its own bytes. Under
`Evidence/<scope>/<category>/` it writes one gzip blob per distinct member into
`__exomem_raw_v1__members/<first two hex>/<sha256>.gz`, named by the SHA-256 of
the member's uncompressed bytes, and then one manifest with its companion page.
Every path in the family carries the raw-protection prefix, so each is
owner-only before its first byte exists. Blobs land outside the vault mutation
guard and only when absent; the manifest is the commit point, written last
through the ordinary Evidence write path. A crash before it leaves blobs that no
manifest names, and the next expansion reuses them.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
import unicodedata
import zipfile
import zlib
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, BinaryIO

from .governance import raw_protection

INVALID = "ARCHIVE_INVALID"
TOO_LARGE = "ARCHIVE_TOO_LARGE"

#: Central-directory entries one archive may declare.
MAX_ENTRIES = 65_536
#: Uncompressed bytes one member may declare.
MAX_MEMBER_BYTES = 2 * 1024**3
COPY_CHUNK_BYTES = 1024 * 1024
POOL_DIRNAME = f"{raw_protection.PREFIX}members"
MANIFEST_SUFFIX = ".export.json"
MANIFEST_SCHEMA_VERSION = 1
#: Fixed so the same member always gzips to the same blob; 6 trades little size for speed.
_GZIP_LEVEL = 6
#: Bit 0 of a zip entry's general-purpose flags: the entry is encrypted (APPNOTE 4.4.4).
_ZIP_FLAG_ENCRYPTED = 0x1


class ArchiveError(Exception):
    """A content-free refusal of an archive: `code` is `INVALID` or `TOO_LARGE`."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def valid_component(part: str) -> bool:
    return (
        part not in {".", ".."}  # nosemgrep: ep-word-membership -- path syntax fixes the dot segments.
        and "\\" not in part
        and unicodedata.normalize("NFC", part) == part
        and not any(unicodedata.category(character) == "Cc" for character in part)
    )


def validated_member(name: Any, relative_prefix: str = "") -> str:
    if not isinstance(name, str) or not name:
        raise ArchiveError(INVALID, "an archive entry has no name")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        raise ArchiveError(INVALID, "an archive entry has an absolute path")
    parts = [part for part in normalized.split("/") if part]
    if not parts or any(not valid_component(part) for part in parts):
        raise ArchiveError(INVALID, "an archive entry has an unsafe path")
    if relative_prefix:
        parts = relative_prefix.split("/") + parts
    return "/".join(parts)


def entry_is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def safe_join(base: Path, member: str) -> Path:
    target = base / member
    try:
        target.resolve().relative_to(base.resolve())
    except ValueError as exc:
        raise ArchiveError(INVALID, "an archive entry leaves its destination") from exc
    return target


def extract_member(
    zip_file: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    dest: Path,
    *,
    per_entry_limit: int,
) -> int:
    written = 0
    descriptor = os.open(
        dest,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    with zip_file.open(info) as source, os.fdopen(descriptor, "wb") as out:
        while True:
            chunk = source.read(COPY_CHUNK_BYTES)
            if not chunk:
                break
            written += len(chunk)
            if written > per_entry_limit:
                raise ArchiveError(TOO_LARGE, "an archive entry exceeds its size limit")
            out.write(chunk)
    return written


# --- Preservation as members ------------------------------------------------


@dataclass(frozen=True)
class _Entry:
    path: str
    info: zipfile.ZipInfo


def _central_directory(zip_file: zipfile.ZipFile) -> tuple[list[_Entry], int]:
    """Validate every entry before any member is read; members sorted by path."""
    infos = zip_file.infolist()
    if len(infos) > MAX_ENTRIES:
        raise ArchiveError(TOO_LARGE, f"the archive declares more than {MAX_ENTRIES:,} entries")
    entries: dict[str, _Entry] = {}
    total = 0
    for info in infos:
        if info.is_dir():
            continue
        if info.flag_bits & _ZIP_FLAG_ENCRYPTED:
            raise ArchiveError(INVALID, "the archive has an encrypted entry")
        if entry_is_symlink(info):
            raise ArchiveError(INVALID, "the archive has a symlink entry")
        path = validated_member(info.filename)
        if PureWindowsPath(path).drive:
            raise ArchiveError(INVALID, "an archive entry has a drive path")
        if path in entries:
            raise ArchiveError(INVALID, "the archive names one member twice")
        if info.file_size > MAX_MEMBER_BYTES:
            raise ArchiveError(TOO_LARGE, "an archive member exceeds 2 GiB")
        entries[path] = _Entry(path, info)
        total += info.file_size
    return [entries[path] for path in sorted(entries)], total


def _member_chunks(zip_file: zipfile.ZipFile, entry: _Entry) -> Iterator[bytes]:
    """A member's bytes; `zipfile` stops at the declared size and checks the CRC."""
    read = 0
    try:
        with zip_file.open(entry.info) as source:
            while chunk := source.read(COPY_CHUNK_BYTES):
                read += len(chunk)
                yield chunk
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as exc:
        raise ArchiveError(INVALID, "an archive member cannot be read") from exc
    if read != entry.info.file_size:
        raise ArchiveError(INVALID, "an archive member does not match its declared size")


def _member_digest(zip_file: zipfile.ZipFile, entry: _Entry) -> str:
    digest = hashlib.sha256()
    for chunk in _member_chunks(zip_file, entry):
        digest.update(chunk)
    return digest.hexdigest()


def _write_blob(zip_file: zipfile.ZipFile, entry: _Entry, blob: Path, sha256: str) -> None:
    """Gzip one member into the pool, landing it only when no blob of its name exists."""
    blob.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(prefix=".", suffix=".part", dir=blob.parent)
    staged = Path(raw)
    try:
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "wb") as sink:
            with gzip.GzipFile(filename="", mode="wb", fileobj=sink,
                               compresslevel=_GZIP_LEVEL, mtime=0) as packed:
                for chunk in _member_chunks(zip_file, entry):
                    digest.update(chunk)
                    packed.write(chunk)
            sink.flush()
            os.fsync(sink.fileno())
        if digest.hexdigest() != sha256:
            raise ArchiveError(INVALID, "an archive member changed while it was read")
        # Same name, same uncompressed bytes: a concurrent expansion that lands
        # the blob first is as good as this one.
        if not blob.exists():
            os.replace(staged, blob)
    finally:
        staged.unlink(missing_ok=True)


def _modified(info: zipfile.ZipInfo) -> str | None:
    """The entry's own timestamp as written: a local time with no zone."""
    try:
        return dt.datetime(*info.date_time).isoformat()
    except (TypeError, ValueError):
        return None


def _manifest_name(filename: str, sha256: str) -> str:
    """`<prefix><stem>.<12 hex>.export.json`; the digest keeps re-exports of one name apart."""
    leaf = PurePosixPath(filename.replace("\\", "/")).name
    if leaf.casefold().startswith(raw_protection.PREFIX):
        leaf = leaf[len(raw_protection.PREFIX):]
    stem = leaf[: -len(".zip")] if leaf.casefold().endswith(".zip") else leaf
    return f"{raw_protection.PREFIX}{stem or 'archive'}.{sha256[:12]}{MANIFEST_SUFFIX}"


def _hash_stream(stream: BinaryIO, *, max_bytes: int) -> tuple[str, int]:
    stream.seek(0)
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(COPY_CHUNK_BYTES):
        size += len(chunk)
        if size > max_bytes:
            raise ArchiveError(TOO_LARGE, f"the archive exceeds the {max_bytes:,}-byte limit")
        digest.update(chunk)
    stream.seek(0)
    return digest.hexdigest(), size


def _recorded(vault_root: Path, folder: Path, sha256: str) -> tuple[dict[str, Any], int] | None:
    """The duplicate receipt fields of a family manifest that records this archive.

    A manifest's companion is a dataset card, which carries no governance
    companion block, so the pair is resolved through its raw-protection binding.
    """
    from . import reserved_paths

    try:
        manifests = sorted(folder.glob(f"{raw_protection.PREFIX}*{MANIFEST_SUFFIX}"))
    except OSError:
        return None
    for manifest in manifests:
        relative = manifest.relative_to(vault_root).as_posix()
        found = raw_protection.binding(vault_root, relative)
        if found is None:
            continue
        block, companion, _companion_hash = found
        try:
            data = reserved_paths.read_generic_bytes(vault_root, relative).data
            archive = json.loads(data).get("archive")
        except (OSError, ValueError, AttributeError, reserved_paths.ReservedPathLeafError):
            continue
        if hashlib.sha256(data).hexdigest() != block["artifact_sha256"]:
            continue
        if isinstance(archive, dict) and archive.get("sha256") == sha256:
            ref = block["original_ref"]
            return (
                {
                    "path": relative,
                    "stored_path": relative,
                    "sidecar_path": companion,
                    "ref": ref,
                    "duplicate_of": {"path": relative, "ref": ref},
                    "hash": block["artifact_sha256"],
                },
                len(data),
            )
    return None


def preserve_members(
    vault_root: Path,
    *,
    guard: Callable[[], AbstractContextManager[Any]],
    scope: str,
    category: str,
    filename: str,
    stream: BinaryIO,
    max_bytes: int,
    verified: str,
    description: str | None = None,
    sha256: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Preserve a seekable zip as members; return the receipt and whether it stored.

    `verified` says what checked the archive's SHA-256 (`session` or `upload`).
    `guard` opens the vault mutation guard around the manifest commit only. A
    caller that already verified the archive passes its `sha256` to skip a pass.
    Raises `ArchiveError` or `preserve.PreserveError`; neither leaves a manifest.
    """
    from . import preserve as preserve_module
    from .vault import kb_root

    refusals = [
        refusal
        for refusal in (
            preserve_module.destination_segment_refusal(scope, field="scope"),
            preserve_module.destination_segment_refusal(category, field="category"),
        )
        if refusal
    ]
    if refusals:
        raise preserve_module.PreserveError("INVALID_PRESERVE", ["scope", "category"], "; ".join(refusals))
    folder = kb_root(vault_root) / "Evidence" / scope / category
    preserve_module.validate_raw_capture(
        filename, destination=str(folder.relative_to(vault_root)), raw_protection=True
    )
    if sha256 is None:
        sha256, size = _hash_stream(stream, max_bytes=max_bytes)
    else:
        stream.seek(0, io.SEEK_END)
        size = stream.tell()
        stream.seek(0)
    archive = {
        "filename": PurePosixPath(filename.replace("\\", "/")).name,
        "sha256": sha256,
        "bytes": size,
        "verified": verified,
    }

    def already_stored() -> tuple[dict[str, Any], bool] | None:
        found = _recorded(vault_root, folder, sha256)
        if found is None:
            return None
        fields, manifest_size = found
        return {
            **fields,
            "state": "already_stored",
            "outcome": "stored",
            "warnings": [],
            "size": manifest_size,
            "hash_algorithm": "sha256",
            "media_id": f"sha256:{fields['hash']}",
            "content_type": "application/json",
            "archive": archive,
        }, False

    # Before any member is read: the same archive into the same family is one fact.
    if (receipt := already_stored()) is not None:
        return receipt
    try:
        zip_file = zipfile.ZipFile(stream)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveError(INVALID, "archive=members takes a zip archive") from exc
    with zip_file:
        entries, total = _central_directory(zip_file)
        probe = folder
        while not probe.exists() and probe != vault_root:
            probe = probe.parent
        if total > shutil.disk_usage(probe).free:
            raise ArchiveError(TOO_LARGE, "the archive's members exceed the free disk space")
        pool = folder / POOL_DIRNAME
        members: list[dict[str, Any]] = []
        written = 0
        for entry in entries:
            digest = _member_digest(zip_file, entry)
            blob = pool / digest[:2] / f"{digest}.gz"
            if not blob.exists():
                _write_blob(zip_file, entry, blob, digest)
                written += 1
            members.append(
                {
                    "path": entry.path,
                    "sha256": digest,
                    "bytes": entry.info.file_size,
                    "modified": _modified(entry.info),
                    "blob": blob.relative_to(folder).as_posix(),
                    "stored_bytes": blob.stat().st_size,
                }
            )
    manifest = {"schema_version": MANIFEST_SCHEMA_VERSION, "archive": archive, "members": members}
    data = (json.dumps(manifest, sort_keys=True, ensure_ascii=True, indent=1) + "\n").encode("ascii")
    with guard():
        if (receipt := already_stored()) is not None:
            return receipt
        result = preserve_module.preserve(
            vault_root,
            scope=scope,
            category=category,
            filename=_manifest_name(archive["filename"], sha256),
            content_stream=io.BytesIO(data),
            content_type="application/json",
            description=description
            or f"Archive {archive['filename']} preserved as {len(members):,} members.",
            max_stream_bytes=len(data),
            raw_protection=True,
        )
    receipt = result.as_dict()
    receipt["archive"] = {**archive, "members": len(members), "new_blobs": written}
    return receipt, True
