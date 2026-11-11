"""An archive that an agent sent through `upload_files` (lane `import`), unpacked into `_Imports/<id>/`.

An agent that cannot run `exomem import <folder>` on the server's machine packs the folder as one tar, gzip
tar or zip, sends it, and gives `imports.file` the upload's token. The first call redeems the held archive
and unpacks it into a stage that `file` then files as it files any staged folder. The agent sends bytes,
never a path, so the rule that no tool argument names a local path holds.

The stage's id derives from the token, so a repeated call finds the stage the first call made and unpacks
nothing again. The stage appears under its final name only when whole: it is unpacked into a hidden folder
beside it and renamed. Every unpack runs under one lock, so a retry that overlaps a slow first call waits for
it. An archive that is refused is released, because the same bytes would be refused again; one that stopped
for a reason that may pass, such as free space, goes back to its hold.

The format comes from the first bytes: a zip starts with a local file header, a gzip stream with its magic,
and anything else is read as a plain tar. bzip2, xz and the rest are refused: Python's decompressors for them
have no output bound, so a few hundred bytes could fill the server's memory. Gzip is inflated here, by a reader
that stops at a limit, and the tar reader stops a record after its end block, so a padded stream costs nothing.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import uuid
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from filelock import FileLock

from . import (
    adoption_run,
    archive_members,
    cloud_import,
    held_uploads,
    import_stage,
    private_state,
    upload_handoff,
    vault,
)

MAX_EXPANDED_ENV = "EXOMEM_IMPORT_MAX_EXPANDED_BYTES"
# The held archive is at most 100 MiB, which a compressed archive can expand a thousandfold; this bounds that
# bomb to what one import may stage. The unpack runs inside one MCP request (50 s): it took about 10 s per GiB on
# a fast local volume, so this leaves room for a volume ten times slower.
DEFAULT_MAX_EXPANDED_BYTES = 512 * 1024**2
#: The unpacker's own refusal tokens, spelled for the agent that sent the archive; the set is fixed by its checks.
_MEMBER_REASONS = {
    "link": "it is a link, and links are never unpacked",
    "special": "it is a device, a sparse file or another special entry",
    "absolute": "its path is absolute",
    "parent": "its path climbs out of the folder with `..`",
    "name": "its name is too long, holds control characters or cannot be stored on this volume",
    "duplicate": "the archive lists it twice, or lists another entry whose name differs only in letter case",
    "not_directory": "a file already sits where this entry needs a folder",
}
#: Magic numbers are fixed by the ZIP and gzip specifications; the file's name is the sender's word and decides nothing.
_ZIP_MAGIC = b"PK\x03\x04"
_GZIP_MAGIC = b"\x1f\x8b"
#: The ZIP methods whose decompressors stop at the size a read asks for; `zipfile`'s bzip2 and lzma ones do not.
_ZIP_METHODS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
#: One entry's pax, long-name and sparse headers may add up to this much; `tarfile` reads them into memory.
_HEADER_LIMIT = 1024**2
_GZIP_WBITS = zlib.MAX_WBITS | 16  # zlib's switch for the gzip wrapper
#: The bytes a member's headers and padding may add to what it holds, for the limit on a gzip stream's output.
_MEMBER_OVERHEAD = 4096
_CHUNK = 1024**2
_STORE = "import-uploads"
_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_SPACE = "INSUFFICIENT_SPACE"


class ImportUploadError(Exception):
    """A stable refusal of an import upload; `retryable` says the held archive may be unpacked again."""

    def __init__(self, code: str, reason: str, *, retryable: bool = False) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class Staged:
    """The stage of an upload: its vault-relative path, and the reply of a call that stopped before it was made."""

    path: str
    stopped: dict | None = None
    #: The stage has no record that vouches for its files, so adoption keeps them after filing them.
    unrecorded: bool = False


def max_expanded_bytes() -> int:
    """The most bytes one import upload may expand to: the operator's setting, else the default."""
    raw = os.environ.get(MAX_EXPANDED_ENV, "").strip()
    return int(raw) if raw.isascii() and raw.isdigit() and int(raw) > 0 else DEFAULT_MAX_EXPANDED_BYTES


def stage_path(upload: object) -> str:
    """`_Imports/<id>` for an upload token, the same path every time; refuses anything that is not a token."""
    nonce = upload_handoff.ticket_nonce(upload)
    if nonce is None:
        raise ImportUploadError("IMPORT_REQUEST_INVALID", "upload takes the token that upload_files returned for lane import")
    digest = hashlib.sha256(f"exomem-import-id:{nonce}".encode("ascii")).digest()
    return f"{vault.IMPORT_STAGE_DIRNAME}/{uuid.UUID(bytes=digest[:16], version=5)}"


def stage(vault_root: Path, upload: str) -> Staged:
    """Unpack the held archive of `upload` into its stage, once; a stage that exists is returned as it is."""
    relative = stage_path(upload)
    import_id = relative.rsplit("/", 1)[1]
    imports = vault_root / vault.IMPORT_STAGE_DIRNAME
    final = imports / import_id
    temp = imports / f".{import_id}.unpack"
    try:
        if imports.is_symlink():
            raise ImportUploadError("IMPORT_STAGING_UNAVAILABLE", f"{vault.IMPORT_STAGE_DIRNAME} is a link, so nothing is unpacked into it")
        imports.mkdir(exist_ok=True)
        lock = FileLock(private_state.store(vault_root, _STORE) / ".lock")
    except OSError:
        raise ImportUploadError("IMPORT_STAGING_UNAVAILABLE", f"{vault.IMPORT_STAGE_DIRNAME} cannot be written", retryable=True) from None
    with lock:
        if os.path.lexists(final):
            if final.is_dir() and not final.is_symlink():
                return Staged(relative, unrecorded=_unrecorded(vault_root, import_id))
            raise ImportUploadError("IMPORT_STAGING_UNAVAILABLE", f"{relative} is not a folder")
        if adoption_run.AdoptionRunStore(vault_root).runs_at(relative):
            # Filing removes a finished stage, and a late repeat of the call must still hear that the import is done.
            return Staged(relative, unrecorded=_unrecorded(vault_root, import_id))
        # Only a call that crashed leaves a hidden folder, and the lock says that none is running now. It goes
        # before the hold is claimed, so a slow removal never runs while the archive is out of its hold.
        for leftover in imports.glob(".*.unpack"):
            shutil.rmtree(leftover, ignore_errors=True)
        held = _redeem(vault_root, upload_handoff.ticket_nonce(upload) or "")
        members: list[dict] = []
        try:
            _unpack(held.path, temp, members)
            os.rename(temp, final)
        except ImportUploadError as refused:
            shutil.rmtree(temp, ignore_errors=True)
            if not refused.retryable:
                held.path.unlink(missing_ok=True)
                raise
            kept = held_uploads.restore(vault_root, held)
            if refused.code != _SPACE:
                raise
            return Staged(relative, _stopped(imports, kept))
        except BaseException:
            shutil.rmtree(temp, ignore_errors=True)
            held_uploads.restore(vault_root, held)
            raise
        held.path.unlink(missing_ok=True)
        # Last: the record lets adoption remove staged files, so a crash before it leaves a copy, never a loss.
        import_stage.write_local_record(vault_root, import_id, members)
    return Staged(relative, unrecorded=_unrecorded(vault_root, import_id))


def _unrecorded(vault_root: Path, import_id: str) -> bool:
    return import_stage.read(vault_root, import_id) is None


def unrecorded_note(path: str) -> dict:
    """What only the person can do when the stage has no record: the files filed from it are still in it."""
    return {"need": "discard", "text": (
        f"The staged copies in {path} are kept, because this vault cannot yet vouch for them. "
        "Never delete them yourself; tell the person that they may.")}


def _redeem(vault_root: Path, nonce: str) -> held_uploads.Redeemed:
    reference = held_uploads.HELD_SCHEME + upload_handoff.hold_secret(nonce)
    try:
        return held_uploads.redeem(vault_root, reference, lane=held_uploads.IMPORT_LANE)
    except held_uploads.HeldUploadError as error:
        if error.code != "HELD_UPLOAD_UNAVAILABLE":
            raise ImportUploadError(error.code, error.reason) from None
        raise ImportUploadError(
            "IMPORT_UPLOAD_UNAVAILABLE",
            "no archive is held for this upload: it was not sent, it was sent by another session, it expired "
            "(a held upload lasts an hour) or an earlier call refused it. Call upload_files with lane import "
            "and send the archive again",
        ) from None


def _unpack(archive: Path, staging: Path, members: list[dict]) -> None:
    """Unpack a tar, gzip tar or zip into the new folder `staging`, listing each file; a refusal leaves nothing behind."""
    cap, reserve = max_expanded_bytes(), cloud_import.reserve_bytes_from_env()

    def listed(path: str, size: int, sha256: str) -> None:
        members.append(import_stage.member(path, size, sha256))

    try:
        with archive.open("rb") as stream:
            head = stream.read(len(_ZIP_MAGIC))
            stream.seek(0)
            if head == _ZIP_MAGIC:
                _unpack_zip(stream, staging, cap, reserve, listed)
            else:
                _unpack_tar(stream, head.startswith(_GZIP_MAGIC), staging, cap, reserve, listed)
    except cloud_import.ArchiveRefused as refused:
        raise _from_tar(refused, cap) from None
    except archive_members.ArchiveError as refused:
        raise _from_zip(refused, cap) from None
    except OSError as error:
        if error.errno in (errno.ENOSPC, errno.EDQUOT):
            raise ImportUploadError(_SPACE, "the volume cannot hold the archive", retryable=True) from None
        raise ImportUploadError("IMPORT_STAGING_UNAVAILABLE", "the archive cannot be unpacked on this volume", retryable=True) from None


class _Inflated:
    """The bytes of a gzip file, inflated up to `limit` in all; past it `overrun` is set and the read fails.

    A tar reader that opens gzip itself has no bound on its output, so a few kilobytes of compressed zeros
    would be inflated for as long as the stream goes on. Each read here inflates at most the size it asks for.
    A gzip file may hold several members one after another (bgzip, `cat a.gz b.gz`); the tar goes on in the next.
    """

    def __init__(self, raw: BinaryIO, limit: int) -> None:
        self._raw = raw
        self._limit = limit
        self._inflater = zlib.decompressobj(wbits=_GZIP_WBITS)
        self._produced = 0
        self.overrun = False

    def read(self, size: int = -1) -> bytes:
        size = size if size > 0 else _CHUNK
        while True:
            if self._inflater.eof:
                data = self._inflater.unused_data or self._raw.read(_CHUNK)
                if not data.startswith(_GZIP_MAGIC):
                    return b""  # the file ends; bytes that start no other member are ignored, as `gunzip` ignores them
                self._inflater = zlib.decompressobj(wbits=_GZIP_WBITS)
            else:
                # An empty `data` still lets the inflater hand out the output it held back at the previous limit.
                data = self._inflater.unconsumed_tail or self._raw.read(_CHUNK)
            out = self._inflater.decompress(data, size)
            self._produced += len(out)
            if self._produced > self._limit:
                self.overrun = True
                raise ValueError("the stream inflates past its limit")
            if out:
                return out
            if not data:
                return b""  # the file ends inside the gzip stream, so the tar reader sees a short archive


def _unpack_tar(
    stream: BinaryIO, gzipped: bool, staging: Path, cap: int, reserve: int, listed: Callable[[str, int, str], None],
) -> None:
    """Unpack a plain or gzip tar, reading to its end block and one record of padding and no further."""
    source: BinaryIO | _Inflated = stream
    inflated: _Inflated | None = None
    if gzipped:
        # What the stream holds is the members, a header and padding for each, and the end block.
        inflated = source = _Inflated(
            stream, cap + archive_members.MAX_ENTRIES * _MEMBER_OVERHEAD + cloud_import.END_WINDOW_BYTES)
    policy = cloud_import.ReadPolicy(
        mode="r|", stop_after_end=True, portable_paths=True, max_header_bytes=_HEADER_LIMIT)
    try:
        cloud_import.unpack_stream(
            source, staging, max_bytes=cap, max_members=archive_members.MAX_ENTRIES,  # type: ignore[arg-type]
            reserve_bytes=reserve, on_file=listed, policy=policy)
    except cloud_import.ArchiveRefused:
        if inflated is not None and inflated.overrun:
            raise cloud_import.ArchiveRefused(cloud_import.ARCHIVE_TOO_LARGE, "inflated") from None
        raise


def _unpack_zip(
    stream: BinaryIO, staging: Path, cap: int, reserve: int, listed: Callable[[str, int, str], None],
) -> None:
    """Write each member of a zip into `staging`, counting the bytes that reach the disk against `cap`."""
    os.mkdir(staging, 0o700)
    current: str | None = None
    try:
        with archive_members.open_zip(stream, max_entries=archive_members.MAX_ENTRIES) as zipped:
            # `zipfile` finds the end record in the last 64 KiB, wherever the zip starts; a zip that starts later
            # is a zip inside something else, and its other bytes would be left unread.
            if min((info.header_offset for info in zipped.infolist()), default=0) != 0:
                raise archive_members.ArchiveError(archive_members.INVALID, "the zip has other data in front of it")
            entries = archive_members.central_directory(
                zipped, max_entries=archive_members.MAX_ENTRIES, portable_paths=True)
            for entry in entries:
                current = entry.path
                if entry.info.compress_type not in _ZIP_METHODS:
                    raise archive_members.ArchiveError(
                        archive_members.INVALID, "it is compressed with a method other than deflate, which is the only one read")
            current = None
            if sum(entry.info.file_size for entry in entries) > cap:
                raise archive_members.ArchiveError(archive_members.TOO_LARGE, "the members declare more bytes than the cap")
            written = 0
            for entry in entries:
                current = entry.path
                try:
                    target = archive_members.safe_join(staging, entry.path)
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    if entry.info.file_size > cloud_import.free_bytes(staging) - reserve:
                        raise archive_members.ArchiveError(archive_members.NO_SPACE, "the volume cannot hold this member")
                    digest = hashlib.sha256()
                    with os.fdopen(os.open(target, _FILE_FLAGS, 0o600), "wb") as sink:
                        for chunk in archive_members.member_chunks(zipped, entry):
                            written += len(chunk)
                            if written > cap:
                                raise archive_members.ArchiveError(archive_members.TOO_LARGE, "the members expand past the cap")
                            digest.update(chunk)
                            sink.write(chunk)
                except OSError as error:
                    if error.errno not in cloud_import.NAME_ERRNOS:
                        raise
                    raise archive_members.ArchiveError(
                        archive_members.INVALID, "its name cannot be stored on this volume") from None
                listed(entry.path, entry.info.file_size, digest.hexdigest())
    except archive_members.ArchiveError as refused:
        refused.member = refused.member or current
        raise
    # The held bytes go once the stage exists, so the stage must be on disk first, as `unpack_stream` leaves a tar's.
    cloud_import.flush_to_disk(staging)


def _named(member: str | None) -> str:
    """A member's name as it may be shown: escaped to ASCII and cut, since an archive's names are its sender's content."""
    return f"member {json.dumps(member[:120])}" if member else "the archive"


def _from_tar(refused: cloud_import.ArchiveRefused, cap: int) -> ImportUploadError:
    if refused.code == cloud_import.STORAGE_ALLOWANCE_EXCEEDED:
        return ImportUploadError(_SPACE, "the volume cannot hold the archive", retryable=True)
    if refused.code == cloud_import.IMPORT_STAGING_UNAVAILABLE:
        return ImportUploadError(refused.code, "the staging folder cannot be written", retryable=True)
    if refused.code == cloud_import.ARCHIVE_TOO_LARGE:
        if refused.reason == "members":
            return ImportUploadError(refused.code, f"the archive holds more than {archive_members.MAX_ENTRIES:,} entries; nothing was unpacked")
        if refused.reason == "header":
            return ImportUploadError(refused.code, f"an entry's headers hold more than {_HEADER_LIMIT:,} bytes in all; nothing was unpacked")
        where = f" at {_named(refused.member)}" if refused.member else ""
        return ImportUploadError(
            refused.code, f"the archive expands past {cap:,} bytes{where} (setting {MAX_EXPANDED_ENV}); nothing was unpacked")
    if refused.code == cloud_import.ARCHIVE_MEMBER_REFUSED:
        why = _MEMBER_REASONS.get(refused.reason, refused.reason)
        return ImportUploadError(refused.code, f"{_named(refused.member)} is refused because {why}; nothing was unpacked")
    return ImportUploadError(
        refused.code, f"the upload is not a complete tar, gzip tar or zip that this server reads ({refused.reason}); nothing was unpacked")


def _from_zip(refused: archive_members.ArchiveError, cap: int) -> ImportUploadError:
    if refused.code == archive_members.NO_SPACE:
        return ImportUploadError(_SPACE, refused.reason, retryable=True)
    code = cloud_import.ARCHIVE_TOO_LARGE if refused.code == archive_members.TOO_LARGE else (
        cloud_import.ARCHIVE_MEMBER_REFUSED if refused.member else cloud_import.ARCHIVE_UNREADABLE)
    limit = f" (limit {cap:,} bytes, setting {MAX_EXPANDED_ENV}; entries {archive_members.MAX_ENTRIES:,})" if code == cloud_import.ARCHIVE_TOO_LARGE else ""
    return ImportUploadError(code, f"{_named(refused.member)}: {refused.reason}{limit}; nothing was unpacked")


def _stopped(imports: Path, kept: bool) -> dict:
    """The reply of a call that stopped for room, worded like the one `file` gives when a chunk does not fit.

    `kept` says whether the archive is back in its hold; when it is not, the person must send it again.
    """
    reserve = cloud_import.reserve_bytes_from_env()
    free = cloud_import.free_bytes(imports)
    return {
        "filing": "stopped",
        "stop": {
            "code": _SPACE,
            "reason": f"The archive needs room to unpack and a reserve of {reserve} bytes; {free} bytes are free.",
        },
        "person": [{"need": "free_space", "text": (
            "Stop. Free space on the vault's volume and tell the person, then repeat this call with the same "
            "upload: it is still held and unpacks from the start." if kept else
            "Stop. Free space on the vault's volume and tell the person. The archive was not kept: call upload_files "
            "with lane import, send it again, then call this with the new token.")}],
        "summary": "Filing is stopped: the archive is not unpacked yet.",
    }
