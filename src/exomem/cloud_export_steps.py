"""The Cloud export's three steps: pause, pack and upload (add-cloud-media-store D8, 2026-11-08).

The pause runs under the export hold with the cell volume at /data and the export's staging claim
mounted beside it. It holds no media key and has no egress. It copies every archive member that is
present on the volume into staging, below the export's directory at the member's archive path, and
writes the plan: the members with their sizes, and each evicted original with its stored reference.

The pack mounts the staging claim alone, at /data. It holds the media key and reaches only the media
edge. It hashes the staged members, fetches each evicted original by its version ID, and writes the
archive into a spool of numbered parts on staging. The upload mounts the staging claim alone, holds no
key, and sends the parts to their presigned URLs. The pack and the upload share only files on staging.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from . import originals, vault_portability
from .cloud_export import (
    EXPORT_FAILED,
    EXPORT_MEDIA_UNREACHABLE,
    EXPORT_TOO_LARGE,
    ExportRefused,
    _put,
    etag_digest,
)

# The step that cellctl names on the command line; a closed protocol set.
PAUSE, PACK, UPLOAD = "pause", "pack", "upload"
# The media modes cellctl sets for a cell with a media store: the edge serves it, or it keeps a disabled
# store identity without an edge credential (manifests.media_store_retained). A closed protocol set.
MEDIA_SERVED, MEDIA_RETAINED = "served", "retained"
# The export's own directory at the staging root, and its parts: the staged members at their archive
# paths, the plan, and the spool of archive parts.
EXPORT_DIR = ".cell-export"
MEMBERS_NAME, PLAN_NAME, SPOOL_NAME = "members", "plan.json", "spool"
END_NAME, FAILED_NAME, UPLOAD_FAILED_NAME = "end.json", "failed.json", "upload-failed.json"
PLAN_VERSION = 1
# The spool holds at most this many parts, the one being written included, so staging needs room for
# two parts beside the members (D8).
SPOOL_PARTS = 2
# How often the pack and the upload look at the spool for the other's progress.
SPOOL_POLL_SECONDS = 0.5
# Waits between attempts to read one stored version again. Their sum, about two minutes, covers a media
# edge that restarts with its Recreate rollout; an edge that stays down longer fails the export.
FETCH_RETRY_SECONDS = (2, 4, 8, 16, 30, 60)
# The archive's own bytes beside its members: the manifest's fixed fields, and per member its manifest
# record and ZIP headers (local, central, ZIP64 and data descriptor), which grow with the path.
ARCHIVE_BASE_BYTES = 64 * 1024
MEMBER_OVERHEAD_BYTES = 512


def planned_archive_bytes(paths_and_sizes) -> int:
    """An upper bound of the archive's size: the members' bytes plus the ZIP and manifest bytes around them."""
    return ARCHIVE_BASE_BYTES + sum(size + MEMBER_OVERHEAD_BYTES + 3 * len(path.encode("utf-8"))
                                    for path, size in paths_and_sizes)


def _write_json(path: Path, value: dict) -> None:
    """Write a record whole or not at all: the other step reads it only after the rename."""
    pending = path.with_name(path.name + ".tmp")
    with open(pending, "w", encoding="utf-8") as stream:
        json.dump(value, stream, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, path)
    _sync_directory(path.parent)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _signature(value: os.stat_result) -> tuple[int, ...]:
    return value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _copy_member(source: Path, target: Path) -> int:
    """Copy one regular file in the kernel; refuse a link, and a file that changes while it is copied.

    copy_file_range is the first choice. Linux refuses it between two filesystems that do not implement
    it themselves (EXDEV since 5.19), which is the case of the cell volume and its staging claim, and a
    Python built without it lacks it; the copy then uses sendfile, also in the kernel.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    reading = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(reading)
        if not stat.S_ISREG(before.st_mode):
            raise vault_portability.PortabilityError("UNSAFE_SOURCE_ENTRY", "vault exports accept regular files only")
        writing = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        try:
            copied, kernel_range = 0, hasattr(os, "copy_file_range")
            while copied < before.st_size:
                count = min(before.st_size - copied, 1 << 30)
                if kernel_range:
                    try:
                        moved = os.copy_file_range(reading, writing, count, copied, copied)
                    except OSError as error:
                        # The errors with which the kernel declines this pair of files, not a failed read.
                        if error.errno not in (errno.EXDEV, errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
                            raise
                        kernel_range = False
                        continue
                else:
                    moved = os.sendfile(writing, reading, copied, count)
                if moved == 0:
                    break
                copied += moved
        finally:
            os.close(writing)
        if _signature(os.fstat(reading)) != _signature(before) or copied != before.st_size:
            raise vault_portability.PortabilityError("SOURCE_CHANGED_DURING_EXPORT", "a source file changed during export")
    finally:
        os.close(reading)
    return copied


@dataclass(frozen=True)
class PauseResult:
    members: int
    member_bytes: int
    evicted: int
    evicted_bytes: int
    planned_bytes: int
    unreadable_companions: int

    def report(self) -> dict:
        return {"phase": PAUSE, "members": self.members, "member_bytes": self.member_bytes, "evicted": self.evicted,
                "evicted_bytes": self.evicted_bytes, "planned_bytes": self.planned_bytes,
                "unreadable_companions": self.unreadable_companions}


def _evicted(vault: Path, media_mode: str | None) -> tuple[list[dict], int]:
    """The evicted originals with their references, and the count of companions that could not be read."""
    binding = originals.binding_for(vault)
    if binding is None:
        if media_mode is not None:
            # Without the store identity an evicted original reads as deleted, and the archive would omit it.
            raise vault_portability.PortabilityError("MEDIA_STORE_UNBOUND", "a media mode is set but no store binds")
        return [], 0
    from . import media_store
    from .writer_lease import get_manager

    unreadable: list[str] = []
    found: list[dict] = []
    with get_manager().consistency_guard(vault, operation="cell_export_pause", holder_kind="background"):
        for path, binary, reference in media_store.evicted_members(
                vault, binding, lambda companion, _error: unreadable.append(companion)):
            found.append({"path": path, "size": binary.size, "sha256": binary.sha256, "reference": reference.fields()})
    if found and media_mode == MEDIA_RETAINED:
        raise ExportRefused(EXPORT_MEDIA_UNREACHABLE)
    return found, len(unreadable)


def pause(vault: Path, staging: Path, *, export_id: str, cell_id: str, media_mode: str | None,
          max_bytes: int) -> PauseResult:
    """Copy the present members into staging and write the plan. The caller holds the export hold."""
    export_dir = staging / EXPORT_DIR
    # Staging is its own claim; a copy onto the cell volume would fill the volume that it relieves.
    if staging.resolve().is_relative_to(vault.parent.resolve()) or os.path.lexists(export_dir):
        raise ExportRefused(EXPORT_FAILED)
    armed_requirement = vault_portability.prepare_quiesced_export(vault)
    evicted, unreadable = _evicted(vault, media_mode)
    listed = list(vault_portability.iter_archive_members(vault))
    stored = [(entry["path"], entry["size"]) for entry in evicted]
    # Refused on the listed sizes before any copy, so an export too large to upload costs the hold nothing.
    if planned_archive_bytes([(member.path, member.source_path.lstat().st_size) for member in listed] + stored) > max_bytes:
        raise ExportRefused(EXPORT_TOO_LARGE)
    export_dir.mkdir()
    # The upload mounts only the spool, by its subPath, so the directory exists before the upload starts.
    (export_dir / SPOOL_NAME).mkdir()
    members: list[list] = []
    member_bytes = 0
    for member in listed:
        size = _copy_member(member.source_path, export_dir / MEMBERS_NAME / member.path)
        members.append([member.path, size, member.classification])
        member_bytes += size
    planned = planned_archive_bytes([(path, size) for path, size, _class in members] + stored)
    if planned > max_bytes:
        raise ExportRefused(EXPORT_TOO_LARGE)
    _write_json(export_dir / PLAN_NAME, {
        "v": PLAN_VERSION, "export_id": export_id, "cell_id": cell_id, "armed_requirement": armed_requirement,
        "members": members, "evicted": evicted, "unreadable_companions": unreadable,
    })
    # The pack reads staging from another pod, possibly after the claim moved to another node.
    os.sync()
    return PauseResult(len(members), member_bytes, len(evicted), sum(entry["size"] for entry in evicted), planned,
                       unreadable)


class _PrefixSink:
    """The archive entry of one stored member, written once across reads that restart from the first byte.

    A read that restarts after a transport failure delivers the bytes already written again. They are
    hashed, not written, and must have the digest of what was written; then writing goes on after them.
    """

    def __init__(self, output: BinaryIO):
        self.output = output
        self.written = 0
        self.digest = hashlib.sha256()
        self._position = 0
        self._prefix = hashlib.sha256()
        self._expected = self.digest.digest()

    def restart(self) -> None:
        self._position, self._prefix, self._expected = 0, hashlib.sha256(), self.digest.copy().digest()

    def write(self, data) -> int:
        view = memoryview(data)
        if self._position < self.written:
            take = min(len(view), self.written - self._position)
            self._prefix.update(view[:take])
            self._position += take
            if self._position == self.written and self._prefix.digest() != self._expected:
                raise vault_portability.PortabilityError("STORED_MEMBER_UNVERIFIED", "a re-read differs from its prefix")
            view = view[take:]
        if view:
            self.output.write(view)
            self.digest.update(view)
            self.written += len(view)
            self._position += len(view)
        return len(data)

    def flush(self) -> None:
        self.output.flush()


def _stored_writer(vault: Path, entry: dict, sleep: Callable[[float], None]) -> Callable[[BinaryIO], tuple[int, str]]:
    """The writer of one evicted original: fetched by version ID, decrypted, and re-read after a transport fault."""
    from . import media_store

    reference = originals.stored_reference(entry["reference"])
    if reference is None:
        raise vault_portability.PortabilityError("STORED_MEMBER_UNVERIFIED", "a planned reference is malformed")

    def absent() -> bool:
        # The edge answers a read of an absent version like an outage; its head tells the two apart.
        try:
            return media_store.store_for(vault).head(reference.object, reference.version) is None
        except media_store.MediaStoreError:
            return False

    def write(output: BinaryIO) -> tuple[int, str]:
        sink = _PrefixSink(output)
        for wait in (*FETCH_RETRY_SECONDS, None):
            sink.restart()
            try:
                head = media_store.write_stored_original(vault, reference, sink)
            except media_store.MediaStoreError as error:
                if error.code != "MEDIA_STORE_UNAVAILABLE" or wait is None:
                    raise
                if absent():
                    raise vault_portability.PortabilityError(
                        "STORED_MEMBER_ABSENT", "a planned version is absent from the media store") from None
                sleep(wait)
                continue
            if head.size < sink.written:
                raise vault_portability.PortabilityError("STORED_MEMBER_UNVERIFIED", "a re-read is shorter than its prefix")
            return sink.written, sink.digest.hexdigest()
        raise AssertionError("unreachable")

    return write


class SpoolSink:
    """The archive as numbered parts on staging, written while at most SPOOL_PARTS parts are there."""

    def __init__(self, spool: Path, part_bytes: int, max_parts: int, *, sleep: Callable[[float], None] = time.sleep):
        self.spool, self.part_bytes, self.max_parts, self.sleep = spool, part_bytes, max_parts, sleep
        self.spool.mkdir(parents=True, exist_ok=True)
        self.offset, self.parts = 0, 0
        self.digest = hashlib.sha256()
        self._part: BinaryIO | None = None
        self._part_size = 0

    def tell(self) -> int:
        return self.offset

    def _waiting_parts(self) -> int:
        if (self.spool / UPLOAD_FAILED_NAME).exists():
            raise ExportRefused(EXPORT_FAILED)
        return sum(1 for path in self.spool.iterdir() if path.name.startswith("part-"))

    def _open_part(self) -> None:
        if self.parts >= self.max_parts:
            raise ExportRefused(EXPORT_TOO_LARGE)
        while self._waiting_parts() >= SPOOL_PARTS:
            self.sleep(SPOOL_POLL_SECONDS)
        self._part = open(self.spool / f"part-{self.parts + 1:05d}.tmp", "wb")
        self._part_size = 0

    def _close_part(self) -> None:
        assert self._part is not None
        self._part.flush()
        os.fsync(self._part.fileno())
        self._part.close()
        self.parts += 1
        os.replace(self.spool / f"part-{self.parts:05d}.tmp", self.spool / f"part-{self.parts:05d}")
        _sync_directory(self.spool)
        self._part = None

    def write(self, data) -> int:
        view = memoryview(data)
        self.digest.update(view)
        self.offset += len(view)
        while view:
            if self._part is None:
                self._open_part()
            count = min(len(view), self.part_bytes - self._part_size)
            self._part.write(view[:count])
            self._part_size += count
            view = view[count:]
            if self._part_size == self.part_bytes:
                self._close_part()
        return len(data)

    def flush(self) -> None:
        if self._part is not None:
            self._part.flush()

    def finish(self) -> dict:
        if self._part is not None and self._part_size:
            self._close_part()
        end = {"bytes": self.offset, "sha256": self.digest.hexdigest(), "parts": self.parts}
        _write_json(self.spool / END_NAME, end)
        return end


def pack(vault: Path, staging: Path, *, export_id: str, cell_id: str, part_bytes: int, max_parts: int,
         max_bytes: int, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Write the archive from the plan into the spool; `vault` only names the cell's media store binding."""
    export_dir = staging / EXPORT_DIR
    spool = export_dir / SPOOL_NAME
    try:
        plan = json.loads((export_dir / PLAN_NAME).read_text(encoding="utf-8"))
        if plan.get("v") != PLAN_VERSION or plan.get("export_id") != export_id or plan.get("cell_id") != cell_id:
            raise ExportRefused(EXPORT_FAILED)
        staged = [vault_portability.StagedMember(path, _staged_source(export_dir / MEMBERS_NAME, path), size,
                                                 classification)
                  for path, size, classification in plan["members"]]
        stored = [vault_portability.StoredMember(entry["path"], entry["size"], entry["sha256"],
                                                 _stored_writer(vault, entry, sleep)) for entry in plan["evicted"]]
        context = vault_portability.PortabilityContext(
            cell_id=cell_id, vault_id=cell_id, operation_id=export_id, created_at=datetime.now(UTC).isoformat(),
            operator_authorized=True, lifecycle_state="quiesced", routing_stopped=True, active_mutations=0,
            background_writers_stopped=True, reads_allowed=True,
        )
        sink = SpoolSink(spool, part_bytes, max_parts, sleep=sleep)
        vault_portability.stream_planned_archive(
            sink, staged, stored, context=context, armed_requirement=plan["armed_requirement"],
            limits=vault_portability.PortabilityLimits(max_total_bytes=max_bytes, max_file_bytes=max_bytes))
        end = sink.finish()
    except BaseException as error:
        spool.mkdir(parents=True, exist_ok=True)
        code = error.code if isinstance(error, ExportRefused) else EXPORT_FAILED
        _write_json(spool / FAILED_NAME, {"error": code})
        raise
    return {"phase": PACK, **end, "evicted": len(stored), "unreadable_companions": plan["unreadable_companions"]}


def _staged_source(members: Path, path: object) -> Path:
    """The staged copy of one plan member, below the members directory with no symbolic link on the way.

    The plan is data on a shared claim. A path that leaves the members directory, or passes a link, would
    put any file that the pack can read, such as its media key, into the archive; it fails the pack.
    """
    if isinstance(path, str):
        source = members / path
        # realpath resolves every link, `.` and `..`, so only a plain relative path maps onto itself.
        if os.path.realpath(source) == os.path.realpath(members) + os.sep + path:
            return source
    raise vault_portability.PortabilityError("UNSAFE_STAGED_MEMBER", "a planned member leaves staging or passes a link")


def upload(staging: Path, part_urls: list[str], *, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Send each spooled part in order to its presigned URL and delete it; report the parts as C6 checks them."""
    spool = staging / EXPORT_DIR / SPOOL_NAME
    etags: list[str] = []
    sent = 0
    try:
        while True:
            failed = spool / FAILED_NAME
            if failed.exists():
                reported = json.loads(failed.read_text(encoding="utf-8")).get("error")
                raise ExportRefused(reported if isinstance(reported, str) and reported in _PACK_CODES else EXPORT_FAILED)
            part = spool / f"part-{len(etags) + 1:05d}"
            if part.exists():
                if len(etags) >= len(part_urls):
                    raise ExportRefused(EXPORT_TOO_LARGE)
                with open(part, "rb") as stream:
                    etags.append(_put(part_urls[len(etags)], stream))
                    sent += os.fstat(stream.fileno()).st_size
                part.unlink()
                continue
            end_path = spool / END_NAME
            if end_path.exists():
                end = json.loads(end_path.read_text(encoding="utf-8"))
                if end.get("parts") == len(etags):
                    if end.get("bytes") != sent:
                        raise ExportRefused(EXPORT_FAILED)
                    return {"phase": UPLOAD, "bytes": sent, "sha256": end["sha256"], "parts": len(etags),
                            "etag_digest": etag_digest(etags)}
            sleep(SPOOL_POLL_SECONDS)
    except BaseException:
        if spool.is_dir():
            _write_json(spool / UPLOAD_FAILED_NAME, {"error": EXPORT_FAILED})
        raise


# The pack's failure codes that the upload passes on; any other failure record reads as EXPORT_FAILED.
_PACK_CODES = frozenset({EXPORT_FAILED, EXPORT_TOO_LARGE})
