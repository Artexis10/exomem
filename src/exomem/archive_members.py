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
manifest names, and the next expansion reuses them. An archive a family manifest
already records writes nothing, except that any blob the manifest names and the
pool lost comes back from the uploaded archive.

One expansion at a time runs in a vault, under a file lock, so each one can
first remove the member temps that a killed expansion left in the pool.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import struct
import tempfile
import unicodedata
import zipfile
import zlib
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, BinaryIO

from filelock import FileLock

from .governance import raw_protection

INVALID = "ARCHIVE_INVALID"
TOO_LARGE = "ARCHIVE_TOO_LARGE"
#: The disk lacks room for a blob: nothing about the archive is wrong, so it may succeed later.
NO_SPACE = "ARCHIVE_NO_SPACE"

#: Central-directory entries one archive may declare.
MAX_ENTRIES = 65_536
#: Uncompressed bytes one member may declare.
MAX_MEMBER_BYTES = 2 * 1024**3
#: Bytes the central directory may declare. `zipfile` reads it whole and builds an entry
#: per 46-byte record before any count can be checked; 16 MiB holds 65,536 entries with
#: paths near 200 bytes and caps that build near 365,000 entries.
MAX_DIRECTORY_BYTES = 16 * 1024**2
COPY_CHUNK_BYTES = 1024 * 1024
POOL_DIRNAME = f"{raw_protection.PREFIX}members"
MANIFEST_SUFFIX = ".export.json"
MANIFEST_SCHEMA_VERSION = 1
#: A manifest entry takes about 310 bytes plus its escaped path; 512 apiece leaves each
#: of MAX_ENTRIES members a path of about 200 bytes on average.
MAX_MANIFEST_BYTES = MAX_ENTRIES * 512
#: Fixed so the same member always gzips to the same blob; 6 trades little size for speed.
_GZIP_LEVEL = 6
#: Bit 0 of a zip entry's general-purpose flags: the entry is encrypted (APPNOTE 4.4.4).
_ZIP_FLAG_ENCRYPTED = 0x1
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")  # the format of a member's name in the pool
#: APPNOTE 4.3.16, 4.3.15 and 4.3.14: the end record, the ZIP64 locator and the ZIP64 end record.
_END_RECORD = struct.Struct("<4s4H2LH")
_ZIP64_LOCATOR = struct.Struct("<4sLQL")
_ZIP64_END_RECORD = struct.Struct("<4sQ2H2L4Q")
#: The end record is the file's last 22 bytes, followed by a comment of at most 65,535 bytes.
_END_SEARCH_BYTES = _END_RECORD.size + 0xFFFF


def member_blob(sha256: str) -> str:
    """A member's blob path relative to its Evidence family: the one member-pool layout.

    The name is the SHA-256 of the member's uncompressed bytes, under a directory of
    its first two hex digits. Expansion writes blobs here and imports read them here.
    """
    if not _SHA256_HEX.fullmatch(sha256):
        raise ValueError("a member blob is named by a lowercase hex SHA-256")
    return f"{POOL_DIRNAME}/{sha256[:2]}/{sha256}.gz"


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


def _declared_directories(stream: BinaryIO) -> list[tuple[int, int]]:
    """The entry count and directory size each end record declares: the classic one and any ZIP64 one.

    `zipfile` may take either, so the limits are checked against both.
    """
    stream.seek(0, io.SEEK_END)
    start = max(0, stream.tell() - _END_SEARCH_BYTES)
    stream.seek(start)
    tail = stream.read()
    at = tail.rfind(b"PK\x05\x06")
    if at < 0 or len(tail) - at < _END_RECORD.size:
        return []  # not a zip; `zipfile` refuses it
    # Fields 4 and 5: the total entry count and the directory size.
    declared = [tuple(_END_RECORD.unpack_from(tail, at)[4:6])]
    # The ZIP64 end record sits right before its locator, which sits right before the end record.
    locator_at = start + at - _ZIP64_LOCATOR.size
    record_at = locator_at - _ZIP64_END_RECORD.size
    if record_at >= 0:
        stream.seek(record_at)
        found = stream.read(_ZIP64_END_RECORD.size + _ZIP64_LOCATOR.size)
        if found.startswith(b"PK\x06\x06") and found[_ZIP64_END_RECORD.size:].startswith(b"PK\x06\x07"):
            # Fields 7 and 8: the total entry count and the directory size.
            declared.append(tuple(_ZIP64_END_RECORD.unpack_from(found)[7:9]))
    return declared


def _open_zip(stream: BinaryIO) -> zipfile.ZipFile:
    """Open an uploaded zip once its end records declare a central directory within the limits."""
    for entries, directory in _declared_directories(stream):
        if entries > MAX_ENTRIES:
            raise ArchiveError(TOO_LARGE, f"the archive declares more than {MAX_ENTRIES:,} entries")
        if directory > MAX_DIRECTORY_BYTES:
            raise ArchiveError(TOO_LARGE, "the archive's central directory is too large")
    stream.seek(0)
    try:
        return zipfile.ZipFile(stream)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveError(INVALID, "archive=members takes a zip archive") from exc
    except UnicodeDecodeError as exc:
        raise ArchiveError(INVALID, "an archive entry's name is not valid UTF-8") from exc


def _central_directory(zip_file: zipfile.ZipFile) -> list[_Entry]:
    """Validate every entry before any member is read; members sorted by path."""
    infos = zip_file.infolist()
    if len(infos) > MAX_ENTRIES:
        raise ArchiveError(TOO_LARGE, f"the archive declares more than {MAX_ENTRIES:,} entries")
    entries: dict[str, _Entry] = {}
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
    return [entries[path] for path in sorted(entries)]


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
    # Checked per blob, so a re-export needs room only for what it adds; a blob is about its member's size.
    if entry.info.file_size > shutil.disk_usage(blob.parent).free:
        raise ArchiveError(NO_SPACE, "the archive's new members exceed the free disk space")
    descriptor, raw = tempfile.mkstemp(prefix=".", suffix=_TEMP_SUFFIX, dir=blob.parent)
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
            raise ArchiveError(INVALID, "an archive member does not match its recorded SHA-256")
        # Same name, same uncompressed bytes: a concurrent expansion that lands
        # the blob first is as good as this one.
        if not blob.exists():
            os.replace(staged, blob)
    finally:
        staged.unlink(missing_ok=True)


#: A member's temp in the pool: `.<random><suffix>` beside the blob it becomes.
_TEMP_SUFFIX = ".part"


def _expansion_lock(vault_root: Path) -> FileLock:
    """Held by the one expansion that runs in this vault; the pool sweep relies on it."""
    from . import private_state

    return FileLock(private_state.store(vault_root, "archive-members") / "expansion.lock")


def _sweep_pool(folder: Path) -> None:
    """Remove the member temps a killed expansion left; under the lock, no live one owns any."""
    for leftover in (folder / POOL_DIRNAME).glob(f"*/.*{_TEMP_SUFFIX}"):
        leftover.unlink(missing_ok=True)


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


@dataclass(frozen=True)
class Manifest:
    """An export manifest that proved to be one: raw-bound bytes, parsed and checked."""

    size: int
    archive: dict[str, Any]
    #: Each `{path, sha256, bytes, blob, ...}`, sorted by path, its blob in the member pool.
    members: list[dict[str, Any]]
    #: The raw-protection block of its companion; `artifact_sha256` is the manifest's SHA-256.
    binding: dict[str, Any]
    companion: str


def read_manifest(vault_root: Path, path: str) -> Manifest | None:
    """The export manifest at vault-relative `path`, or None when the file is not one.

    Trust comes before parsing. The name carries the raw-protection prefix and the
    manifest suffix, a companion binds the file under raw protection, and its bytes
    stay within `MAX_MANIFEST_BYTES`, refused unread when larger, and hash to the bound
    SHA-256. Raw protection keeps the manifest and every blob it names owner-only, so
    a caller the owner lets read the manifest under raw protection may read its blobs.
    """
    from .vault import PathGuardError, read_bounded_guarded_bytes

    leaf = path.rpartition("/")[2]
    if not (leaf.startswith(raw_protection.PREFIX) and leaf.endswith(MANIFEST_SUFFIX)):
        return None
    found = raw_protection.binding(vault_root, path)
    if found is None:
        return None
    block, companion, _companion_hash = found
    try:
        data, _guard = read_bounded_guarded_bytes(vault_root, path, limit=MAX_MANIFEST_BYTES)
    except (OSError, PathGuardError):
        return None
    if hashlib.sha256(data).hexdigest() != block["artifact_sha256"]:
        return None
    try:
        document = json.loads(data)
    except (ValueError, RecursionError):
        return None
    if type(document) is not dict:
        return None
    version, archive, members = document.get("schema_version"), document.get("archive"), document.get("members")
    if not (
        type(version) is int and version == MANIFEST_SCHEMA_VERSION
        and type(archive) is dict and type(members) is list and len(members) <= MAX_ENTRIES
    ):
        return None
    previous = None
    for member in members:
        if not (
            type(member) is dict
            and type(member.get("path")) is str
            and member["path"]
            and (previous is None or member["path"] > previous)
            and type(member.get("sha256")) is str
            and _SHA256_HEX.fullmatch(member["sha256"])
            and member.get("blob") == member_blob(member["sha256"])
            and type(member.get("bytes")) is int
            and member["bytes"] >= 0
        ):
            return None
        previous = member["path"]
    return Manifest(len(data), archive, members, block, companion)


def _recorded(
    vault_root: Path, folder: Path, sha256: str
) -> tuple[dict[str, Any], int, list[Any]] | None:
    """The duplicate receipt fields, size and members of a family manifest recording this archive.

    A manifest's companion is a dataset card, which carries no governance
    companion block, so the pair is resolved through its raw-protection binding.
    """
    try:
        manifests = sorted(folder.glob(f"{raw_protection.PREFIX}*{MANIFEST_SUFFIX}"))
    except OSError:
        return None
    for manifest in manifests:
        relative = manifest.relative_to(vault_root).as_posix()
        found = read_manifest(vault_root, relative)
        if found is not None and found.archive.get("sha256") == sha256:
            ref = found.binding["original_ref"]
            return (
                {
                    "path": relative,
                    "stored_path": relative,
                    "sidecar_path": found.companion,
                    "ref": ref,
                    "duplicate_of": {"path": relative, "ref": ref},
                    "hash": found.binding["artifact_sha256"],
                },
                found.size,
                found.members,
            )
    return None


def _free_bytes(vault_root: Path, folder: Path) -> int:
    probe = folder
    while not probe.exists() and probe != vault_root:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def _restore(vault_root: Path, folder: Path, stream: BinaryIO, recorded: list[Any]) -> int:
    """Re-expand only the members a recorded manifest names whose blobs left the pool.

    Each comes back from the uploaded archive, verified against its recorded SHA-256
    as it is gzipped, and lands only where no blob exists; nothing else is written.
    Returns how many blobs came back.
    """
    missing: dict[str, str] = {}
    for member in recorded:  # read_manifest checked each member and its blob path
        if not (folder / member["blob"]).exists():
            missing[member["path"]] = member["sha256"]
    if not missing:
        return 0
    with _open_zip(stream) as zip_file:
        entries = {entry.path: entry for entry in _central_directory(zip_file)}
        if any(path not in entries for path in missing):
            raise ArchiveError(INVALID, "the archive lacks a member its manifest records")
        for path, sha256 in sorted(missing.items()):
            _write_blob(zip_file, entries[path], folder / member_blob(sha256), sha256)
    return len(missing)


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
        fields, manifest_size, recorded = found
        restored = _restore(vault_root, folder, stream, recorded)
        return {
            **fields,
            "state": "already_stored",
            "outcome": "stored",
            "warnings": [],
            "size": manifest_size,
            "hash_algorithm": "sha256",
            "media_id": f"sha256:{fields['hash']}",
            "content_type": "application/json",
            "archive": {**archive, "restored": restored},
        }, False

    with _expansion_lock(vault_root):
        _sweep_pool(folder)
        # Before any member is read: the same archive into the same family is one fact.
        if (receipt := already_stored()) is not None:
            return receipt
        with _open_zip(stream) as zip_file:
            entries = _central_directory(zip_file)
            # A small check before any member is read; each new blob checks its own room.
            if max((entry.info.file_size for entry in entries), default=0) > _free_bytes(vault_root, folder):
                raise ArchiveError(NO_SPACE, "the archive's largest member exceeds the free disk space")
            members: list[dict[str, Any]] = []
            written = 0
            for entry in entries:
                digest = _member_digest(zip_file, entry)
                blob = folder / member_blob(digest)
                if not blob.exists():
                    _write_blob(zip_file, entry, blob, digest)
                    written += 1
                members.append(
                    {
                        "path": entry.path,
                        "sha256": digest,
                        "bytes": entry.info.file_size,
                        "modified": _modified(entry.info),
                        "blob": member_blob(digest),
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
