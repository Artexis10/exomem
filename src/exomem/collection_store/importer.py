"""Preserved-source streaming import jobs (OpenSpec add-collection-query-engine §12).

``record_memory(action="import", collection=…, import_request={mode, source_ref,
format, mapping, continuation})`` previews, starts, reports and cancels a durable
job that streams one preserved Sources/Evidence file into a store-routed Records
collection. The single-writer service runs jobs: :func:`run_jobs` advances them in
bounded batches on the thread that owns the store writer, never in another process.

A job binds its initiating principal (verified authorization-session identifiers
and expiry, never a bearer), purpose, the exact source receipt (path, SHA-256,
size), the target's store and declaration lineage, and the declared mapping.
Before each batch, and again inside the batch transaction, it re-resolves that
principal's authority from a fresh snapshot; a loss rolls the batch back and
pauses the job as ``partial``/``authority_lost``. Ambient service or owner
authority never imports for a job, another principal never inherits a job, and
expiry is renewed only by the bound principal's explicit continuation. A batch is
one ordinary writer transaction (``bulk_upsert_records``) whose checkpoint,
counters and rejections commit with its rows, so a crash or retry can neither skip
nor repeat effects. A state change that commits no rows (start, pause, resume,
cancel, failure, completion) is one content-free control transition carrying the
job id and counts, so it advances the store head and reaches the replica. A
batch that commits no rows (every row rejected, superseded or unchanged) and
leaves the job running advances only its checkpoint: the replica lags that
progress until the next head change, and a host taking over the store redoes
those batches, which write nothing. Source text is data: it never reaches SQL
text or executes.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import fnmatch
import gzip
import hashlib
import json
import logging
import math
import os
import re
import secrets
import time
import zlib
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Never

from .. import records, vault
from .. import structured_collections as collections
from ..governance import principal as principal_module
from ..governance import raw_protection
from ..governance.authorization_session_lifecycle import AuthorizationSessionContext
from ..query_engine import scalars
from . import (
    connection,
    governance,
    import_document,
    import_recommendations,
    import_time,
    schema,
    takeover,
    tokens,
)

log = logging.getLogger(__name__)

# nosemgrep: ep-word-set -- The import request grammar fixes these source formats.
FORMATS = ("ndjson", "json-array", "csv", "json-document")
MAX_ROW_BYTES = 1 << 20
MAX_DEPTH = 32
MAX_BATCH_ROWS = 500
MAX_BATCH_BYTES = 4 << 20
PREVIEW_ROWS = 100
JOB_WINDOW_SECONDS = 3600
MAX_REQUEST_BYTES = 16 << 10
MAX_MAPPED_FIELDS = 64
MAX_LISTED_MEMBERS = 256
MAX_SAVED_IMPORTS = 16
MAX_MANIFEST_BYTES = 64 << 20
# nosemgrep: ep-word-set -- The scalar kinds a CSV cell coerces to; a subset of SCALAR_TYPES.
CSV_TYPES = ("string", "integer", "number", "boolean")
_CHUNK = 1 << 16
_LISTED = 20
_PREVIEW_PATHS = 64
_PATH_BYTES = 256
_JOB_PREFIX = "import-job:"
_JOB_TOKEN = re.compile(r"import-job:([0-9a-f]{32})")
_BOM = b"\xef\xbb\xbf"
_START = {"row": 0, "byte": 0, "state": {}}
_RESUMABLE = ("authority_lost", "time_cap")
# Store refusals a later tick outlives; any other batch error fails the job.
_STORE_STATE = frozenset(
    {takeover.BUSY, takeover.SYNC_PENDING, takeover.DIVERGED, takeover.CUSTODY_LOST}
)


def contract() -> dict[str, Any]:
    """The ``import`` action's contract for store-mode ``describe``, from these limits."""
    return {
        "why": (
            "stream one preserved Sources or Evidence file, or chosen members of a preserved "
            "export, into this collection as a durable job of bounded batches, each committed "
            "whole or not at all"
        ),
        "import_request": {
            "mode": "preview, start, status or cancel",
            "source_ref": (
                "vault path of a preserved Sources or Evidence file, or of an export manifest; "
                "preview and start"
            ),
            "format": (
                f"{', '.join(FORMATS)}; csv is RFC 4180 UTF-8 with a header row; json-document "
                "is one JSON document whose rows mapping.rows names"
            ),
            "members": (
                f"with an export manifest: a glob over member paths, or 1 to {MAX_LISTED_MEMBERS} "
                "paths. Members import in path order, so a later member's row wins a natural-key "
                "collision; a member already imported into this collection with the same mapping "
                "is skipped"
            ),
            "reimport": "all reads members already imported with this mapping again",
            "mapping": {
                "rows": (
                    "json-document only: the row path, such as a[].b[]; each [] crosses an array "
                    "and the last array's elements are rows"
                ),
                "fields": (
                    f"1 to {MAX_MAPPED_FIELDS} declared target fields, each a dotted source path "
                    "(a csv column name), {from, type?, scale?} or {const}; type converts csv text "
                    f"only: {', '.join(CSV_TYPES)}; scale multiplies a number. In json-document a "
                    "path is relative to the row, $.a.b reads the document and $.a[].b the current "
                    "element of a[], before the rows; $index is the row's place in its array and "
                    "$value the row"
                ),
                "time": (
                    "optional {from: 1 to 4 ordered bases, instant?, offset?, local_date}: the "
                    "first basis present gives the UTC instant, its offset and the source-local "
                    "day; a day is never guessed. A basis is {date} or {instant}, then zone (IANA "
                    "name), offset (path to ±HH:MM) or offset_minutes (path), then seconds (path) "
                    "or index: $index with every {s or ms: number or path}. A zone declares fold: "
                    "order (the earlier offset until the clock steps back within one array), "
                    "earlier or later, and with an increment clock: elapsed or wall. A local time "
                    "in a gap is refused TIME_LOCAL_GAP and counted without stopping the job; an "
                    "instant without an offset, zone or offset path is refused TIME_BASIS_UNZONED"
                ),
                "coverage": (
                    "owner-reviewed source paths mapped to {classification: null or location, subtree?: boolean}; "
                    "null covers one leaf, location with subtree true covers every descendant. "
                    "Uncovered or newly encountered subtrees remain owner-only, including mapped aliases"
                ),
                "on_invalid": "stop (default) fails at the first invalid row; skip records each one",
            },
            "continuation": (
                "the job token start returns; status, cancel, or start again to resume a job "
                "paused by authority_lost or time_cap. An identical start returns the same job "
                "unless it failed or was cancelled"
            ),
        },
        "saved": (
            "preview and start may pass mapping as a name: imports.<name> in the manifest holds "
            "{format, mapping, members?}, checked at every revise; preview's mapping.absent counts "
            "the sampled rows without each mapped path"
        ),
        "zone_rules": {"package": "tzdata", "version": import_time.version()},
        "position": (
            "next_position is {row, byte}; a json-document or export job adds member and "
            "member_row, counts consumed bytes in whole members, and reports members read and "
            "skipped"
        ),
        "identity": (
            "rows sharing a natural key: the last valid occurrence wins whatever the batching; "
            "status counts the superseded ones as duplicates"
        ),
        "preview": (
            f"owner-only; reads up to {PREVIEW_ROWS} rows and writes nothing: fields, nested shape, time "
            "fields, flagged rows, mapping findings, row identity and recommended_declarations"
        ),
        "states": {
            "running": (
                "batches continue in the store writer; authority is current, or unverified "
                "until this host re-proves the source bytes"
            ),
            "partial": (
                "paused: authority_lost or time_cap until continued, cancelled for good, or "
                "store_unavailable until the store accepts writes again. Status checks a "
                "running job afresh and reports the pause its next batch would record"
            ),
            "failed": "invalid_row under on_invalid stop, or batch_error; earlier batches stay",
            "complete": "the whole source was consumed",
        },
        "limits": {
            "row_bytes": MAX_ROW_BYTES,
            "depth": MAX_DEPTH,
            "batch_rows": MAX_BATCH_ROWS,
            "batch_bytes": MAX_BATCH_BYTES,
            "window_seconds": JOB_WINDOW_SECONDS,
        },
    }


def _refuse(
    code: str,
    reason: str,
    *,
    at: str,
    expected: Any = None,
    allowed: Any = None,
    repair: str,
    retryable: bool = False,
) -> Never:
    raise collections.CollectionError(
        code,
        reason,
        {
            "at": at,
            "expected": expected,
            "allowed": allowed,
            "repair": repair,
            "retryable": retryable,
        },
    )


def _source_not_found() -> Never:
    _refuse(
        "IMPORT_SOURCE_NOT_FOUND",
        "source is not a released preserved Sources or Evidence file",
        at="import_request.source_ref",
        expected="vault path of a preserved Sources or Evidence file",
        repair="preserve the export with preserve_evidence or capture_source and pass its returned path",
    )


def _job_not_found() -> Never:
    _refuse(
        "IMPORT_JOB_NOT_FOUND",
        "import job was not found",
        at="import_request.continuation",
        repair="use a continuation from an import you started, with current access to its source "
        "and collection",
    )


def _invalid(at: str, reason: str, *, expected: Any = None, allowed: Any = None) -> Never:
    _refuse(
        "IMPORT_REQUEST_INVALID",
        reason,
        at=at,
        expected=expected,
        allowed=allowed,
        repair="correct import_request and retry",
    )


def _mapping_invalid(at: str, reason: str, *, expected: Any = None, allowed: Any = None) -> Never:
    _refuse(
        "IMPORT_MAPPING_INVALID",
        reason,
        at=at,
        expected=expected,
        allowed=allowed,
        repair="declare each target field from a source path; preview shows the source shape",
    )


class _Lost(Exception):
    """Authority, source release or lineage no longer holds; the batch must not commit."""


# Request envelope


@dataclass(frozen=True, slots=True)
class _Request:
    mode: str
    source_ref: str | None
    format: str | None
    mapping: Any
    job_id: str | None
    members: str | tuple[str, ...] | None = None
    reimport: str | None = None
    saved: str | None = None


#: Modes that write nothing. Lease and egress classify these as reads and every other mode as a mutation.
READ_ONLY_MODES = frozenset({"preview", "status"})
_MODE_FIELDS = {
    "preview": ({"source_ref", "format"}, {"mapping", "members"}),
    "start": ({"source_ref", "format", "mapping"}, {"members", "reimport"}),
    "status": ({"continuation"}, set()),
    "cancel": ({"continuation"}, set()),
}


def _parse_request(raw: Any) -> _Request:
    if not isinstance(raw, Mapping):
        _invalid("import_request", "import_request must be an object", expected="object")
    try:
        encoded = json.dumps(raw, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        _invalid("import_request", "import_request must be JSON")
    if len(encoded.encode()) > MAX_REQUEST_BYTES:
        _invalid(
            "import_request",
            "import_request is too large",
            expected=f"<= {MAX_REQUEST_BYTES} bytes",
        )
    keys = ["continuation", "format", "mapping", "members", "mode", "reimport", "source_ref"]
    unknown = sorted(set(raw) - set(keys))
    if unknown:
        _invalid(f"import_request.{unknown[0]}", "unknown import_request key", allowed=keys)
    mode = raw.get("mode")
    if not isinstance(mode, str) or mode not in _MODE_FIELDS:
        _invalid("import_request.mode", "mode is required", allowed=list(_MODE_FIELDS))
    supplied = {key for key, value in raw.items() if value is not None} - {"mode"}
    required, optional = _MODE_FIELDS[mode]
    if mode == "start" and "continuation" in supplied:
        required, optional = {"continuation"}, set()
    elif type(raw.get("mapping")) is str:
        # A saved import names its own format; the request names only the source.
        required, optional = required - {"format"}, (optional | {"mapping"}) - {"format"}
    for name in sorted(required - supplied):
        _invalid(f"import_request.{name}", f"{mode} requires {name}")
    for name in sorted(supplied - required - optional):
        _invalid(f"import_request.{name}", f"{mode} does not take {name}")
    if "format" in supplied and raw["format"] not in FORMATS:
        _invalid("import_request.format", "unsupported format", allowed=list(FORMATS))
    members = raw.get("members")
    if "members" in supplied:
        members = _selector(members)
    if "reimport" in supplied and (
        raw["reimport"] != "all" or not ("members" in supplied or type(raw.get("mapping")) is str)
    ):
        _invalid(
            "import_request.reimport",
            "reimport all re-reads an export's members already imported with this mapping",
            allowed=["all"],
            expected="members",
        )
    job_id = None
    if "continuation" in supplied:
        matched = (
            _JOB_TOKEN.fullmatch(raw["continuation"]) if type(raw["continuation"]) is str else None
        )
        if matched is None:
            _job_not_found()
        job_id = matched[1]
    return _Request(
        mode, raw.get("source_ref"), raw.get("format"), raw.get("mapping"), job_id, members,
        raw.get("reimport"),
    )


def _selector(raw: Any, at: str = "import_request.members") -> str | tuple[str, ...]:
    """An export's member selector: one glob over member paths, or a list of paths."""
    if type(raw) is str and raw and len(raw.encode()) <= _PATH_BYTES:
        return raw
    if (
        isinstance(raw, list)
        and 1 <= len(raw) <= MAX_LISTED_MEMBERS
        and all(type(path) is str and path for path in raw)
        and len(set(raw)) == len(raw)
    ):
        return tuple(raw)
    _invalid(
        at,
        "members is a glob over the export's member paths or a list of distinct paths",
        expected=f"glob, or 1 to {MAX_LISTED_MEMBERS} paths",
    )


def saved_imports(manifest: collections.CollectionManifest, data: Mapping[str, Any]) -> dict[str, Any]:
    """The manifest's saved imports, each proved to compile against this manifest.

    ``imports.<name>`` holds ``{format, mapping, members?}`` as JSON data. Governed
    create and revise run this, so a saved import that no longer fits the collection
    refuses the revision instead of failing the next start.
    """
    declared = data.get("imports")
    if declared is None:
        return {}
    if not isinstance(declared, Mapping) or len(declared) > MAX_SAVED_IMPORTS:
        _mapping_invalid("imports", f"imports maps up to {MAX_SAVED_IMPORTS} names to saved imports",
                         expected="object")
    found = {}
    for name, spec in declared.items():
        at = f"imports.{name}"
        try:
            encoded = json.dumps(spec, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError):
            _mapping_invalid(at, "a saved import is JSON data", expected="object")
        if (
            type(name) is not str or not name or len(name.encode()) > _PATH_BYTES
            or not isinstance(spec, Mapping) or len(encoded.encode()) > MAX_REQUEST_BYTES
        ):
            _mapping_invalid(at, "a saved import is a bounded {format, mapping, members?}", expected="object")
        for key in sorted(set(spec) - {"format", "mapping", "members"}):
            _mapping_invalid(f"{at}.{key}", "unknown saved import key", allowed=["format", "mapping", "members"])
        if spec.get("format") not in FORMATS:
            _mapping_invalid(f"{at}.format", "unsupported format", allowed=list(FORMATS))
        if "members" in spec:
            _selector(spec["members"], f"{at}.members")
        try:
            compile_mapping(spec.get("mapping"), manifest, spec["format"])
        except collections.CollectionError as error:
            _mapping_invalid(f"{at}.{error.details['at']}", error.reason, expected=error.details.get("expected"),
                             allowed=error.details.get("allowed"))
        found[name] = spec
    return found


def _resolved(writer, row: Mapping[str, Any], manifest, request: _Request) -> _Request:
    """The request with a named saved import's format, mapping and default members."""
    if type(request.mapping) is not str:
        return request
    text = writer.connection.execute(
        "SELECT manifest_text FROM collection_manifests WHERE collection_id=? AND manifest_version=?",
        (row["collection_id"], row["manifest_version"]),
    ).fetchone()[0]
    saved = saved_imports(manifest, vault.parse_frontmatter(text, strict=True)[0])
    if request.mapping not in saved:
        _mapping_invalid("import_request.mapping", "the collection saves no import of this name",
                         allowed=sorted(saved))
    spec = saved[request.mapping]
    members = request.members
    if members is None and "members" in spec:
        members = _selector(spec["members"])
    return dataclasses.replace(request, format=spec["format"], mapping=spec["mapping"], members=members,
                               saved=request.mapping)


# Source resolution


@dataclass(frozen=True, slots=True)
class Source:
    ref: str
    path: Path
    guard: vault.PathGuard
    members: tuple[_Member, ...] = ()


def resolve_source(
    root: Path, operation: governance.OperationAuthorization, reference: Any
) -> tuple[Source, str, int]:
    """The one gate from a source ref to readable bytes, with their SHA-256 and size.

    ``records.resolve_preserved_source`` decides which refs name a preserved
    Sources/Evidence file; ordinary authorization then releases it. The digest is
    streamed under a generation guard, so a governed session never makes the
    reader load a large file whole, and only once a session grant names the file
    or it is released: a withheld file is refused unread, in the time an absent
    one takes. Absent, unpreserved, withheld and malformed refs refuse
    identically; there is no filesystem-path, URL or executable form.
    """
    found: list[tuple[Source, str, int]] = []

    def released(relative: str, path: Path) -> bool:
        guard = vault.PathGuard.capture(
            root,
            relative,
            leaf_policy="generation",
            expected_generation=vault.stat_generation(os.lstat(path)),
        )
        source = Source(relative, path, guard)
        proved: list[tuple[str, int]] = []

        def prove() -> str:
            if not proved:
                proved.append(_digest(root, source))
            return proved[0][0]

        if not operation.allows_file(relative, content_sha256=prove):
            return False
        prove()
        found.append((source, *proved[0]))
        return True

    if records.resolve_preserved_source(root, reference, released) is None:
        _source_not_found()
    return found[0]


def _open_source(source: Source):
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source.path, flags)
    handle = os.fdopen(descriptor, "rb", buffering=0)
    info = os.fstat(descriptor)
    expected = source.guard.leaf_identity
    if expected is None or (info.st_dev, info.st_ino) != (expected.device, expected.inode):
        handle.close()
        raise vault.PathGuardError("PATH_GUARD_CHANGED", "import source identity changed")
    return handle


def _digest(root: Path, source: Source) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with _open_source(source) as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
    source.guard.recheck(root)
    return digest.hexdigest(), size


# Export members
#
# An export manifest (OpenSpec bring-in-large-exports §2) lists each member's path,
# SHA-256 and size and names its gzip blob in the same Evidence family. The manifest
# is the authority: it resolves through ``resolve_source`` like any preserved file, and
# a member is read only from blob bytes that prove to be that member.

_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class _Member:
    index: int
    path: str
    sha256: str
    bytes: int
    blob: str


def _manifest_invalid() -> Never:
    _refuse(
        "IMPORT_MANIFEST_INVALID",
        "source is not an export manifest",
        at="import_request.source_ref",
        expected="a preserved export manifest: schema_version 1, members sorted by path",
        repair="pass the manifest path that preserving the archive returned",
    )


def _blob(sha256: str) -> str:
    # The member pool layout of design §2, mirrored from the archive expansion until its
    # archive_members module joins this batch and this imports the layout from there.
    return f"{raw_protection.PREFIX}members/{sha256[:2]}/{sha256}.gz"


def _export(root: Path, source: Source, size: int, selector: Any) -> tuple[_Member, ...]:
    """The members ``selector`` picks from the export manifest ``source``, in path order."""
    if size > MAX_MANIFEST_BYTES:
        _manifest_invalid()
    chunks, read = [], 0
    with _open_source(source) as handle:
        while read <= MAX_MANIFEST_BYTES and (chunk := handle.read(1 << 20)):
            chunks.append(chunk)
            read += len(chunk)
    source.guard.recheck(root)
    try:
        document = json.loads(b"".join(chunks).decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        _manifest_invalid()
    members = document.get("members") if type(document) is dict else None
    if type(document.get("schema_version") if members is not None else None) is not int or (
        document["schema_version"] != 1 or type(members) is not list
    ):
        _manifest_invalid()
    previous = None
    for member in members:
        if (
            type(member) is not dict
            or type(member.get("path")) is not str
            or not member["path"]
            or type(member.get("sha256")) is not str
            or not _SHA256.fullmatch(member["sha256"])
            or type(member.get("bytes")) is not int
            or member["bytes"] < 0
            or member.get("blob") != _blob(member["sha256"])
            or (previous is not None and member["path"] <= previous)
        ):
            _manifest_invalid()
        previous = member["path"]
    if type(selector) is str:
        chosen = [member for member in members if fnmatch.fnmatchcase(member["path"], selector)]
    else:
        by_path = {member["path"]: member for member in members}
        for position, path in enumerate(selector):
            if path not in by_path:
                _invalid(
                    f"import_request.members[{position}]",
                    "the export has no member at this path",
                    expected="a member path from the manifest",
                )
        chosen = [by_path[path] for path in sorted(selector)]
    if not chosen:
        _invalid(
            "import_request.members",
            "the selector matches no member of the export",
            expected="a glob or list that names member paths",
        )
    family = source.ref.rpartition("/")[0]
    return tuple(
        _Member(index, member["path"], member["sha256"], member["bytes"], f"{family}/{member['blob']}")
        for index, member in enumerate(chosen)
    )


class _Unpacked:
    """A member's uncompressed bytes from its open gzip blob."""

    def __init__(self, raw) -> None:
        self.raw, self.data = raw, gzip.GzipFile(fileobj=raw, mode="rb")

    def read(self, size: int = -1) -> bytes:
        return self.data.read(size)

    def seek(self, offset: int) -> int:
        return self.data.seek(offset)

    def close(self) -> None:
        try:
            self.data.close()
        finally:
            self.raw.close()


def _open_member(root: Path, member: _Member) -> tuple[_Unpacked, vault.PathGuard]:
    """A member's bytes once they prove to be the bound member, else ``_Lost``.

    The blob opens under the identity guard of any import source, and one streamed
    pass hashes its uncompressed bytes before any row is read: a batch commits rows
    before its member ends, so rows must never come from bytes the hash would refuse.
    """
    try:
        path = Path(root) / member.blob
        guard = vault.PathGuard.capture(
            root,
            member.blob,
            leaf_policy="generation",
            expected_generation=vault.stat_generation(os.lstat(path)),
        )
        source = Source(member.blob, path, guard)
        digest, size = hashlib.sha256(), 0
        with _open_source(source) as raw, gzip.GzipFile(fileobj=raw, mode="rb") as data:
            while size <= member.bytes and (chunk := data.read(1 << 20)):
                digest.update(chunk)
                size += len(chunk)
        guard.recheck(root)
        if (digest.hexdigest(), size) != (member.sha256, member.bytes):
            raise _Lost
        return _Unpacked(_open_source(source)), guard
    except (OSError, EOFError, zlib.error, vault.PathGuardError) as error:
        raise _Lost from error


# Streaming readers

_OUTSIDE = re.compile(rb'["\[\]{},]')
_IN_STRING = re.compile(rb'["\\]')
_CSV_TOKEN = re.compile(rb'[",\n]')
_WHITESPACE = b" \t\r\n"


class _JsonScan:
    """Find one JSON value's end and its nesting depth without decoding it."""

    __slots__ = ("deepest", "depth", "escape", "in_string")

    def __init__(self) -> None:
        self.depth = self.deepest = 0
        self.in_string = self.escape = False

    def advance(self, data, i: int, n: int) -> int | None:
        """Scan ``data[i:n]``; return where the value ends, or None if it continues."""
        while i < n:
            if self.in_string:
                if self.escape:
                    self.escape = False
                    i += 1
                    continue
                match = _IN_STRING.search(data, i, n)
                if match is None:
                    return None
                i = match.end()
                if data[match.start()] == 0x5C:
                    self.escape = True
                else:
                    self.in_string = False
                continue
            match = _OUTSIDE.search(data, i, n)
            if match is None:
                return None
            char, i = data[match.start()], match.end()
            if char == 0x22:
                self.in_string = True
            elif char in (0x5B, 0x7B):
                self.depth += 1
                self.deepest = max(self.deepest, self.depth)
            elif char in (0x5D, 0x7D):
                if self.depth == 0:
                    return match.start()
                self.depth -= 1
                if self.depth == 0:
                    return i
            elif self.depth == 0:
                return match.start()
        return None


# Pinned dialect, RFC 4180: comma, double-quote with doubling, CRLF or LF, a
# header row, UTF-8. Parsed here rather than with ``csv``, whose field limit is
# process-global state shared with other readers.
_CSV_FIELD = re.compile(r'"([^"]*(?:""[^"]*)*)"|([^",\r\n]*)')


def _csv_record(text: str) -> list[str] | None:
    """One record's fields, or ``None`` when it is not well-formed RFC 4180."""
    text = text.removesuffix("\n").removesuffix("\r")
    fields, position = [], 0
    while True:
        match = _CSV_FIELD.match(text, position)
        quoted = match[1]
        fields.append(match[2] if quoted is None else quoted.replace('""', '"'))
        position = match.end()
        if position == len(text):
            return fields
        if text[position] != ",":
            return None
        position += 1


@dataclass(slots=True)
class Row:
    ordinal: int
    start: int
    end: int
    value: Any = None
    error: tuple[str, str] | None = None
    fatal: bool = False
    span: int | None = None  # the row's own size when start and end do not measure it
    after: dict[str, Any] | None = None  # a streamed source's cursor once this row is taken


def _reject_constant(name: str) -> Never:
    raise ValueError(f"non-finite JSON constant {name}")


def _decode_object(data: bytes) -> tuple[Any, tuple[str, str] | None]:
    scan = _JsonScan()
    scan.advance(data, 0, len(data))
    if scan.deepest > MAX_DEPTH:
        return None, ("IMPORT_ROW_TOO_DEEP", "")
    try:
        value = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    except UnicodeDecodeError:
        return None, ("IMPORT_ROW_ENCODING", "")
    except (ValueError, RecursionError):
        return None, ("IMPORT_ROW_MALFORMED", "")
    if not isinstance(value, dict):
        return None, ("IMPORT_ROW_NOT_OBJECT", "")
    return value, None


class _LineFrame:
    """An NDJSON record ends at its newline."""

    __slots__ = ("position",)

    def __init__(self) -> None:
        self.position = 0

    def find(self, buffer: bytearray) -> int | None:
        index = buffer.find(b"\n", self.position)
        if index == -1:
            self.position = len(buffer)
            return None
        return index + 1

    def shift(self, count: int) -> None:
        self.position -= count


class _CsvFrame:
    """A pinned-dialect CSV record ends at a newline outside a quoted field.

    A double quote opens a quoted field only at the field's start; anywhere else
    it is a literal that leaves just that record malformed, so one stray quote
    cannot swallow the records after it.
    """

    __slots__ = ("field", "position", "quoted")

    def __init__(self) -> None:
        self.position = self.field = 0
        self.quoted = False

    def find(self, buffer: bytearray) -> int | None:
        size = len(buffer)
        while self.position < size:
            if self.quoted:
                index = buffer.find(b'"', self.position)
                if index == -1:
                    self.position = size
                    return None
                if index + 1 == size:
                    self.position = index  # a doubled quote may follow in the next chunk
                    return None
                if buffer[index + 1] == 0x22:
                    self.position = index + 2
                else:
                    self.quoted, self.position = False, index + 1
                continue
            match = _CSV_TOKEN.search(buffer, self.position)
            if match is None:
                self.position = size
                return None
            index = match.start()
            self.position = index + 1
            if buffer[index] == 0x0A:
                return index + 1
            if buffer[index] == 0x2C:
                self.field = index + 1
            elif index == self.field:
                self.quoted = True
        return None

    def shift(self, count: int) -> None:
        self.position -= count
        self.field -= count


class _Reader:
    """Bounded incremental reader resuming at an exact byte offset and parser state."""

    def __init__(self, handle, fmt: str, checkpoint: Mapping[str, Any]) -> None:
        self.handle, self.fmt = handle, fmt
        self.base = int(checkpoint["byte"])
        self.ordinal = int(checkpoint["row"])
        self.state = dict(checkpoint["state"])
        self.buffer = bytearray()
        self.eof = False
        handle.seek(self.base)

    def position(self) -> dict[str, Any]:
        return {"row": self.ordinal, "byte": self.base, "state": dict(self.state)}

    def after(self, row: Row) -> dict[str, Any]:
        return {"row": row.ordinal + 1, "byte": row.end, "state": dict(self.state)}

    def _read(self) -> bytes:
        chunk = b"" if self.eof else self.handle.read(_CHUNK)
        self.eof = not chunk
        return chunk

    def _consume(self, count: int) -> None:
        del self.buffer[:count]
        self.base += count

    def _bom(self) -> None:
        if self.base == 0:
            while len(self.buffer) < len(_BOM) and (chunk := self._read()):
                self.buffer += chunk
            if self.buffer.startswith(_BOM):
                self._consume(len(_BOM))

    def _peek(self) -> int | None:
        """Skip whitespace and return the next byte without consuming it."""
        while True:
            stripped = len(self.buffer) - len(self.buffer.lstrip(_WHITESPACE))
            if stripped:
                self._consume(stripped)
            if self.buffer:
                return self.buffer[0]
            chunk = self._read()
            if not chunk:
                return None
            self.buffer += chunk

    def at_end(self) -> bool:
        if self.fmt == "json-array" and self.state.get("array") in {"first", "next"}:
            return self._peek() == 0x5D
        return self._peek() is None

    def next(self) -> Row | None:
        self._bom()
        if self.fmt == "ndjson":
            return self._ndjson()
        if self.fmt == "json-array":
            return self._array()
        return self._csv()

    def _row(
        self,
        start: int,
        value: Any = None,
        error: tuple[str, str] | None = None,
        fatal: bool = False,
    ) -> Row:
        row = Row(self.ordinal, start, self.base, value, error, fatal)
        self.ordinal += 1
        return row

    def _frame(self, frame: _LineFrame | _CsvFrame) -> tuple[bytes | None, bool]:
        """Take one record ``frame`` delimits; one over the cap is skipped, never buffered.

        Returns ``(record, too_large)``; ``(None, False)`` is the end of the source.
        """
        skipping = False
        while True:
            end = frame.find(self.buffer)
            if end is not None:
                skipping = skipping or end - 1 > MAX_ROW_BYTES
                record = b"" if skipping else bytes(self.buffer[:end])
                self._consume(end)
                return record, skipping
            if len(self.buffer) > MAX_ROW_BYTES:
                skipping = True
            if skipping:
                scanned = frame.position
                self._consume(scanned)
                frame.shift(scanned)
            chunk = self._read()
            if not chunk:
                if not self.buffer and not skipping:
                    return None, False
                skipping = skipping or len(self.buffer) > MAX_ROW_BYTES
                record = b"" if skipping else bytes(self.buffer)
                self._consume(len(self.buffer))
                return record, skipping
            self.buffer += chunk

    def _ndjson(self) -> Row | None:
        while True:
            start = self.base
            line, too_large = self._frame(_LineFrame())
            if line is None:
                return None
            if too_large:
                return self._row(start, error=("IMPORT_ROW_TOO_LARGE", ""))
            if not line.strip(_WHITESPACE):
                continue
            value, error = _decode_object(line.strip(_WHITESPACE))
            return self._row(start, value, error)

    def _array(self) -> Row | None:
        state = self.state.get("array", "start")
        while True:
            char = self._peek()
            start = self.base
            if state == "start":
                if char != 0x5B:
                    return None if char is None else self._fatal(start)
                self._consume(1)
                state = self.state["array"] = "first"
                continue
            if state == "end":
                return None if char is None else self._fatal(start)
            if char == 0x5D:
                self._consume(1)
                self.state["array"] = "end"
                return None if self._peek() is None else self._fatal(self.base)
            if state == "next":
                if char != 0x2C:
                    return self._fatal(start)
                self._consume(1)
                char = self._peek()
                start = self.base
            if char is None:
                return self._fatal(start)
            return self._element(start)

    def _fatal(self, start: int) -> Row:
        return self._row(start, error=("IMPORT_SOURCE_MALFORMED", ""), fatal=True)

    def _element(self, start: int) -> Row:
        scan, position, skipping = _JsonScan(), 0, False
        while True:
            end = scan.advance(self.buffer, position, len(self.buffer))
            if end is not None:
                break
            position = len(self.buffer)
            if position > MAX_ROW_BYTES:
                skipping = True
            if skipping:
                self._consume(position)
                position = 0
            chunk = self._read()
            if not chunk:
                return self._fatal(start)
            self.buffer += chunk
        skipping = skipping or end > MAX_ROW_BYTES
        data = None if skipping else bytes(self.buffer[:end])
        self._consume(end)
        self.state["array"] = "next"
        if skipping:
            return self._row(start, error=("IMPORT_ROW_TOO_LARGE", ""))
        value, error = _decode_object(data)
        return self._row(start, value, error)

    def _csv(self) -> Row | None:
        while True:
            start = self.base
            record, too_large = self._frame(_CsvFrame())
            if record is None:
                return None
            if too_large:
                return self._row(
                    start, error=("IMPORT_ROW_TOO_LARGE", ""), fatal="header" not in self.state
                )
            try:
                text = record.decode("utf-8")
            except UnicodeDecodeError:
                return self._row(
                    start, error=("IMPORT_ROW_ENCODING", ""), fatal="header" not in self.state
                )
            if not text.strip():
                continue
            fields = _csv_record(text)
            if fields is None:
                return self._row(
                    start, error=("IMPORT_ROW_MALFORMED", ""), fatal="header" not in self.state
                )
            if "header" not in self.state:
                header = fields
                if not header or len(set(header)) != len(header) or not all(header):
                    return self._fatal(start)
                self.state["header"] = header
                continue
            header = self.state["header"]
            if len(fields) != len(header):
                return self._row(start, error=("IMPORT_ROW_MALFORMED", ""))
            return self._row(
                start, {name: cell for name, cell in zip(header, fields, strict=True) if cell != ""}
            )


# A bounded member reader's return when it leaves a member it read (``_Streams``).
_MEMBER_END = object()


@dataclass(frozen=True, slots=True)
class _Stream:
    sha256: str | None  # an export member's; None for one proved json-document file
    bytes: int
    open: Callable[[], tuple[Any, vault.PathGuard | None]]


class _Streams:
    """Rows of whole streams read in order: an export's members, or one json-document file.

    Its cursor is ``{row, byte, member, member_row}``: the next row's ordinal across the
    source, the bytes of the streams before the current one, that stream's index and
    the rows taken from it. A resumed reader reopens its stream and skips those rows.
    Leaving a stream, read whole or skipped as already imported, is an event placed at
    the next stream's start, so a batch records it once its checkpoint is past it.
    A ``bounded`` reader, a job's, returns ``_MEMBER_END`` when it leaves a stream it
    read, so a batch never holds rows of two members and the batch that ends a member
    commits its import log row.
    """

    def __init__(self, streams, fmt, rows, checkpoint, skip=None, *, bounded=False) -> None:
        self.streams, self.fmt, self.rows, self.skip = streams, fmt, rows, skip
        self.bounded = bounded
        self.ordinal, self.base = int(checkpoint["row"]), int(checkpoint["byte"])
        self.member, self.member_row = int(checkpoint["member"]), int(checkpoint["member_row"])
        self.handle = self.inner = self.guard = None
        self.pending: list[Row] = []
        self.events: list[tuple[tuple[int, int], str, str | None, int]] = []
        self.opened: list[vault.PathGuard] = []
        self.marked: dict[str, Any] | None = None

    def position(self) -> dict[str, Any]:
        return {
            "row": self.ordinal,
            "byte": self.base,
            "member": self.member,
            "member_row": self.member_row,
        }

    def after(self, row: Row) -> dict[str, Any]:
        return row.after

    def next(self) -> Row | None:
        while self.member < len(self.streams):
            stream = self.streams[self.member]
            if self.inner is None:
                if self.member_row == 0 and self.skip is not None and self.skip(stream):
                    self._leave("skipped", stream)
                    continue
                self._enter(stream)
            try:
                item = next(self.inner, None)
            except import_document.Malformed:
                item = (None, ("IMPORT_SOURCE_MALFORMED", ""), True, 0)
            except (OSError, EOFError, zlib.error) as error:
                raise _Lost from error
            if item is None:
                self._leave("read", stream)
                if self.bounded and self.member < len(self.streams):
                    return _MEMBER_END
                continue
            value, error, fatal, span = item
            row = Row(self.ordinal, self.base, self.base, value, error, fatal, span)
            self.ordinal += 1
            self.member_row += 1
            row.after = self.position()
            return row
        return None

    def _items(self, handle) -> Iterator[tuple[Any, tuple[str, str] | None, bool, int]]:
        member = self.member
        if self.fmt == "json-document":
            for found in import_document.Rows(
                handle, self.rows.routes, self.rows.wanted, row_bytes=MAX_ROW_BYTES, depth=MAX_DEPTH
            ):
                context = (
                    None if found.error
                    else _Context(found.value, found.index, found.scopes, [member, found.array])
                )
                yield context, found.error, False, found.size
            return
        reader = _Reader(handle, self.fmt, _START)
        while (row := reader.next()) is not None:
            context = None if row.error else _Context(row.value, row.ordinal, (), [member])
            yield context, row.error, row.fatal, row.end - row.start

    def _enter(self, stream: _Stream) -> None:
        self.handle, self.guard = stream.open()
        if self.guard is not None:
            self.opened.append(self.guard)
        self.inner = self._items(self.handle)
        try:
            for _ in range(self.member_row):
                if next(self.inner, None) is None:
                    raise _Lost  # verified bytes hold fewer rows than the checkpoint took
        except (import_document.Malformed, OSError, EOFError, zlib.error) as error:
            raise _Lost from error

    def _leave(self, kind: str, stream: _Stream) -> None:
        self._close()
        self.events.append(((self.member + 1, 0), kind, self.member, stream.sha256))
        self.base += stream.bytes
        self.member += 1
        self.member_row = 0

    def taken(self, checkpoint: Mapping[str, Any]) -> list[tuple[str, int, str | None]]:
        """The streams left up to ``checkpoint``: ``(read or skipped, index, sha256)``."""
        at, done = (checkpoint["member"], checkpoint["member_row"]), []
        while self.events and self.events[0][0] <= at:
            done.append(self.events.pop(0)[1:])
        return done

    def guards(self) -> list[vault.PathGuard]:
        """The blob guards this batch read under, for its commit to re-prove."""
        opened, self.opened = self.opened, []
        return [*opened, *([self.guard] if self.guard is not None and self.guard not in opened else [])]

    def _close(self) -> None:
        handle, self.handle, self.inner, self.guard = self.handle, None, None, None
        if handle is not None:
            handle.close()

    close = _close


def _cursor(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
    return {key: checkpoint[key] for key in ("row", "byte", "member", "member_row")}


def _streamed(binding: Mapping[str, Any]) -> bool:
    """Whether a job reads whole streams: an export's members, or one JSON document."""
    return "members" in binding["source"] or binding["mapping"]["format"] == "json-document"


# Mapping


_ABSENT = object()
_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# nosemgrep: ep-word-set -- The time-basis grammar fixes these keys.
_BASIS_KEYS = (
    "date", "instant", "zone", "offset", "offset_minutes", "seconds", "index", "every", "clock", "fold"
)
# nosemgrep: ep-word-set -- The time-basis keys whose value is a source path.
_BASIS_PATHS = ("date", "instant", "offset", "offset_minutes", "seconds", "index")
# nosemgrep: ep-word-set -- The grammar's interval units, keyed to their timedelta argument.
_UNITS = {"s": "seconds", "ms": "milliseconds"}
# nosemgrep: ep-word-set -- The grammar's clock and fold rules.
_CLOCKS, _FOLDS = ("elapsed", "wall"), ("order", "earlier", "later")
# json-document's reserved path forms: the row's index, the row itself, an enclosing scope.
_INDEX, _VALUE, _SCOPE = "$index", "$value", "$."


@dataclass(slots=True)
class _Context:
    """A json-document row with what its fields may read beyond it."""

    row: Any
    index: int
    scopes: tuple[dict, ...]
    array: Any


@dataclass(frozen=True, slots=True)
class _Path:
    """Where one mapped value comes from: the row, an enclosing scope, the row's place or a literal."""

    keys: tuple[str, ...] = ()
    level: int | None = None  # an enclosing scope: 0 the document, n the nth crossed array's element
    position: bool = False  # $index
    whole: bool = False  # $value
    literal: Any = _ABSENT  # {"const": value}

    def read(self, value: Any) -> Any:
        if self.literal is not _ABSENT:
            return self.literal
        if type(value) is _Context:
            if self.position:
                return value.index
            if self.whole:
                return value.row
            if self.level is not None:
                return value.scopes[self.level].get(self.keys, _ABSENT)
            value = value.row
        return _lookup(value, self.keys)


@dataclass(frozen=True, slots=True)
class _Field:
    target: str
    path: tuple[str, ...]
    kind: str | None
    source: _Path
    scale: int | float | None = None


@dataclass(frozen=True, slots=True)
class _Rows:
    """A json-document row path: ``routes[n]`` is the key path from scope n to the next array."""

    text: str
    routes: tuple[tuple[str, ...], ...]
    prefixes: tuple[str, ...]
    wanted: tuple[dict[tuple[str, ...], str], ...]


@dataclass(frozen=True, slots=True)
class _Basis:
    index: int
    date: _Path | None = None
    instant: _Path | None = None
    zone: Any = None  # a ZoneInfo from the pinned rules
    offset: _Path | None = None
    offset_minutes: _Path | None = None
    seconds: _Path | None = None
    position: _Path | None = None
    every: Any = None  # a positive number or a _Path to one
    unit: str | None = None
    clock: str | None = None
    fold: str | None = None
    label: str = ""

    def resolve(self, value: Any, order: import_time.Fold | None) -> Any:
        """``(utc, minutes, day)`` or ``(code, at)`` for this basis, or None when it is absent.

        A date alone gives only its day. Otherwise the base names a local wall clock
        (a date's midnight, or an instant without an offset) or an instant; the zone or
        offset places a wall clock, and the increment moves it by elapsed or wall time.
        """
        at = f"mapping.time.from[{self.index}]"
        base = "date" if self.date is not None else "instant"
        raw = (self.date or self.instant).read(value)
        if raw is _ABSENT or raw is None:
            return None
        place = "offset" if self.offset is not None else "offset_minutes"
        placed = self.offset is not None or self.offset_minutes is not None
        given = (self.offset or self.offset_minutes).read(value) if placed else None
        step = self._step(value, at)
        if given is _ABSENT or step is None:
            return None
        fixed = None
        if self.date is not None:
            try:
                if type(raw) is not str or not _DATE.fullmatch(raw):
                    raise ValueError
                day = dt.date.fromisoformat(raw)
            except ValueError:
                return "TIME_BASIS_INVALID", f"{at}.date"
            if self.zone is None and not placed:
                return None, None, raw
            utc, wall = None, dt.datetime.combine(day, dt.time())
        else:
            utc, wall = _instant(raw), None
            if utc is None:
                if self.zone is None and not placed:
                    return ("TIME_BASIS_UNZONED" if _unzoned(raw) else "TIME_BASIS_INVALID"), f"{at}.instant"
                wall = _wall(raw)
                if wall is None:
                    return "TIME_BASIS_INVALID", f"{at}.instant"
        if placed:
            fixed = _offset_minutes(given) if place == "offset" else _minutes(given)
            if fixed is None:
                return "TIME_BASIS_INVALID", f"{at}.{place}"
        if type(step) is tuple:
            return step
        try:
            if utc is not None and step and self.zone is not None and self.clock == "wall":
                utc, wall = None, utc.astimezone(self.zone).replace(tzinfo=None)
            if utc is not None:
                utc += step
                minutes = (
                    fixed if fixed is not None
                    else _zone_minutes(utc, self.zone) if self.zone is not None
                    else scalars.instant_offset_minutes(raw)
                )
            elif fixed is not None:
                utc = (wall + step - dt.timedelta(minutes=fixed)).replace(tzinfo=dt.UTC)
                minutes = fixed
            else:
                elapsed = bool(step) and self.clock == "elapsed"
                array = value.array if type(value) is _Context else None
                local = import_time.resolve(wall if elapsed else wall + step, self.zone, self.fold, order, array)
                if local is None:
                    return "TIME_LOCAL_GAP", f"{at}.{base}"
                utc = local.astimezone(dt.UTC) + (step if elapsed else dt.timedelta())
                minutes = _zone_minutes(utc, self.zone)
            if minutes is None:
                return "TIME_BASIS_INVALID", f"{at}.zone"
            day = (utc + dt.timedelta(minutes=minutes)).date().isoformat()
        except (OverflowError, ValueError):
            return "TIME_BASIS_INVALID", f"{at}.{base}"
        return utc, minutes, day

    def _step(self, value: Any, at: str) -> Any:
        """The declared increment as a timedelta, an error, or None when its source is absent."""
        if self.seconds is not None:
            raw = self.seconds.read(value)
            if raw is _ABSENT:
                return None
            if not _finite(raw):
                return "TIME_BASIS_INVALID", f"{at}.seconds"
            try:
                return dt.timedelta(seconds=raw)
            except OverflowError:
                return "TIME_BASIS_INVALID", f"{at}.seconds"
        if self.position is None:
            return dt.timedelta()
        index = self.position.read(value)
        every = self.every.read(value) if type(self.every) is _Path else self.every
        if index is _ABSENT or every is _ABSENT:
            return None
        if not _finite(every) or every <= 0:
            return "TIME_BASIS_INVALID", f"{at}.every"
        try:
            return dt.timedelta(**{self.unit: index * every})
        except OverflowError:
            return "TIME_BASIS_INVALID", f"{at}.index"


@dataclass(frozen=True, slots=True)
class Plan:
    """A compiled, closed mapping: source paths to declared target fields."""

    fields: tuple[_Field, ...]
    bases: tuple[_Basis, ...]
    instant: str | None
    offset: str | None
    local_date: str | None
    on_invalid: str
    natural_key: tuple[str, ...]
    canonical: dict[str, Any] = field(compare=False)
    rows: _Rows | None = None

    @property
    def zoned(self) -> bool:
        return any(basis.zone is not None for basis in self.bases)

    @property
    def ordered(self) -> bool:
        """Whether a fold resolves by order, so the job carries its progress."""
        return any(basis.fold == "order" for basis in self.bases)

    def values(self, value: Any) -> tuple[dict[str, Any] | None, tuple[str, str] | None]:
        out: dict[str, Any] = {}
        for spec in self.fields:
            found = spec.source.read(value)
            if found is _ABSENT:
                continue
            if spec.kind is not None:
                found = _coerce(found, spec.kind)
            if spec.scale is not None and found is not None and found is not _ABSENT:
                found = _scaled(found, spec.scale)
            if found is _ABSENT:
                return None, ("IMPORT_VALUE_INVALID", f"mapping.fields.{spec.target}")
            out[spec.target] = found
        return out, None

    def apply(
        self, value: Any, order: import_time.Fold | None = None
    ) -> tuple[dict[str, Any] | None, tuple[str, str] | None]:
        out, error = self.values(value)
        if error is None and self.bases:
            times, error, _ = self.time(value, order)
            out.update(times)
        return (None, error) if error is not None else (out, None)

    def time(
        self, value: Any, order: import_time.Fold | None = None
    ) -> tuple[dict[str, Any], tuple[str, str] | None, _Basis | None]:
        """Resolve the declared time basis; a missing one is flagged, never guessed."""
        for basis in self.bases:
            found = basis.resolve(value, order)
            if found is None:
                continue
            if len(found) == 2:
                return {}, found, basis
            utc, minutes, day = found
            out = {self.local_date: day}
            if utc is not None and self.instant is not None:
                out[self.instant] = (
                    utc.strftime("%Y-%m-%dT%H:%M:%S")
                    + (f".{utc.microsecond:06d}" if utc.microsecond else "")
                    + "Z"
                )
            if minutes is not None and self.offset is not None:
                sign = "-" if minutes < 0 else "+"
                out[self.offset] = f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"
            return out, None, basis
        return {}, ("TIME_BASIS_ABSENT", "mapping.time.from"), None


def basis_paths(basis: Mapping[str, Any]) -> list[Any]:
    """The source paths a declared time basis reads, an interval's included; field admission
    classifies the time fields by them."""
    every = basis.get("every")
    intervals = every.values() if isinstance(every, Mapping) else ()
    return [basis[key] for key in _BASIS_PATHS if key in basis] + [
        value for value in intervals if type(value) is str
    ]


def _lookup(value: Any, path: tuple[str, ...]) -> Any:
    for part in path:
        if not isinstance(value, Mapping) or part not in value:
            return _ABSENT
        value = value[part]
    return value


def _coerce(text: Any, kind: str) -> Any:
    if type(text) is not str:
        return _ABSENT
    if kind == "string":
        return text
    if kind == "boolean":
        return {"true": True, "false": False}.get(text, _ABSENT)
    if kind == "integer":
        return int(text) if _INTEGER.fullmatch(text) else _ABSENT
    if not _NUMBER.fullmatch(text):
        return _ABSENT
    if _INTEGER.fullmatch(text):
        return int(text)
    number = float(text)
    return number if number == number and abs(number) != float("inf") else _ABSENT


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _scaled(value: Any, scale: int | float) -> Any:
    """``value`` times ``scale``, in decimal so a declared 0.001 does not add binary noise."""
    if not _finite(value):
        return _ABSENT
    if type(value) is int and type(scale) is int:
        return value * scale
    product = float(decimal.Decimal(repr(value)) * decimal.Decimal(repr(scale)))
    return product if math.isfinite(product) else _ABSENT


def _unzoned(raw: Any) -> bool:
    try:
        return type(raw) is str and dt.datetime.fromisoformat(raw).tzinfo is None
    except ValueError:
        return False


def _instant(raw: Any) -> dt.datetime | None:
    try:
        return scalars.parse_instant(raw)
    except scalars.ScalarValueError:
        return None


def _wall(raw: Any) -> dt.datetime | None:
    """A local wall-clock time in the instant grammar, without its offset."""
    if type(raw) is not str:
        return None
    found = _instant(raw + "Z")
    return None if found is None else found.replace(tzinfo=None)


def _offset_minutes(raw: Any) -> int | None:
    try:
        return scalars.offset_minutes(raw)
    except scalars.ScalarValueError:
        return None


def _minutes(raw: Any) -> int | None:
    return raw if type(raw) is int and -24 * 60 < raw < 24 * 60 else None


def _zone_minutes(utc: dt.datetime, where) -> int | None:
    seconds = int(utc.astimezone(where).utcoffset().total_seconds())
    return None if seconds % 60 else seconds // 60


def _path(raw: Any, at: str, fmt: str) -> tuple[str, ...]:
    if type(raw) is not str or not raw or len(raw.encode()) > _PATH_BYTES:
        _mapping_invalid(at, "a source path is a non-empty string", expected="string")
    parts = (raw,) if fmt == "csv" else tuple(raw.split("."))
    if not all(parts):
        _mapping_invalid(at, "a dotted source path has an empty segment", expected="a.b.c")
    return parts


def _source(raw: Any, at: str, fmt: str, rows: _Rows | None) -> _Path:
    """Compile one source path; json-document adds row-relative, scope, index and value forms."""
    parts = _path(raw, at, fmt)
    if rows is None:
        return _Path(parts)
    if raw == _INDEX:
        return _Path(position=True)
    if raw == _VALUE:
        return _Path(whole=True)
    if not raw.startswith(_SCOPE):
        if any("[" in part or "]" in part for part in parts):
            _mapping_invalid(
                at, "a field path is relative to the row; $. reads the document or an enclosing element",
                expected="a.b, $.a.b or $.a[].b",
            )
        return _Path(parts)
    rest = text = raw[len(_SCOPE):]
    level = 0
    for depth, prefix in enumerate(rows.prefixes, 1):
        if rest.startswith(prefix + "."):
            level, text = depth, rest[len(prefix) + 1:]
    keys = tuple(text.split("."))
    route = rows.routes[level] if level < len(rows.routes) else None
    if not all(keys) or any("[" in key or "]" in key for key in keys) or not route:
        _mapping_invalid(
            at, "$. names a field of the document or of an element the row path crosses",
            allowed=[_SCOPE, *(f"{_SCOPE}{prefix}." for prefix in rows.prefixes[:-1])],
        )
    if keys[: len(route)] == route or route[: len(keys)] == keys:
        _mapping_invalid(at, "an enclosing field cannot hold or enter the row path", expected="a sibling of the row path")
    rows.wanted[level].setdefault(keys, at)
    return _Path(keys, level)


def _row_path(raw: Any) -> _Rows:
    """``a[].b[]``: dotted names, each ``[]`` crossing an array; rows are the last array's elements."""
    if type(raw) is not str or not raw or len(raw.encode()) > _PATH_BYTES:
        _mapping_invalid("mapping.rows", "rows is the path to the rows' array", expected="a[].b[]")
    routes, keys = [], []
    for number, part in enumerate(raw.split(".")):
        name, arrays = part, 0
        while name.endswith("[]"):
            name, arrays = name[:-2], arrays + 1
        if "[" in name or "]" in name or (not name and (number or not arrays)):
            _mapping_invalid(
                "mapping.rows", "a row path is dotted names, each [] crossing an array", expected="a[].b[]"
            )
        if name:
            keys.append(name)
        for _ in range(arrays):
            routes.append(tuple(keys))
            keys = []
    if keys:
        _mapping_invalid("mapping.rows", "a row path ends with [], so each row is an array element", expected="a[].b[]")
    prefixes = tuple(raw[: index + 2] for index in range(len(raw)) if raw.startswith("[]", index))
    return _Rows(raw, tuple(routes), prefixes, tuple({} for _ in routes))


def _target(raw: Any, at: str, declared: Mapping[str, Any], used: set[str]) -> str:
    if type(raw) is not str or raw not in declared:
        _mapping_invalid(at, "target is not a declared collection field", allowed=sorted(declared))
    if raw in used:
        _mapping_invalid(at, "target is mapped twice", expected="one source per target")
    used.add(raw)
    return raw


def _field(target: str, spec: Any, at: str, fmt: str, rows: _Rows | None) -> tuple[_Field, dict]:
    """One field mapping: a source path, ``{from, type?, scale?}`` or ``{const}``."""
    if not isinstance(spec, Mapping):
        path = _path(spec, at, fmt)
        kind = "string" if fmt == "csv" else None
        return _Field(target, path, kind, _source(spec, at, fmt, rows)), {"from": list(path), "type": kind}
    if "const" in spec:
        if set(spec) != {"const"}:
            _mapping_invalid(at, "a literal is {const} alone", allowed=["const"])
        return _Field(target, (), None, _Path(literal=spec["const"])), {"const": spec["const"]}
    for key in sorted(set(spec) - {"from", "type", "scale"}):
        _mapping_invalid(f"{at}.{key}", "unknown field mapping key", allowed=["const", "from", "scale", "type"])
    kind = None
    if "type" in spec:
        if fmt != "csv":
            _mapping_invalid(f"{at}.type", "only csv text is converted; JSON values keep their type", expected=None)
        if spec["type"] not in CSV_TYPES:
            _mapping_invalid(f"{at}.type", "unsupported csv type", allowed=list(CSV_TYPES))
        kind = spec["type"]
    elif fmt == "csv":
        kind = "string"
    scale = spec.get("scale")
    if "scale" in spec and not _finite(scale):
        _mapping_invalid(f"{at}.scale", "scale is a finite number", expected="number")
    path = _path(spec.get("from"), f"{at}.from", fmt)
    canonical = {"from": list(path), "type": kind, **({"scale": scale} if "scale" in spec else {})}
    return _Field(target, path, kind, _source(spec["from"], f"{at}.from", fmt, rows), scale), canonical


def _basis(index: int, raw: Any, fmt: str, rows: _Rows | None) -> tuple[_Basis, dict]:
    """One time basis: a base, then a zone or an offset, then an optional increment."""
    at = f"mapping.time.from[{index}]"
    if not isinstance(raw, Mapping):
        _mapping_invalid(at, "a time basis is an object", expected="object")
    for key in sorted(set(raw) - set(_BASIS_KEYS)):
        _mapping_invalid(f"{at}.{key}", "unknown time basis key", allowed=sorted(_BASIS_KEYS))
    bases = [key for key in ("date", "instant") if key in raw]
    if len(bases) != 1:
        _mapping_invalid(at, "a time basis has one base, a date or an instant", allowed=["date", "instant"])
    places = [key for key in ("zone", "offset", "offset_minutes") if key in raw]
    increments = [key for key in ("seconds", "index") if key in raw]
    if len(places) > 1 or len(increments) > 1:
        _mapping_invalid(
            at, "a time basis has at most one zone or offset and one increment",
            allowed=[["zone", "offset", "offset_minutes"], ["seconds", "index"]],
        )
    if ("every" in raw) != ("index" in raw):
        _mapping_invalid(f"{at}.every", "index and every go together", allowed=["index", "every"])
    if increments and bases == ["date"] and not places:
        _mapping_invalid(at, "a date with an increment needs a zone or an offset", allowed=["zone", "offset", "offset_minutes"])
    zoned = "zone" in raw
    for key, needed, allowed in (("clock", zoned and bool(increments), _CLOCKS), ("fold", zoned, _FOLDS)):
        if needed and raw.get(key) not in allowed:
            _mapping_invalid(f"{at}.{key}", f"a zone{' with an increment' if key == 'clock' else ''} declares {key}",
                             allowed=list(allowed))
        if not needed and key in raw:
            _mapping_invalid(f"{at}.{key}", f"{key} applies only to a zone{' with an increment' if key == 'clock' else ''}",
                             expected=None)
    canonical: dict[str, Any] = {}
    paths: dict[str, _Path] = {}
    for key in _BASIS_PATHS:
        if key in raw:
            if key == "index" and (rows is None or raw[key] != _INDEX):
                _mapping_invalid(f"{at}.index", "index counts a json-document row's place in its array", allowed=[_INDEX])
            paths[key] = _source(raw[key], f"{at}.{key}", fmt, rows)
            canonical[key] = list(_path(raw[key], f"{at}.{key}", fmt))
    where = None
    if zoned:
        where = import_time.zone(raw["zone"])
        if where is None:
            _mapping_invalid(f"{at}.zone", "zone is an IANA zone the pinned rules hold", expected="Area/Location")
        canonical["zone"] = raw["zone"]
    every = unit = None
    if "every" in raw:
        given = raw["every"]
        if not isinstance(given, Mapping) or len(given) != 1 or next(iter(given)) not in _UNITS:
            _mapping_invalid(f"{at}.every", "every is {s: n} or {ms: n}", allowed=sorted(_UNITS))
        name, every = next(iter(given.items()))
        unit = _UNITS[name]
        if type(every) is str:
            canonical["every"] = {name: list(_path(every, f"{at}.every.{name}", fmt))}
            every = _source(every, f"{at}.every.{name}", fmt, rows)
        elif not _finite(every) or every <= 0:
            _mapping_invalid(f"{at}.every.{name}", "an interval is a positive number or a source path", expected="number")
        else:
            canonical["every"] = {name: every}
    for key in ("clock", "fold"):
        if key in raw:
            canonical[key] = raw[key]
    label = "+".join(key for key in ("date", "instant", *places, *increments) if key in raw)
    basis = _Basis(
        index, paths.get("date"), paths.get("instant"), where, paths.get("offset"), paths.get("offset_minutes"),
        paths.get("seconds"), paths.get("index"), every, unit, raw.get("clock"), raw.get("fold"), label,
    )
    return basis, canonical


def compile_mapping(raw: Any, manifest: collections.CollectionManifest, fmt: str) -> Plan:
    """Validate a declared mapping against the collection; nothing in it executes."""
    declared = manifest.schema.fields
    if not isinstance(raw, Mapping):
        _mapping_invalid("mapping", "mapping must be an object", expected="object")
    keys = ["coverage", "fields", "on_invalid", "time", *(["rows"] if fmt == "json-document" else [])]
    for key in sorted(set(raw) - set(keys)):
        _mapping_invalid(f"mapping.{key}", "unknown mapping key", allowed=sorted(keys))
    rows = None
    if fmt == "json-document":
        if "rows" not in raw:
            _mapping_invalid("mapping.rows", "a json-document mapping names its row path", expected="a[].b[]")
        rows = _row_path(raw["rows"])
    coverage = raw.get("coverage", {})
    if not isinstance(coverage, Mapping) or len(coverage) > 256:
        _mapping_invalid("mapping.coverage", "coverage must be a bounded source-path map")
    for path, spec in coverage.items():
        _path(path, "mapping.coverage", fmt)
        # S1 fixes location versus explicitly reviewed ordinary leaves; null never covers future descendants.
        if (not isinstance(spec, Mapping) or set(spec) - {"classification", "subtree"}
                or "classification" not in spec or spec["classification"] not in (None, "location")
                or type(spec.get("subtree", False)) is not bool
                or (spec.get("subtree") and spec["classification"] is None)):
            _mapping_invalid("mapping.coverage", "declare a leaf classification or a protected location subtree")
    fields_raw = raw.get("fields")
    if not isinstance(fields_raw, Mapping) or not fields_raw or len(fields_raw) > MAX_MAPPED_FIELDS:
        _mapping_invalid(
            "mapping.fields", "fields maps 1 to 64 target fields to source paths", expected="object"
        )
    used: set[str] = set()
    fields, canonical_fields = [], {}
    for target, spec in fields_raw.items():
        at = f"mapping.fields.{target}"
        if target not in declared:
            _mapping_invalid(
                at, "target is not a declared collection field", allowed=sorted(declared)
            )
        compiled, canonical_fields[target] = _field(_target(target, at, declared, used), spec, at, fmt, rows)
        fields.append(compiled)
    bases: list[_Basis] = []
    canonical_bases: list[dict] = []
    instant = offset = local_date = None
    time_raw = raw.get("time")
    if time_raw is not None:
        if not isinstance(time_raw, Mapping):
            _mapping_invalid("mapping.time", "time must be an object", expected="object")
        for key in sorted(set(time_raw) - {"from", "instant", "offset", "local_date"}):
            _mapping_invalid(
                f"mapping.time.{key}",
                "unknown time key",
                allowed=["from", "instant", "local_date", "offset"],
            )
        if "local_date" not in time_raw:
            _mapping_invalid(
                "mapping.time.local_date",
                "a time mapping names the field for the source-local day",
                expected="declared date field",
            )
        local_date = _target(time_raw["local_date"], "mapping.time.local_date", declared, used)
        if time_raw.get("instant") is not None:
            instant = _target(time_raw["instant"], "mapping.time.instant", declared, used)
        if time_raw.get("offset") is not None:
            offset = _target(time_raw["offset"], "mapping.time.offset", declared, used)
        alternatives = time_raw.get("from")
        if not isinstance(alternatives, list) or not 1 <= len(alternatives) <= 4:
            _mapping_invalid(
                "mapping.time.from", "from lists 1 to 4 time bases in order", expected="array"
            )
        for index, basis in enumerate(alternatives):
            compiled, canonical = _basis(index, basis, fmt, rows)
            bases.append(compiled)
            canonical_bases.append(canonical)
    on_invalid = raw.get("on_invalid", "stop")
    if on_invalid not in {"stop", "skip"}:
        _mapping_invalid(
            "mapping.on_invalid", "on_invalid is stop or skip", allowed=["skip", "stop"]
        )
    missing = [name for name in manifest.schema.natural_key if name not in used]
    if missing:
        _mapping_invalid(
            "mapping.fields", "every natural-key field must be mapped", expected=missing
        )
    canonical = {
        "fields": canonical_fields,
        "time": None
        if not bases
        else {"from": canonical_bases, "instant": instant, "offset": offset, "local_date": local_date},
        "on_invalid": on_invalid,
        "format": fmt,
        "coverage": dict(coverage),
        **({"rows": rows.text} if rows is not None else {}),
    }
    return Plan(
        tuple(fields),
        tuple(bases),
        instant,
        offset,
        local_date,
        on_invalid,
        tuple(manifest.schema.natural_key),
        canonical,
        rows,
    )


# Batching


@dataclass(slots=True)
class Batch:
    rows: list[tuple[Row, dict[str, Any]]]
    rejections: list[tuple[Row, str, str]]
    stop: tuple[Row, str, str] | None
    checkpoint: dict[str, Any]
    source_bytes: int
    eof: bool
    members: list[tuple[str, int, str | None]] = field(default_factory=list)


# A local time inside a gap does not exist; refusing it never stops the job.
_GAP = "TIME_LOCAL_GAP"


def _next_batch(reader, plan: Plan, pending: list[Row], order: import_time.Fold | None) -> Batch:
    begin = reader.position()["byte"] if not pending else pending[0].start
    rows: list[tuple[Row, dict[str, Any]]] = []
    rejections: list[tuple[Row, str, str]] = []
    checkpoint = reader.position() if not pending else None
    eof = ended = False
    spent = 0
    while len(rows) + len(rejections) < MAX_BATCH_ROWS:
        row = pending.pop() if pending else reader.next()
        if row is None or row is _MEMBER_END:
            eof, ended = row is None, True
            break
        extent = row.end - begin if row.span is None else spent + row.span
        if (rows or rejections) and extent > MAX_BATCH_BYTES:
            pending.append(row)
            break
        spent = extent
        error = row.error
        values = None
        if error is None:
            values, error = plan.apply(row.value, order)
            row.value = None
        if error is not None and (row.fatal or (plan.on_invalid == "stop" and error[0] != _GAP)):
            stopped = _ordered(checkpoint or reader.position(), order)
            return Batch(rows, rejections, (row, *error), stopped, 0, False)
        if error is not None:
            rejections.append((row, *error))
        else:
            rows.append((row, values))
        checkpoint = reader.after(row)
    if ended:
        checkpoint = reader.position()
    checkpoint = _ordered(checkpoint, order)
    return Batch(rows, rejections, None, checkpoint, checkpoint["byte"] - begin, eof)


def _ordered(checkpoint: dict[str, Any], order: import_time.Fold | None) -> dict[str, Any]:
    return checkpoint if order is None else {**checkpoint, "fold": order.state()}


def iter_batches(
    handle, fmt: str, plan: Plan, checkpoint: Mapping[str, Any] | None = None
) -> Iterator[Batch]:
    """Stream ≤500-row/≤4 MiB batches; memory is one batch and one buffered row."""
    if fmt == "json-document":
        checkpoint = checkpoint or {"row": 0, "byte": 0, "member": 0, "member_row": 0}
        reader = _Streams((_Stream(None, 0, lambda: (handle, None)),), fmt, plan.rows, checkpoint)
    else:
        checkpoint = checkpoint or _START
        reader = _Reader(handle, fmt, checkpoint)
    order = import_time.Fold(checkpoint.get("fold")) if plan.ordered else None
    pending = getattr(reader, "pending", [])
    while True:
        batch = _next_batch(reader, plan, pending, order)
        yield batch
        if batch.stop is not None or batch.eof:
            return


# Durable jobs


@dataclass(slots=True)
class _Job:
    id: str
    collection_id: str
    binding: dict[str, Any]
    state: str
    reason: str | None
    checkpoint: dict[str, Any]
    progress: dict[str, Any]
    window_started: int
    window_expires: int

    def principal(self) -> principal_module.RequestPrincipal:
        bound = self.binding["principal"]
        context = AuthorizationSessionContext(**bound["session"]) if bound["session"] else None
        return principal_module.RequestPrincipal(
            audience_id=bound["audience_id"],
            surface=bound["surface"],
            purpose=bound["purpose"],
            issuer_family=bound["issuer_family"],
            remote_owner=bound["remote_owner"],
            local_owner=bound["local_owner"],
            authorization_session_id=context.session_id if context else None,
            verified_authorization_session=context,
        )


def _load(conn, job_id: str) -> _Job | None:
    row = conn.execute(
        "SELECT job_id,collection_id,binding_json,state,reason,checkpoint_json,progress_json,"
        "window_started,window_expires FROM import_jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    return _Job(
        row[0],
        row[1],
        json.loads(row[2]),
        row[3],
        row[4],
        json.loads(row[5]),
        json.loads(row[6]),
        row[7],
        row[8],
    )


def _bound_principal(who: principal_module.RequestPrincipal) -> dict[str, Any]:
    context = who.verified_authorization_session
    session = None
    if isinstance(context, AuthorizationSessionContext):
        session = {name: getattr(context, name) for name in AuthorizationSessionContext.__slots__}
    return {
        "audience_id": who.audience_id,
        "surface": who.surface,
        "issuer_family": who.issuer_family,
        "purpose": who.purpose,
        "remote_owner": who.remote_owner,
        "local_owner": who.local_owner,
        "session": session,
    }


def _owns(who: principal_module.RequestPrincipal, job: _Job) -> bool:
    bound = job.binding["principal"]
    return who.resolved and (who.audience_id, who.issuer_family) == (
        bound["audience_id"],
        bound["issuer_family"],
    )


def _lineage(conn, cid: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT c.manifest_version,m.manifest_hash,c.type_name,c.type_version,c.encoding FROM collections c "
        "JOIN collection_manifests m ON m.collection_id=c.collection_id AND m.manifest_version=c.manifest_version "
        "WHERE c.collection_id=?",
        (cid,),
    ).fetchone()
    meta = dict(
        conn.execute(
            "SELECT key,value FROM store_meta WHERE key IN (?,?)",
            (schema.META_STORE_ID, schema.META_LINEAGE),
        )
    )
    lineage = meta.get(schema.META_LINEAGE)
    return {
        "collection_id": cid,
        "store_id": meta.get(schema.META_STORE_ID),
        "lineage": json.loads(lineage) if lineage else [],
        "manifest_version": row[0],
        "manifest_hash": row[1],
        "type": [row[2], row[3]],
        "encoding": row[4],
    }


def _holds(bound: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    """Whether the bound target still holds: the same store and declaration.

    A takeover appends its tenure to the store's lineage, so the bound lineage
    need only be a prefix of the current one. Another store, or a lineage that
    forks from the bound one, does not hold.
    """
    lineage = bound["lineage"]
    return current["lineage"][: len(lineage)] == lineage and all(
        current[key] == value for key, value in bound.items() if key != "lineage"
    )


def _identity(binding: Mapping[str, Any]) -> str:
    """What an identical start binds: principal, exact source, target, mapping."""
    principal = binding["principal"]
    return hashlib.sha256(
        _json(
            {
                "principal": [principal["audience_id"], principal["issuer_family"]],
                "source": binding["source"],
                "target": {k: v for k, v in binding["target"].items() if k != "lineage"},
                "mapping": binding["mapping"]["sha256"],
            }
        ).encode()
    ).hexdigest()


def _now() -> int:
    return int(time.time())


def _iso(timestamp: int) -> str:
    return dt.datetime.fromtimestamp(timestamp, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _status(writer, job: _Job, mode: str, **extra: Any) -> dict[str, Any]:
    progress, checkpoint = job.progress, job.checkpoint
    rejections = [
        {"row": ordinal, "byte": offset, "code": code, "at": at}
        for ordinal, offset, code, at in writer.connection.execute(
            "SELECT ordinal,byte_offset,code,at FROM import_rejections WHERE job_id=? ORDER BY ordinal LIMIT ?",
            (job.id, _LISTED),
        )
    ]
    state, reason, error = job.state, job.reason, progress["error"]
    checked = {}
    blocked = writer.handle.import_blocked.get(job.id) if state == "running" else None
    if blocked is not None:
        # The store refuses writes, so this pause is host-local; a later tick resumes.
        state, reason = "partial", "store_unavailable"
        error = {
            "code": blocked,
            "at": "store",
            "row": checkpoint["row"],
            "byte": checkpoint["byte"],
        }
    elif state == "running" and _now() >= job.window_expires:
        state, reason = "partial", "time_cap"
    elif state == "running":
        checked["authority"] = _verdict(writer, job)
        if checked["authority"] == "lost":
            state, reason = "partial", "authority_lost"
    bound = job.binding["source"]
    position = {"row": checkpoint["row"], "byte": checkpoint["byte"]}
    exported = {}
    if "member" in checkpoint:
        position.update(member=checkpoint["member"], member_row=checkpoint["member_row"])
    if "members" in bound:
        exported["members"] = {"selected": len(bound["members"]["sha256"]), **progress["members"]}
    return {
        "mode": mode,
        **extra,
        **checked,
        **exported,
        "continuation": _JOB_PREFIX + job.id,
        "state": state,
        "reason": reason,
        "collection_id": job.collection_id,
        "source_ref": job.binding["source"]["ref"],
        "rows": {
            "imported": progress["imported"],
            "rejected": progress["rejected"],
            "duplicates": progress["duplicates"],
            "remaining": 0 if state == "complete" else None,
            "inserted": progress["inserted"],
            "updated": progress["updated"],
            "unchanged": progress["unchanged"],
        },
        "source_bytes": {
            "consumed": checkpoint["byte"],
            "total": bound["members"]["bytes"] if "members" in bound else bound["bytes"],
        },
        "batches": progress["batches"],
        "window": {"started_at": _iso(job.window_started), "expires_at": _iso(job.window_expires)},
        "next_position": position,
        "last_receipt": progress["last_receipt"],
        "rejections": rejections,
        "error": error,
    }


@contextmanager
def _snapshot(writer):
    """One read transaction for fresh authority checks outside a mutation."""
    writer.handle.release_cache.check()
    writer.connection.execute("BEGIN")
    try:
        yield writer.connection
    finally:
        writer.connection.execute("ROLLBACK")


def _grant(writer, job: _Job, operation) -> None:
    """The bound principal's authority now, short of proving the source bytes; else _Lost.

    The checks are a live (never extended) session, the collection's complete-state
    mutation rule, the bound store and declaration (``_holds``) and release of the
    exact bound source digest.
    """
    bound = job.binding
    session = bound["principal"]["session"]
    if session is not None and _now() > session["expires_at"]:
        raise _Lost
    if not _owns(operation.who, job):
        raise _Lost
    try:
        operation.require_collection(job.collection_id, complete=True)
    except collections.CollectionError as error:
        raise _Lost from error
    if operation.failed or not _holds(
        bound["target"], _lineage(writer.connection, job.collection_id)
    ):
        raise _Lost
    source = bound["source"]
    if not operation.allows_file(source["ref"], content_sha256=source["sha256"]):
        raise _Lost


def _authorize(writer, job: _Job, *, prove: bool) -> Source:
    """Re-resolve the bound principal's authority now; raise _Lost when any part fails.

    Beyond ``_grant``, the bytes must match the bound digest: a host without its
    own proof of them re-hashes them when ``prove`` allows.
    """
    operation = writer._fresh_authorization()
    try:
        _grant(writer, job, operation)
        proof = _proofs(writer).get(job.id)
        if proof is None:
            if not prove:
                raise _Lost
            proof = _prove(writer, job, operation)
        proof.guard.recheck(writer.root)
        return proof
    except vault.PathGuardError as error:
        raise _Lost from error
    finally:
        operation.close()


def _verdict(writer, job: _Job) -> str:
    """What a fresh check finds for a durably running job: current, unverified or lost.

    A restarted host knows only the durable row; status reports this instead, so
    it never shows a bare running that the first tick turns into a pause.
    Unverified means the authority holds but this host has not yet proved the
    source bytes (its next batch re-hashes them), or the check could not run.
    """
    try:
        with principal_module.request_scope(job.principal()):
            operation = writer._fresh_authorization()
            try:
                _grant(writer, job, operation)
            finally:
                operation.close()
        proof = _proofs(writer).get(job.id)
        if proof is None:
            return "unverified"
        proof.guard.recheck(writer.root)
    except (_Lost, vault.PathGuardError):
        return "lost"
    except Exception:  # noqa: BLE001 - an unreadable check is reported, not guessed
        log.warning("import job authority check failed", exc_info=True)
        return "unverified"
    return "current"


def _proofs(writer) -> dict[str, Source]:
    """This writer handle's proofs of bound source bytes; a new host starts with none."""
    return writer.handle.import_proofs


def _prove(writer, job: _Job, operation) -> Source:
    bound = job.binding["source"]
    try:
        source, sha256, size = resolve_source(writer.root, operation, bound["ref"])
        if (sha256, size) != (bound["sha256"], bound["bytes"]):
            raise _Lost
        if "members" in bound:
            members = _export(writer.root, source, size, bound["members"]["select"])
            if [member.sha256 for member in members] != bound["members"]["sha256"]:
                raise _Lost
            source = Source(source.ref, source.path, source.guard, members)
    except collections.CollectionError as error:
        raise _Lost from error
    _proofs(writer)[job.id] = source
    return source


def _live(writer, job: _Job, proof: Source, plan: Plan) -> _Streams:
    """This handle's open reader for a streamed job, rebuilt unless it stands at the checkpoint.

    Keeping it open across batches reads each member once; after a batch that did not
    commit, or on a new handle, the job's checkpoint decides where reading resumes.
    """
    readers = writer.handle.import_readers
    reader = readers.get(job.id)
    if reader is not None and reader.marked == _cursor(job.checkpoint):
        return reader
    if reader is not None:
        reader.close()
    bound, root = job.binding["source"], writer.root
    if "members" in bound:
        streams = tuple(
            _Stream(member.sha256, member.bytes, lambda member=member: _open_member(root, member))
            for member in proof.members
        )
    else:
        streams = (_Stream(None, bound["bytes"], lambda: (_open_source(proof), None)),)
    skip = None
    if "members" in bound and bound["members"]["reimport"] is None:
        key = (job.collection_id, job.binding["mapping"]["sha256"])

        def skip(stream: _Stream) -> bool:
            return writer.connection.execute(
                "SELECT EXISTS(SELECT 1 FROM import_members WHERE collection_id=? "
                "AND mapping_sha256=? AND member_sha256=?)",
                (*key, stream.sha256),
            ).fetchone()[0] == 1

    reader = readers[job.id] = _Streams(
        streams, job.binding["mapping"]["format"], plan.rows, job.checkpoint, skip, bounded=True
    )
    return reader


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _stamp() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _collapse(
    rows: list[tuple[Row, dict[str, Any]]], manifest, source_ref: str, *, logged: bool = False
) -> tuple[list[tuple[Row, dict[str, Any]]], int, list[tuple[str | None, Any]]]:
    """Keep each natural key's last occurrence in a batch, by the writer's own identity rule.

    A row the writer would refuse has no identity here, so it neither supersedes
    nor is superseded: the last valid occurrence wins. When the collection has a
    natural key, or the job ``logged`` its members, it also returns each row's
    natural key and stored values as the writer derives them, ``(None, None)`` for
    a row the writer would refuse.
    """
    if not (manifest.schema.natural_key or logged):
        return rows, 0, []
    prepared: list[tuple[str | None, Any]] = []
    for _, values in rows:
        try:
            stored = records._bulk_row_values(manifest, values, source_ref)
            prepared.append((collections.derived_item_key(manifest, stored), stored))
        except collections.CollectionError:
            prepared.append((None, None))
    if not manifest.schema.natural_key:
        return rows, 0, prepared
    last = {key: index for index, (key, _) in enumerate(prepared) if key is not None}
    kept = [
        row
        for index, (row, (key, _)) in enumerate(zip(rows, prepared, strict=True))
        if key is None or last[key] == index
    ]
    return kept, len(rows) - len(kept), prepared


def _rewrote(writer, job: _Job, outcome: Mapping[str, Any]) -> bool:
    """Whether a row re-states an identity this job wrote in an earlier batch."""
    if (
        outcome["outcome"] not in {"updated", "unchanged"}
        or outcome.get("identity") != "natural-key"
    ):
        return False
    found = writer.connection.execute(
        "SELECT t.request_id FROM items i JOIN version_identity v ON v.row_id=i.row_id "
        "AND v.row_version=i.row_version-? JOIN txns t ON t.txn_id=v.txn_id "
        "WHERE i.collection_id=? AND i.item_key=?",
        (1 if outcome["outcome"] == "updated" else 0, job.collection_id, outcome["item_key"]),
    ).fetchone()
    return found is not None and (found[0] or "").startswith(f"import:{job.id}:")


def _record(
    writer,
    job: _Job,
    batch: Batch,
    *,
    kept: list[tuple[Row, dict[str, Any]]] = (),
    superseded: int = 0,
    result: Mapping[str, Any] | None = None,
    fail: tuple[Row, str, str] | None = None,
    prepared: list[tuple[str | None, Any]] = (),
    version: int = 0,
) -> None:
    """Advance the checkpoint, counters and rejections inside the batch transaction.

    A row superseded in this batch, or re-stating what this job wrote in an
    earlier one, counts once as a duplicate, so the counts do not depend on where
    batches end. ``batches`` counts settlements that committed rows. ``prepared``
    is ``_collapse``'s identity of each batch row and ``version`` the collection's
    schema version, from which an export member's import log hashes its rows.
    """
    progress = dict(job.progress)
    sequence = job.checkpoint["batch"]
    checkpoint = job.checkpoint
    if fail is not None:
        row, code, at = fail
        progress["error"] = {"code": code, "at": at, "row": row.ordinal, "byte": row.start}
        state, reason = "failed", "invalid_row"
    else:
        rejections = list(batch.rejections)
        counts = {"inserted": 0, "updated": 0, "unchanged": 0}
        duplicates = superseded
        for outcome in (result or {}).get("rows", ()):
            if outcome["outcome"] == "rejected":
                rejections.append(
                    (kept[outcome["index"]][0], outcome.get("code", "IMPORT_ROW_REJECTED"), "item")
                )
            elif _rewrote(writer, job, outcome):
                duplicates += 1
            else:
                counts[outcome["outcome"]] += 1
        if result is not None and result.get("committed"):
            progress["last_receipt"] = {
                "batch": sequence,
                "batch_id": result["batch_id"],
                "transition_id": result["first_transition"],
            }
            progress["batches"] += 1
        for name, value in counts.items():
            progress[name] += value
        progress["imported"] += sum(counts.values())
        progress["rejected"] += len(rejections)
        progress["duplicates"] += duplicates
        logged = _logged(batch, kept, result, prepared, version) if "members" in job.binding["source"] else []
        tally = _members(writer, job, batch, progress, logged)
        writer._execute(
            "INSERT INTO import_rejections(job_id,ordinal,byte_offset,code,at) VALUES (?,?,?,?,?)",
            [(job.id, row.ordinal, row.start, code, at) for row, code, at in rejections],
            many=True,
        )
        checkpoint = {**batch.checkpoint, "batch": sequence + 1}
        if tally is not None:
            checkpoint["tally"] = tally
        state, reason = ("complete" if batch.eof else "running"), None
    cursor = writer._execute(
        "UPDATE import_jobs SET state=?,reason=?,checkpoint_json=?,progress_json=?,updated_at=? "
        "WHERE job_id=? AND state='running' AND json_extract(checkpoint_json,'$.batch')=?",
        (state, reason, _json(checkpoint), _json(progress), _stamp(), job.id, sequence),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("import job changed under its batch")
    if state != "running" and not (result is not None and result.get("committed")):
        # Rows committed carry the change in their own transaction; otherwise record it.
        operation = "import_job_fail" if state == "failed" else "import_job_complete"
        _control(writer, job, operation, progress, checkpoint)


# A member's running row digest starts here; each accepted row extends it (``_members``).
_NO_ROWS = hashlib.sha256(b"").hexdigest()


def _logged(
    batch: Batch,
    kept: list[tuple[Row, dict[str, Any]]],
    result: Mapping[str, Any] | None,
    prepared: list[tuple[str | None, Any]],
    version: int,
) -> list[tuple[Row, str | None, str | None]]:
    """This batch's rows in source order as ``(row, item key, payload hash)``, both None if rejected.

    A row a later row of its member superseded in this batch was accepted all the same,
    so a member's counts and digest do not depend on where its batches end. The payload
    hash is ``tokens.payload_hash`` of the row the member supplies; an import has no body.
    """
    outcomes = {outcome["index"]: outcome for outcome in (result or {}).get("rows", ())}
    written = {id(row): index for index, (row, _) in enumerate(kept)}
    entries = [(row, None, None) for row, _, _ in batch.rejections]
    for (row, _), (key, stored) in zip(batch.rows, prepared, strict=True):
        index = written.get(id(row))
        if index is not None:
            if outcomes[index]["outcome"] == "rejected":
                entries.append((row, None, None))
                continue
            key = outcomes[index]["item_key"]
        entries.append((row, key, tokens.payload_hash(version, key, stored, "")))
    return sorted(entries, key=lambda entry: entry[0].ordinal)


def _members(
    writer,
    job: _Job,
    batch: Batch,
    progress: dict[str, Any],
    rows: list[tuple[Row, str | None, str | None]],
) -> dict[str, Any] | None:
    """Log each export member this batch finished; return the open member's tally.

    A member's tally counts its accepted and rejected rows and chains a SHA-256 over each
    accepted row's item key and payload hash, in member order. It rides in the job's
    checkpoint while the member is open, so a restart resumes it with the rows. The batch
    that leaves a member appends its import log row, and the ``import_member`` transition
    the row names, in the transaction that advances the checkpoint past it: a crash never
    leaves one without the other. A skipped member writes nothing.
    """
    if "members" not in job.binding["source"]:
        return None
    opened = job.checkpoint
    tallies = {opened["member"]: dict(opened["tally"])} if opened["member_row"] else {}
    for row, key, payload in rows:
        tally = tallies.setdefault(row.after["member"], _tally())
        if key is None:
            tally["rejected"] += 1
        else:
            tally["accepted"] += 1
            tally["rows_digest"] = hashlib.sha256(
                f"{tally['rows_digest']}\0{key}\0{payload}".encode()
            ).hexdigest()
    counted = dict(progress["members"])
    for kind, index, sha256 in batch.members:
        counted[kind] += 1
        if kind == "read":
            _log_member(writer, job, index, sha256, tallies.pop(index, None) or _tally())
    progress["members"] = counted
    at = batch.checkpoint
    return tallies.get(at["member"]) if at["member_row"] else None


def _tally() -> dict[str, Any]:
    return {"accepted": 0, "rejected": 0, "rows_digest": _NO_ROWS}


def _log_member(writer, job: _Job, index: int, sha256: str, tally: Mapping[str, Any]) -> None:
    """Append one member's import log row, named by its own ``import_member`` transition."""
    cid = job.collection_id
    rows = writer.connection.execute(
        "SELECT COUNT(*) FROM items WHERE collection_id=?", (cid,)
    ).fetchone()[0]
    counts = {"member_index": index, "accepted": tally["accepted"], "rejected": tally["rejected"],
              "row_count_after": rows}
    ids = {"import_job_ids": [job.id], "member_sha256": [sha256], "rows_digest": [tally["rows_digest"]]}
    [receipt] = writer.record_control_transition(
        "import_member", {cid: {"counts": counts, "ids": ids}}, why=f"import {job.id[:12]} member {index}"
    )
    [txn_id] = writer.connection.execute(
        "SELECT txn_id FROM txns WHERE transition_id=?", (receipt["transition_id"],)
    ).fetchone()
    writer._execute(
        "INSERT INTO import_members(collection_id,seq,txn_id,job_id,member_index,member_sha256,"
        "mapping_sha256,accepted,rejected,rows_digest,row_count_after) VALUES "
        "(?,(SELECT COALESCE(MAX(seq),0)+1 FROM import_members WHERE collection_id=?),?,?,?,?,?,?,?,?,?)",
        (cid, cid, txn_id, job.id, index, sha256, job.binding["mapping"]["sha256"], tally["accepted"],
         tally["rejected"], tally["rows_digest"], rows),
    )


def _control(
    writer,
    job: _Job,
    operation: str,
    progress: Mapping[str, Any] | None = None,
    checkpoint: Mapping[str, Any] | None = None,
) -> None:
    """Record one row-free job-state change as a content-free control transition.

    It joins the open writer mutation, advancing the store's commit_seq and head
    so the replica carries the change. The receipt holds the job id and counts,
    never the source, its path or a row value.
    """
    progress, checkpoint = progress or job.progress, checkpoint or job.checkpoint
    counts = {name: progress[name] for name in ("imported", "rejected", "duplicates", "batches")}
    writer.record_control_transition(
        operation,
        {
            job.collection_id: {
                "counts": {**counts, "rows_read": checkpoint["row"]},
                "ids": {"import_job_ids": [job.id]},
            }
        },
        why=f"import {job.id[:12]} {operation.removeprefix('import_job_').replace('_', ' ')}",
    )


class _Settlement:
    """Commit-time authority recheck plus progress, inside the writer's transaction."""

    def __init__(
        self,
        job: _Job,
        batch: Batch,
        kept: list[tuple[Row, dict[str, Any]]],
        superseded: int,
        plan: Plan,
        proof: Source,
        guards: list[vault.PathGuard] = (),
        prepared: list[tuple[str | None, Any]] = (),
        version: int = 0,
    ) -> None:
        self.job, self.batch, self.kept, self.superseded, self.plan = (
            job,
            batch,
            kept,
            superseded,
            plan,
        )
        self.source = (proof.ref, proof.guard)
        self.guards = guards
        self.prepared, self.version = prepared, version

    def __call__(self, writer, result: Mapping[str, Any]) -> None:
        _authorize(writer, self.job, prove=False)
        _recheck(writer, self.guards)
        rejected = [outcome for outcome in result["rows"] if outcome["outcome"] == "rejected"]
        if rejected and not result.get("committed") and self.plan.on_invalid == "stop":
            first = rejected[0]
            row = self.kept[first["index"]][0]
            _record(
                writer,
                self.job,
                self.batch,
                fail=(row, first.get("code", "IMPORT_ROW_REJECTED"), "item"),
            )
        else:
            _record(
                writer,
                self.job,
                self.batch,
                kept=self.kept,
                superseded=self.superseded,
                result=result,
                prepared=self.prepared,
                version=self.version,
            )


def _recheck(writer, guards) -> None:
    """Re-prove the member blobs a batch read; a changed one loses the job's authority."""
    try:
        for guard in guards:
            guard.recheck(writer.root)
    except vault.PathGuardError as error:
        raise _Lost from error


def _transaction(root: Path, writer, work) -> None:
    """One writer mutation, so a control transition can join it."""
    from .preview import _mutate

    def import_job_settlement():
        with writer._mutation():
            work()

    _mutate(root, import_job_settlement)


def _forget(writer, job_id: str) -> None:
    """Drop a job's host-local proof, open reader and store block once it stops running."""
    _proofs(writer).pop(job_id, None)
    writer.handle.import_blocked.pop(job_id, None)
    reader = writer.handle.import_readers.pop(job_id, None)
    if reader is not None:
        reader.close()


_SETTLED = {
    "time_cap": "import_job_pause",
    "authority_lost": "import_job_authority_lost",
    "batch_error": "import_job_fail",
}


def _settle(root: Path, writer, job: _Job, state: str, reason: str, error=None) -> str:
    """Pause or fail a running job outside a batch; a failure records its typed error.

    The driver records it under its own principal: the bound one may have lost
    the collection, and the transition carries no row or source content.
    """
    _forget(writer, job.id)

    def write():
        progress = {**job.progress, "error": error} if error is not None else job.progress
        cursor = writer._execute(
            "UPDATE import_jobs SET state=?,reason=?,progress_json=?,updated_at=? "
            "WHERE job_id=? AND state='running'",
            (state, reason, _json(progress), _stamp(), job.id),
        )
        if cursor.rowcount == 1:
            _control(writer, job, _SETTLED[reason], progress)

    _transaction(root, writer, write)
    return "paused"


# Jobs whose deferred pause already logged an unclassified error's traceback. Bounded,
# oldest first out: a job evicted by a flood of others logs one more traceback.
_TRACED_JOBS = 64
_traced: dict[str, None] = {}


def _pause(root: Path, writer, job: _Job, reason: str) -> str:
    """Record ``_step``'s pause, or defer it to the next tick when the record fails.

    A pause that could not be written is not a failed job: the job stays running,
    status reports the pause from its fresh check, and the next tick retries. A
    store or collection refusal is a coded, expected state and logs one line per
    tick. Any other error is a defect: its first deferral per job also logs the
    traceback, and later ticks stay one line.
    """
    try:
        return _settle(root, writer, job, "partial", reason)
    except Exception as error:  # noqa: BLE001 - never escalated to a job failure
        code = getattr(error, "code", None)
        # Any refusal that carries a code is an expected state, the store's BUSY OpError included.
        coded = isinstance(code, str)
        code = code if coded else type(error).__name__
        first = not coded and job.id not in _traced
        if first:
            _traced[job.id] = None
            while len(_traced) > _TRACED_JOBS:
                del _traced[next(iter(_traced))]
        log.warning(
            "import job %s %s pause deferred: %s", job.id[:12], reason, code, exc_info=first
        )
        return "deferred"


def _authorized_now(writer, job: _Job) -> bool:
    try:
        with _snapshot(writer):
            _authorize(writer, job, prove=False)
        return True
    except _Lost:
        return False


def _step(root: Path, writer, job_id: str) -> str:
    """Run one batch of one job under its bound principal, or pause it honestly.

    A pause is recorded outside the bound principal's scope, by the driver.
    """
    job = _load(writer.connection, job_id)
    if job is None or job.state != "running":
        return "idle"
    if _now() >= job.window_expires:
        return _pause(root, writer, job, "time_cap")
    with principal_module.request_scope(job.principal()):
        outcome = _batch(root, writer, job)
    if outcome == "authority_lost":
        return _pause(root, writer, job, "authority_lost")
    writer.handle.import_blocked.pop(job.id, None)
    settled = _load(writer.connection, job.id)
    if settled.state == "running" and settled.checkpoint["batch"] == job.checkpoint["batch"]:
        raise RuntimeError("import batch replay did not settle its checkpoint")
    if settled.state != "running":
        _forget(writer, job.id)
    return outcome


def _batch(root: Path, writer, job: _Job) -> str:
    """One batch as the bound principal: ``batch``, ``paused`` (a stop) or ``authority_lost``."""
    from .preview import _mutate

    try:
        with _snapshot(writer) as conn:
            proof = _authorize(writer, job, prove=True)
            generation, head = conn.execute(
                "SELECT generation,audit_head FROM collections WHERE collection_id=?",
                (job.collection_id,),
            ).fetchone()
            manifest = writer._collection_manifest(writer._collection_row(job.collection_id))[0]
        fmt = job.binding["mapping"]["format"]
        plan = compile_mapping(job.binding["mapping"]["declared"], manifest, fmt)
        order = import_time.Fold(job.checkpoint.get("fold")) if plan.ordered else None
        guards = []
        if _streamed(job.binding):
            reader = _live(writer, job, proof, plan)
            batch = _next_batch(reader, plan, reader.pending, order)
            batch.members = reader.taken(batch.checkpoint)
            reader.marked = _cursor(batch.checkpoint)
            guards = reader.guards()
        else:
            with _open_source(proof) as handle:
                batch = _next_batch(_Reader(handle, fmt, job.checkpoint), plan, [], order)
    except (_Lost, OSError, vault.PathGuardError):
        return "authority_lost"
    sequence = job.checkpoint["batch"]
    logged = "members" in job.binding["source"]  # an export member's log hashes every row
    kept, superseded, prepared = _collapse(batch.rows, manifest, proof.ref, logged=logged)
    version = manifest.schema.version
    try:
        if batch.stop is not None or not kept:

            def settle_alone():
                _authorize(writer, job, prove=False)
                _recheck(writer, guards)
                if batch.stop is not None:
                    _record(writer, job, batch, fail=batch.stop)
                else:
                    _record(writer, job, batch, superseded=superseded, prepared=prepared, version=version)

            _transaction(root, writer, settle_alone)
            return "paused" if batch.stop is not None else "batch"
        _mutate(
            root,
            writer.bulk_upsert_records,
            job.collection_id,
            rows=[{"item": values} for _, values in kept],
            why=f"import {job.id[:12]} batch {sequence}",
            expected_container_hash=tokens.container_hash(job.collection_id, generation, head),
            source=proof.ref,
            on_reject="skip" if plan.on_invalid == "skip" else "abort",
            request_id=f"import:{job.id}:{sequence}",
            _import=_Settlement(job, batch, kept, superseded, plan, proof, guards, prepared, version),
        )
        return "batch"
    except _Lost:
        return "authority_lost"
    except (collections.CollectionError, vault.PathGuardError):
        if not _authorized_now(writer, job):
            return "authority_lost"
        raise


def _after_error(root: Path, writer, job_id: str, sequence: int, error: Exception) -> None:
    """Classify a batch error once its transaction is gone.

    If the checkpoint moved, the batch committed and only its acknowledgement
    failed. A store refusal (busy, sync pending, diverged, custody) blocks the
    job on this host until a later tick finds the store writable. Anything
    else fails the job with a typed code.
    """
    job = _load(writer.connection, job_id)
    if job is None or job.state != "running" or job.checkpoint["batch"] != sequence:
        return
    code = getattr(error, "code", None)
    if code in _STORE_STATE:
        writer.handle.import_blocked[job_id] = code
        return
    if not isinstance(code, str) or not isinstance(
        error, (collections.CollectionError, connection.CollectionStoreError)
    ):
        code = "IMPORT_BATCH_FAILED"
    checkpoint = job.checkpoint
    _settle(
        root,
        writer,
        job,
        "failed",
        "batch_error",
        {"code": code, "at": "batch", "row": checkpoint["row"], "byte": checkpoint["byte"]},
    )


def run_jobs(
    vault_root: Path, *, max_batches: int | None = None, deadline: float | None = None
) -> dict[str, int]:
    """Advance running import jobs on the single store writer's own thread.

    The single-writer service calls this between requests with a batch or
    monotonic-deadline budget. Each batch is an ordinary writer-lease
    transaction; ``_after_error`` settles a batch that raised, and a pause whose
    record failed is retried on the next tick (``deferred``).
    """
    from .preview import bound_writer

    writer = bound_writer(vault_root)
    if writer is None:
        raise connection.CollectionStoreError(
            "COLLECTION_STORE_PREVIEW_REQUIRED", "import jobs run only on the bound store writer"
        )
    if not vault.STAT_GENERATION_TRUSTED:
        _proofs(writer).clear()
    # The driver records pauses and failures as the in-process service; each
    # batch still runs as its job's bound principal.
    with principal_module.library_scope():
        return _drive(Path(vault_root), writer, max_batches, deadline)


def _drive(root: Path, writer, max_batches: int | None, deadline: float | None) -> dict[str, int]:
    summary = {"batches": 0, "paused": 0, "deferred": 0, "errors": 0}
    running = [
        row[0]
        for row in writer.connection.execute(
            "SELECT job_id FROM import_jobs WHERE state='running' ORDER BY created_at, job_id"
        )
    ]
    for job_id in running:
        while (max_batches is None or summary["batches"] < max_batches) and (
            deadline is None or time.monotonic() < deadline
        ):
            job = _load(writer.connection, job_id)
            sequence = job.checkpoint["batch"] if job is not None else None
            try:
                outcome = _step(root, writer, job_id)
            except Exception as error:  # noqa: BLE001 - settled by _after_error
                log.warning("import batch raised; settling the job", exc_info=True)
                summary["errors"] += 1
                try:
                    _after_error(root, writer, job_id, sequence, error)
                except Exception:  # noqa: BLE001 - the store refused even the settlement
                    log.warning("import job settlement deferred to a later tick", exc_info=True)
                break
            if outcome != "batch":
                summary["paused"] += outcome == "paused"
                summary["deferred"] += outcome == "deferred"
                break
            summary["batches"] += 1
    return summary


# Agent route

_PROGRESS = {
    "imported": 0,
    "rejected": 0,
    "duplicates": 0,
    "inserted": 0,
    "updated": 0,
    "unchanged": 0,
    "batches": 0,
    "last_receipt": None,
    "error": None,
}


def dispatch(vault_root: Path, writer, collection: str, raw: Any) -> dict[str, Any]:
    """Serve one validated ``record_memory`` import request on the bound store writer."""
    from .preview import _mutate

    request = _parse_request(raw)
    root = Path(vault_root)
    if request.mode == "preview":
        return _preview(root, writer, collection, request)
    if request.mode == "status":
        with writer.read_snapshot():
            return _status(writer, _visible(writer, collection, request.job_id), "status")
    if request.mode == "cancel":
        return _mutate(root, _cancel, writer, collection, request)
    if request.job_id is not None:
        return _mutate(root, _continue, root, writer, collection, request)
    return _mutate(root, _start, root, writer, collection, request)


def _visible(writer, collection: str, job_id: str) -> _Job:
    """Job ownership, target and source access; every refusal is the same not-found."""
    job = _load(writer.connection, job_id)
    if job is None or not _owns(principal_module.effective_principal(), job):
        _job_not_found()
    try:
        row = writer._collection(collection, facade_profile="records")[0]
        source = job.binding["source"]
        released = writer._operation.allows_file(source["ref"], content_sha256=source["sha256"])
    except collections.CollectionError:
        _job_not_found()
    if row["collection_id"] != job.collection_id or not released:
        _job_not_found()
    return job


def _start(root: Path, writer, collection: str, request: _Request) -> dict[str, Any]:
    """Bind a new job, or return the live one an identical start already bound."""
    who = principal_module.effective_principal()
    if not who.resolved:
        _refuse(
            "IMPORT_PRINCIPAL_REQUIRED",
            "an import job binds a resolved principal",
            at="import_request",
            repair="call import from an identified session",
        )
    with writer.read_snapshot():
        row, manifest, _ = writer._collection(collection, facade_profile="records")
        request = _resolved(writer, row, manifest, request)
        if request.reimport is not None and request.members is None:
            _invalid("import_request.reimport", "reimport applies to an export's members", expected="members")
        plan = compile_mapping(request.mapping, manifest, request.format)
        if not writer._operation.field_plan(manifest).owner:
            from .field_admission import UNRESOLVED, mapping_classes

            # Missing coverage could publish private data; the recipient must obtain a reviewed mapping from the owner.
            if UNRESOLVED in mapping_classes(request.mapping, manifest, request.format).values():
                _mapping_invalid("mapping.coverage", "external import requires reviewed source-path coverage")
    cid = row["collection_id"]
    with _snapshot(writer):
        operation = writer._fresh_authorization()
        try:
            operation.require_collection(cid, complete=True)
            source, sha256, size = resolve_source(root, operation, request.source_ref)
            if request.members is not None:
                members = _export(root, source, size, request.members)
                source = Source(source.ref, source.path, source.guard, members)
        finally:
            operation.close()
    binding = {
        "version": 1,
        "principal": _bound_principal(who),
        "source": {"ref": source.ref, "sha256": sha256, "bytes": size},
        "target": _lineage(writer.connection, cid),
        "mapping": {
            "format": request.format,
            "declared": request.mapping,
            "sha256": hashlib.sha256(_json(plan.canonical).encode()).hexdigest(),
        },
    }
    if request.saved is not None:
        binding["mapping"]["saved"] = request.saved
    if request.members is not None:
        binding["source"]["members"] = {
            "select": request.members if type(request.members) is str else list(request.members),
            "sha256": [member.sha256 for member in source.members],
            "bytes": sum(member.bytes for member in source.members),
            "reimport": request.reimport,
        }
    if plan.zoned:
        binding["mapping"]["zone_rules"] = import_time.version()
    streamed = _streamed(binding)
    start = {"row": 0, "byte": 0, "member": 0, "member_row": 0} if streamed else dict(_START)
    identity = _identity(binding)
    with writer._mutation():
        latest = writer.connection.execute(
            "SELECT job_id FROM import_jobs WHERE identity=? AND state<>'failed' "
            "AND reason IS NOT 'cancelled' ORDER BY created_at DESC, job_id DESC LIMIT 1",
            (identity,),
        ).fetchone()
        if latest is None:
            operation = writer._fresh_authorization()
            try:
                operation.require_collection(cid, complete=True)
                if not operation.allows_file(source.ref, content_sha256=sha256):
                    _source_not_found()
                source.guard.recheck(root)
            except vault.PathGuardError:
                _source_not_found()
            finally:
                operation.close()
            if not _holds(binding["target"], _lineage(writer.connection, cid)):
                _refuse(
                    "IMPORT_LINEAGE_CHANGED",
                    "the collection changed while the import was being bound",
                    at="collection",
                    repair="retry the start",
                    retryable=True,
                )
            job_id, now = secrets.token_hex(16), _now()
            writer._execute(
                "INSERT INTO import_jobs(job_id,identity,collection_id,binding_json,state,reason,"
                "checkpoint_json,progress_json,window_started,window_expires,created_at,updated_at) "
                "VALUES (?,?,?,?,'running',NULL,?,?,?,?,?,?)",
                (
                    job_id,
                    identity,
                    cid,
                    _json(binding),
                    _json({**start, "batch": 0}),
                    _json(
                        {**_PROGRESS, "members": {"read": 0, "skipped": 0}}
                        if request.members is not None
                        else _PROGRESS
                    ),
                    now,
                    now + JOB_WINDOW_SECONDS,
                    _stamp(),
                    _stamp(),
                ),
            )
            _control(writer, _load(writer.connection, job_id), "import_job_start")
    if latest is None:
        _proofs(writer)[job_id] = source
        return _status(writer, _load(writer.connection, job_id), "start")
    return _status(writer, _load(writer.connection, latest[0]), "start", replayed=True)


def _cancel(writer, collection: str, request: _Request) -> dict[str, Any]:
    # Cancelling needs only sight of the job, not write authority over its collection.
    with writer._authorization(mutation=False), writer._mutation():
        job = _visible(writer, collection, request.job_id)
        if job.state == "running" or (job.state == "partial" and job.reason in _RESUMABLE):
            writer._execute(
                "UPDATE import_jobs SET state='partial',reason='cancelled',updated_at=? WHERE job_id=?",
                (_stamp(), job.id),
            )
            _control(writer, job, "import_job_cancel")
            job.state, job.reason = "partial", "cancelled"
            _forget(writer, job.id)
        return _status(writer, job, "cancel")


def _continue(root: Path, writer, collection: str, request: _Request) -> dict[str, Any]:
    """Explicit renewal by the bound principal: re-prove source, lineage and live authority.

    A job still durably running renews too once status would report it paused
    (its window ended or its authority lapsed), before the driver records that.
    """
    with writer.read_snapshot():
        job = _visible(writer, collection, request.job_id)
        lapsed = job.state == "running" and (
            _now() >= job.window_expires or _verdict(writer, job) == "lost"
        )
    if job.state == "running" and not lapsed:
        return _status(writer, job, "start")
    if not (lapsed or (job.state == "partial" and job.reason in _RESUMABLE)):
        _refuse(
            "IMPORT_JOB_NOT_RESUMABLE",
            f"a {job.state} import ({job.reason}) cannot continue",
            at="import_request.continuation",
            expected="a job paused by authority_lost or time_cap",
            repair="start a new import",
        )
    job.binding = {
        **job.binding,
        "principal": _bound_principal(principal_module.effective_principal()),
    }
    _proofs(writer).pop(job.id, None)
    try:
        with _snapshot(writer):
            _authorize(writer, job, prove=True)
    except _Lost:
        _refuse(
            "IMPORT_AUTHORITY_UNAVAILABLE",
            "the job's exact source, collection lineage or authority does not currently hold",
            at="import_request.continuation",
            repair="restore access, or start a new import",
            retryable=True,
        )
    with writer._mutation():
        now = _now()
        cursor = writer._execute(
            "UPDATE import_jobs SET binding_json=?,state='running',reason=NULL,window_started=?,window_expires=?,"
            "updated_at=? WHERE job_id=? AND state IN ('partial','running')",
            (_json(job.binding), now, now + JOB_WINDOW_SECONDS, _stamp(), job.id),
        )
        if cursor.rowcount == 1:
            _control(writer, job, "import_job_resume")
    return _status(writer, _load(writer.connection, job.id), "start")


# Preview


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "integer"
    if type(value) is float:
        return "number"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"


def _time_kind(text: str) -> str | None:
    if _offset_minutes(text) is not None:
        return "offset"
    if _DATE.fullmatch(text):
        return "date"
    try:
        scalars.parse_instant(text)
        return "instant"
    except scalars.ScalarValueError:
        return "unzoned" if _unzoned(text) else None


class _Sample:
    """Bounded facts about ≤100 source rows; no day is ever inferred."""

    def __init__(self, plan: Plan | None, manifest) -> None:
        self.plan, self.manifest = plan, manifest
        self.rows = self.valid = 0
        self.paths: dict[str, set[str]] = {}
        self.nested: dict[str, str] = {}
        self.time_fields: dict[str, set[str]] = {}
        self.flagged: list[dict[str, Any]] = []
        self.bases: dict[str, int] = {}
        self.errors: list[dict[str, Any]] = []
        self.conflicts: dict[str, set[str]] = {}
        self.present: set[str] = set()
        self.absent: dict[str, int] = {}
        self.truncated = {"fields": False, "nested": False, "time_fields": False}

    def _note(self, section: str, found: dict, path: str, value: Any, *, add: bool = False) -> None:
        """Record one path under the same bound for every shape section."""
        if path in found or len(found) < _PREVIEW_PATHS:
            if add:
                found.setdefault(path, set()).add(value)
            else:
                found[path] = value
        else:
            self.truncated[section] = True

    def observe(self, value: Mapping[str, Any], prefix: str = "", depth: int = 0) -> None:
        for key, item in value.items():
            path, kind = f"{prefix}{key}", _json_type(item)
            if len(path.encode()) > _PATH_BYTES:
                self.truncated["fields"] = True  # longer than any mapping path can name
                continue
            self._note("fields", self.paths, path, kind, add=True)
            if kind == "object":
                self._note("nested", self.nested, path, "object")
                if depth < 3:
                    self.observe(item, f"{path}.", depth + 1)
            elif kind == "array":
                inner = sorted({_json_type(element) for element in item[:8]})
                self._note(
                    "nested", self.nested, path, f"array<{'|'.join(inner)}>" if inner else "array"
                )
            elif kind == "string" and (time_kind := _time_kind(item)) is not None:
                self._note("time_fields", self.time_fields, path, time_kind, add=True)

    def add(self, row: Row, order: import_time.Fold | None) -> None:
        self.rows += 1
        if row.error is not None:
            if len(self.errors) < _LISTED:
                self.errors.append(
                    {"row": row.ordinal, "byte": row.start, "code": row.error[0], "at": row.error[1] or None}
                )
            return
        shown = row.value.row if type(row.value) is _Context else row.value
        if isinstance(shown, Mapping):
            self.observe(shown)
        else:
            self._note("fields", self.paths, _VALUE, _json_type(shown), add=True)
        if self.plan is None:
            self.valid += 1
            return
        for spec in self.plan.fields:
            if not spec.path:
                continue
            if spec.source.read(row.value) is _ABSENT:
                self.absent[spec.target] = self.absent.get(spec.target, 0) + 1
            else:
                self.present.add(spec.target)
        values, error = self.plan.values(row.value)
        if self.plan.bases:
            times, flagged, basis = self.plan.time(row.value, order)
            if flagged is not None and len(self.flagged) < 2 * _LISTED:
                self.flagged.append({"row": row.ordinal, "code": flagged[0], "at": flagged[1]})
            elif flagged is None:
                self.bases[basis.label] = self.bases.get(basis.label, 0) + 1
            error = error or flagged
            if error is None:
                values.update(times)
        if error is not None:
            return
        self.valid += 1
        for target, value in values.items():
            try:
                collections.validate_field_value(target, value, self.manifest.schema.fields[target])
            except collections.CollectionError:
                self.conflicts.setdefault(target, set()).add(_json_type(value))

    def findings(self) -> list[dict[str, Any]]:
        found = []
        fields = self.manifest.schema.fields
        for target, observed in sorted(self.conflicts.items()):
            found.append(
                {
                    "code": "IMPORT_TYPE_CONFLICT",
                    "at": f"mapping.fields.{target}",
                    "expected": fields[target].type,
                    "allowed": sorted(observed),
                    "repair": "map a source path whose values have the declared type",
                    "retryable": False,
                }
            )
        for spec in self.plan.fields if self.plan else ():
            if spec.path and spec.target not in self.present and self.valid:
                found.append(
                    {
                        "code": "IMPORT_PATH_ABSENT",
                        "at": f"mapping.fields.{spec.target}",
                        "expected": ".".join(spec.path),
                        "allowed": None,
                        "repair": "the sampled rows never carry this path",
                        "retryable": False,
                    }
                )
        return found


def _sampled(sample: _Sample, reader, order: import_time.Fold | None) -> bool:
    """Sample up to the preview bound; whether no row ended the read early."""
    while sample.rows < PREVIEW_ROWS and (parsed := reader.next()) is not None:
        sample.add(parsed, order)
        if parsed.fatal:
            return False
    return True


def _preview(root: Path, writer, collection: str, request: _Request) -> dict[str, Any]:
    with writer.read_snapshot():
        row, manifest, declared = writer._collection(collection, facade_profile="records")
        # Raw shape previews cannot attest to field-only disclosure; a refused recipient asks the owner to preview.
        if not writer._operation.field_plan(manifest).owner:
            _source_not_found()
        request = _resolved(writer, row, manifest, request)
        source, _, size = resolve_source(root, writer._operation, request.source_ref)
        members = () if request.members is None else _export(root, source, size, request.members)
        plan, findings, rows = None, [], None
        if request.mapping is not None:
            try:
                plan = compile_mapping(request.mapping, manifest, request.format)
                rows = plan.rows
            except collections.CollectionError as error:
                findings.append({"code": error.code, **error.details})
        if request.format == "json-document" and rows is None:
            # Rows still sample under a valid row path when the rest of the mapping is wrong.
            try:
                rows = _row_path(request.mapping.get("rows") if isinstance(request.mapping, Mapping) else None)
            except collections.CollectionError:
                rows = None
        sample = _Sample(plan, manifest)
        order = import_time.Fold() if plan is not None and plan.ordered else None
        if request.format == "json-document" and rows is None:
            complete = False
        elif members or request.format == "json-document":
            streams = tuple(
                _Stream(member.sha256, member.bytes, lambda member=member: _open_member(root, member))
                for member in members
            ) or (_Stream(None, size, lambda: (_open_source(source), None)),)
            reader = _Streams(streams, request.format, rows, {"row": 0, "byte": 0, "member": 0, "member_row": 0})
            try:
                complete = _sampled(sample, reader, order) and reader.next() is None
            except (_Lost, OSError, vault.PathGuardError):
                _source_not_found()
            finally:
                reader.close()
        else:
            try:
                with _open_source(source) as handle:
                    reader = _Reader(handle, request.format, _START)
                    complete = _sampled(sample, reader, order) and reader.at_end()
            except (OSError, vault.PathGuardError):
                _source_not_found()
        encoding = writer.connection.execute(
            "SELECT encoding FROM collections WHERE collection_id=?", (row["collection_id"],)
        ).fetchone()[0]
        manifest_text = writer.connection.execute(
            "SELECT manifest_text FROM collection_manifests WHERE collection_id=? AND manifest_version=?",
            (row["collection_id"], row["manifest_version"]),
        ).fetchone()[0]
    produced = (
        None
        if plan is None
        else {spec.target for spec in plan.fields}
        | {name for name in (plan.instant, plan.offset, plan.local_date) if name}
    )
    inferred = {
        target: path
        for target in manifest.schema.fields
        for path in sorted(sample.paths)
        if path.rsplit(".", 1)[-1] == target
    }
    recommended = (
        None
        if plan is None
        else import_recommendations.recommend(
            plan, manifest, vault.parse_frontmatter(manifest_text, strict=True)[0], declared.indexes
        )
    )
    return {
        "mode": "preview",
        "collection_id": row["collection_id"],
        "format": request.format,
        "source": {
            "ref": source.ref,
            "bytes": size,
            **(
                {"members": {"selected": len(members), "bytes": sum(member.bytes for member in members)}}
                if members
                else {}
            ),
        },
        "rows": {
            "sampled": sample.rows,
            "valid": sample.valid,
            "invalid": sample.rows - sample.valid,
            "complete": complete,
        },
        "fields": {path: sorted(kinds) for path, kinds in sample.paths.items()},
        "nested": sample.nested,
        "truncated": sample.truncated,
        "time": {
            "fields": {path: sorted(kinds) for path, kinds in sample.time_fields.items()},
            "bases": sample.bases if plan is not None and plan.bases else None,
            "flagged": sample.flagged if plan is not None and plan.bases else None,
        },
        "mapping": {
            "declared": plan is not None,
            "saved": request.saved,
            "absent": sample.absent,
            "inferred": inferred,
            "findings": (findings + sample.findings())[: 2 * _LISTED],
        },
        "identity": {
            "natural_key": list(manifest.schema.natural_key),
            "mapped": None if produced is None else set(manifest.schema.natural_key) <= produced,
        },
        "target": {
            "fields": {name: spec.type for name, spec in manifest.schema.fields.items()},
            "encoding": encoding,
        },
        "recommended_declarations": recommended,
        "errors": sample.errors,
        "limits": {
            "preview_rows": PREVIEW_ROWS,
            "row_bytes": MAX_ROW_BYTES,
            "depth": MAX_DEPTH,
            "batch_rows": MAX_BATCH_ROWS,
            "batch_bytes": MAX_BATCH_BYTES,
            "job_seconds": JOB_WINDOW_SECONDS,
        },
    }
