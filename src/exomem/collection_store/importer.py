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
authority never substitutes, another principal never inherits a job, and expiry
is renewed only by the bound principal's explicit continuation. A batch is one
ordinary writer transaction (``bulk_upsert_records``) whose checkpoint, counters
and rejections commit with its rows, so a crash or retry can neither skip nor
repeat effects. Source text is data: it never reaches SQL text or executes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Never

from .. import records, vault
from .. import structured_collections as collections
from ..governance import principal as principal_module
from ..governance.authorization_session_lifecycle import AuthorizationSessionContext
from ..query_engine import scalars
from . import connection, governance, schema, takeover, tokens

log = logging.getLogger(__name__)

FORMATS = ("ndjson", "json-array", "csv")
MAX_ROW_BYTES = 1 << 20
MAX_DEPTH = 32
MAX_BATCH_ROWS = 500
MAX_BATCH_BYTES = 4 << 20
PREVIEW_ROWS = 100
JOB_WINDOW_SECONDS = 3600
MAX_REQUEST_BYTES = 16 << 10
MAX_MAPPED_FIELDS = 64
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
            "stream one preserved Sources or Evidence file into this collection as a durable "
            "job of bounded batches, each committed whole or not at all"
        ),
        "import_request": {
            "mode": "preview, start, status or cancel",
            "source_ref": "vault path of a preserved Sources or Evidence file; preview and start",
            "format": f"{', '.join(FORMATS)}; csv is RFC 4180 UTF-8 with a header row",
            "mapping": {
                "fields": (
                    f"1 to {MAX_MAPPED_FIELDS} declared target fields, each a dotted source path "
                    "(a csv column name) or {from, type}; type converts csv text only: "
                    + ", ".join(CSV_TYPES)
                ),
                "time": (
                    "optional {from: 1 to 4 ordered bases, each {date} or {instant, offset?}, "
                    "instant?, offset?, local_date}: the first basis present gives the UTC "
                    "instant, its offset and the source-local day; a day is never guessed"
                ),
                "on_invalid": "stop (default) fails at the first invalid row; skip records each one",
            },
            "continuation": (
                "the job token start returns; status, cancel, or start again to resume a job "
                "paused by authority_lost or time_cap. An identical start returns the same job "
                "unless it failed or was cancelled"
            ),
        },
        "identity": (
            "rows sharing a natural key: the last valid occurrence wins whatever the batching; "
            "status counts the superseded ones as duplicates"
        ),
        "preview": (
            f"reads up to {PREVIEW_ROWS} rows and writes nothing: fields, nested shape, time "
            "fields, flagged rows, mapping findings and row identity"
        ),
        "states": {
            "running": "batches continue in the store writer",
            "partial": (
                "paused: authority_lost or time_cap until continued, cancelled for good, or "
                "store_unavailable until the store accepts writes again"
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


_MODE_FIELDS = {
    "preview": ({"source_ref", "format"}, {"mapping"}),
    "start": ({"source_ref", "format", "mapping"}, set()),
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
    unknown = sorted(set(raw) - {"mode", "source_ref", "format", "mapping", "continuation"})
    if unknown:
        _invalid(
            f"import_request.{unknown[0]}",
            "unknown import_request key",
            allowed=["continuation", "format", "mapping", "mode", "source_ref"],
        )
    mode = raw.get("mode")
    if mode not in _MODE_FIELDS:
        _invalid("import_request.mode", "mode is required", allowed=list(_MODE_FIELDS))
    supplied = {key for key, value in raw.items() if value is not None} - {"mode"}
    required, optional = _MODE_FIELDS[mode]
    if mode == "start" and "continuation" in supplied:
        required, optional = {"continuation"}, set()
    for name in sorted(required - supplied):
        _invalid(f"import_request.{name}", f"{mode} requires {name}")
    for name in sorted(supplied - required - optional):
        _invalid(f"import_request.{name}", f"{mode} does not take {name}")
    if "format" in supplied and raw["format"] not in FORMATS:
        _invalid("import_request.format", "unsupported format", allowed=list(FORMATS))
    job_id = None
    if "continuation" in supplied:
        matched = (
            _JOB_TOKEN.fullmatch(raw["continuation"]) if type(raw["continuation"]) is str else None
        )
        if matched is None:
            _job_not_found()
        job_id = matched[1]
    return _Request(mode, raw.get("source_ref"), raw.get("format"), raw.get("mapping"), job_id)


# Source resolution


@dataclass(frozen=True, slots=True)
class Source:
    ref: str
    path: Path
    guard: vault.PathGuard


def resolve_source(
    root: Path, operation: governance.OperationAuthorization, reference: Any
) -> tuple[Source, str, int]:
    """The one gate from a source ref to readable bytes, with their SHA-256 and size.

    ``records.resolve_preserved_source`` decides which refs name a preserved
    Sources/Evidence file; this streams the file's digest under a generation guard,
    then asks ordinary authorization to release exactly those bytes, so a governed
    session never makes the reader load a large file whole. Absent, unpreserved,
    withheld and malformed refs refuse identically; there is no filesystem-path,
    URL or executable form.
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
        sha256, size = _digest(root, source)
        found.append((source, sha256, size))
        return operation.allows_file(relative, content_sha256=sha256)

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


# Mapping


_ABSENT = object()
_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True, slots=True)
class _Field:
    target: str
    path: tuple[str, ...]
    kind: str | None


@dataclass(frozen=True, slots=True)
class _Basis:
    index: int
    instant: tuple[str, ...] | None = None
    offset: tuple[str, ...] | None = None
    date: tuple[str, ...] | None = None


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

    def apply(
        self, value: Mapping[str, Any]
    ) -> tuple[dict[str, Any] | None, tuple[str, str] | None]:
        out: dict[str, Any] = {}
        for spec in self.fields:
            found = _lookup(value, spec.path)
            if found is _ABSENT:
                continue
            if spec.kind is not None:
                found = _coerce(found, spec.kind)
                if found is _ABSENT:
                    return None, ("IMPORT_VALUE_INVALID", f"mapping.fields.{spec.target}")
            out[spec.target] = found
        if self.bases:
            times, error = self.time(value)
            if error is not None:
                return None, error
            out.update(times)
        return out, None

    def time(self, value: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, str] | None]:
        """Resolve the declared time basis; a missing one is flagged, never guessed."""
        for basis in self.bases:
            at = f"mapping.time.from[{basis.index}]"
            if basis.date is not None:
                raw = _lookup(value, basis.date)
                if raw is _ABSENT or raw is None:
                    continue
                try:
                    if type(raw) is not str or not _DATE.fullmatch(raw):
                        raise ValueError
                    dt.date.fromisoformat(raw)
                except ValueError:
                    return {}, ("TIME_BASIS_INVALID", f"{at}.date")
                return {self.local_date: raw}, None
            raw = _lookup(value, basis.instant)
            offset_raw = _lookup(value, basis.offset) if basis.offset is not None else None
            if raw is _ABSENT or raw is None or offset_raw is _ABSENT:
                continue
            try:
                utc = scalars.parse_instant(raw)
            except scalars.ScalarValueError:
                return {}, (
                    ("TIME_BASIS_UNZONED" if _unzoned(raw) else "TIME_BASIS_INVALID"),
                    f"{at}.instant",
                )
            minutes = (
                _offset_minutes(offset_raw)
                if basis.offset is not None
                else scalars.instant_offset_minutes(raw)
            )
            if minutes is None:
                return {}, ("TIME_BASIS_INVALID", f"{at}.offset")
            out = {self.local_date: (utc + dt.timedelta(minutes=minutes)).date().isoformat()}
            if self.instant is not None:
                out[self.instant] = (
                    utc.strftime("%Y-%m-%dT%H:%M:%S")
                    + (f".{utc.microsecond:06d}" if utc.microsecond else "")
                    + "Z"
                )
            if self.offset is not None:
                sign = "-" if minutes < 0 else "+"
                out[self.offset] = f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"
            return out, None
        return {}, ("TIME_BASIS_ABSENT", "mapping.time.from")


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


def _unzoned(raw: Any) -> bool:
    try:
        return type(raw) is str and dt.datetime.fromisoformat(raw).tzinfo is None
    except ValueError:
        return False


def _offset_minutes(raw: Any) -> int | None:
    try:
        return scalars.offset_minutes(raw)
    except scalars.ScalarValueError:
        return None


def _path(raw: Any, at: str, fmt: str) -> tuple[str, ...]:
    if type(raw) is not str or not raw or len(raw.encode()) > _PATH_BYTES:
        _mapping_invalid(at, "a source path is a non-empty string", expected="string")
    parts = (raw,) if fmt == "csv" else tuple(raw.split("."))
    if not all(parts):
        _mapping_invalid(at, "a dotted source path has an empty segment", expected="a.b.c")
    return parts


def _target(raw: Any, at: str, declared: Mapping[str, Any], used: set[str]) -> str:
    if type(raw) is not str or raw not in declared:
        _mapping_invalid(at, "target is not a declared collection field", allowed=sorted(declared))
    if raw in used:
        _mapping_invalid(at, "target is mapped twice", expected="one source per target")
    used.add(raw)
    return raw


def compile_mapping(raw: Any, manifest: collections.CollectionManifest, fmt: str) -> Plan:
    """Validate a declared mapping against the collection; nothing in it executes."""
    declared = manifest.schema.fields
    if not isinstance(raw, Mapping):
        _mapping_invalid("mapping", "mapping must be an object", expected="object")
    for key in sorted(set(raw) - {"fields", "time", "on_invalid"}):
        _mapping_invalid(
            f"mapping.{key}", "unknown mapping key", allowed=["fields", "on_invalid", "time"]
        )
    fields_raw = raw.get("fields")
    if not isinstance(fields_raw, Mapping) or not fields_raw or len(fields_raw) > MAX_MAPPED_FIELDS:
        _mapping_invalid(
            "mapping.fields", "fields maps 1 to 64 target fields to source paths", expected="object"
        )
    used: set[str] = set()
    fields = []
    for target, spec in fields_raw.items():
        at = f"mapping.fields.{target}"
        if target not in declared:
            _mapping_invalid(
                at, "target is not a declared collection field", allowed=sorted(declared)
            )
        kind = None
        if isinstance(spec, Mapping):
            for key in sorted(set(spec) - {"from", "type"}):
                _mapping_invalid(
                    f"{at}.{key}", "unknown field mapping key", allowed=["from", "type"]
                )
            if "type" in spec:
                if fmt != "csv":
                    _mapping_invalid(
                        f"{at}.type",
                        "only csv text is converted; JSON values keep their type",
                        expected=None,
                    )
                if spec["type"] not in CSV_TYPES:
                    _mapping_invalid(f"{at}.type", "unsupported csv type", allowed=list(CSV_TYPES))
                kind = spec["type"]
            elif fmt == "csv":
                kind = "string"
            path = _path(spec.get("from"), f"{at}.from", fmt)
        else:
            path = _path(spec, at, fmt)
            kind = "string" if fmt == "csv" else None
        fields.append(_Field(_target(target, at, declared, used), path, kind))
    bases: list[_Basis] = []
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
            at = f"mapping.time.from[{index}]"
            if (
                not isinstance(basis, Mapping)
                or not basis
                or not (
                    set(basis) == {"date"} or set(basis) in ({"instant"}, {"instant", "offset"})
                )
            ):
                _mapping_invalid(
                    at,
                    "a time basis is {date} or {instant, optional offset}",
                    allowed=[["date"], ["instant"], ["instant", "offset"]],
                )
            bases.append(
                _Basis(
                    index, **{key: _path(value, f"{at}.{key}", fmt) for key, value in basis.items()}
                )
            )
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
        "fields": {spec.target: {"from": list(spec.path), "type": spec.kind} for spec in fields},
        "time": None
        if not bases
        else {
            "from": [
                {
                    key: list(getattr(basis, key))
                    for key in ("instant", "offset", "date")
                    if getattr(basis, key) is not None
                }
                for basis in bases
            ],
            "instant": instant,
            "offset": offset,
            "local_date": local_date,
        },
        "on_invalid": on_invalid,
        "format": fmt,
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


def _next_batch(reader: _Reader, plan: Plan, pending: list[Row]) -> Batch:
    begin = reader.position()["byte"] if not pending else pending[0].start
    rows: list[tuple[Row, dict[str, Any]]] = []
    rejections: list[tuple[Row, str, str]] = []
    checkpoint = reader.position() if not pending else None
    eof = False
    while len(rows) + len(rejections) < MAX_BATCH_ROWS:
        row = pending.pop() if pending else reader.next()
        if row is None:
            eof = True
            break
        if (rows or rejections) and row.end - begin > MAX_BATCH_BYTES:
            pending.append(row)
            break
        error = row.error
        values = None
        if error is None:
            values, error = plan.apply(row.value)
            row.value = None
        if error is not None and (row.fatal or plan.on_invalid == "stop"):
            return Batch(rows, rejections, (row, *error), checkpoint or reader.position(), 0, False)
        if error is not None:
            rejections.append((row, *error))
        else:
            rows.append((row, values))
        checkpoint = {"row": row.ordinal + 1, "byte": row.end, "state": reader.position()["state"]}
    if eof:
        checkpoint = reader.position()
    return Batch(rows, rejections, None, checkpoint, checkpoint["byte"] - begin, eof)


def iter_batches(
    handle, fmt: str, plan: Plan, checkpoint: Mapping[str, Any] | None = None
) -> Iterator[Batch]:
    """Stream ≤500-row/≤4 MiB batches; memory is one batch and one buffered row."""
    reader = _Reader(handle, fmt, checkpoint or _START)
    pending: list[Row] = []
    while True:
        batch = _next_batch(reader, plan, pending)
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
    return {
        "mode": mode,
        **extra,
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
        "source_bytes": {"consumed": checkpoint["byte"], "total": job.binding["source"]["bytes"]},
        "batches": progress["batches"],
        "window": {"started_at": _iso(job.window_started), "expires_at": _iso(job.window_expires)},
        "next_position": {"row": checkpoint["row"], "byte": checkpoint["byte"]},
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


def _authorize(writer, job: _Job, *, prove: bool) -> Source:
    """Re-resolve the bound principal's authority now; raise _Lost when any part fails.

    The checks are the collection's complete-state mutation rule, a live (never
    extended) session, the bound store and declaration (``_holds``) and release of
    the exact bound source bytes. A host without its own proof of those bytes
    re-hashes them.
    """
    bound = job.binding
    session = bound["principal"]["session"]
    if session is not None and _now() > session["expires_at"]:
        raise _Lost
    operation = writer._fresh_authorization()
    try:
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
        proof = _proofs(writer).get(job.id)
        if proof is None:
            if not prove:
                raise _Lost
            proof = _prove(writer, job, operation)
        if not operation.allows_file(proof.ref, content_sha256=bound["source"]["sha256"]):
            raise _Lost
        proof.guard.recheck(writer.root)
        return proof
    except vault.PathGuardError as error:
        raise _Lost from error
    finally:
        operation.close()


def _proofs(writer) -> dict[str, Source]:
    """This writer handle's proofs of bound source bytes; a new host starts with none."""
    return writer.handle.import_proofs


def _prove(writer, job: _Job, operation) -> Source:
    bound = job.binding["source"]
    try:
        source, sha256, size = resolve_source(writer.root, operation, bound["ref"])
    except collections.CollectionError as error:
        raise _Lost from error
    if (sha256, size) != (bound["sha256"], bound["bytes"]):
        raise _Lost
    _proofs(writer)[job.id] = source
    return source


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _stamp() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _collapse(
    rows: list[tuple[Row, dict[str, Any]]], manifest, source_ref: str
) -> tuple[list[tuple[Row, dict[str, Any]]], int]:
    """Keep each natural key's last occurrence in a batch, by the writer's own identity rule.

    A row the writer would refuse has no identity here, so it neither supersedes
    nor is superseded: the last valid occurrence wins.
    """
    if not manifest.schema.natural_key:
        return rows, 0
    keys = []
    for _, values in rows:
        try:
            keys.append(
                collections.derived_item_key(
                    manifest, records._bulk_row_values(manifest, values, source_ref)
                )
            )
        except collections.CollectionError:
            keys.append(None)
    last = {key: index for index, key in enumerate(keys) if key is not None}
    kept = [
        row
        for index, (row, key) in enumerate(zip(rows, keys, strict=True))
        if key is None or last[key] == index
    ]
    return kept, len(rows) - len(kept)


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
) -> None:
    """Advance the checkpoint, counters and rejections inside the batch transaction.

    A row superseded in this batch, or re-stating what this job wrote in an
    earlier one, counts once as a duplicate, so the counts do not depend on where
    batches end. ``batches`` counts settlements that committed rows.
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
        writer._execute(
            "INSERT INTO import_rejections(job_id,ordinal,byte_offset,code,at) VALUES (?,?,?,?,?)",
            [(job.id, row.ordinal, row.start, code, at) for row, code, at in rejections],
            many=True,
        )
        checkpoint = {**batch.checkpoint, "batch": sequence + 1}
        state, reason = ("complete" if batch.eof else "running"), None
    cursor = writer._execute(
        "UPDATE import_jobs SET state=?,reason=?,checkpoint_json=?,progress_json=?,updated_at=? "
        "WHERE job_id=? AND state='running' AND json_extract(checkpoint_json,'$.batch')=?",
        (state, reason, _json(checkpoint), _json(progress), _stamp(), job.id, sequence),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("import job changed under its batch")


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
    ) -> None:
        self.job, self.batch, self.kept, self.superseded, self.plan = (
            job,
            batch,
            kept,
            superseded,
            plan,
        )
        self.source = (proof.ref, proof.guard)

    def __call__(self, writer, result: Mapping[str, Any]) -> None:
        _authorize(writer, self.job, prove=False)
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
            )


def _transaction(root: Path, writer, work) -> None:
    from .preview import _mutate

    def import_job_settlement():
        with writer.handle.transaction():
            work()

    _mutate(root, import_job_settlement)


def _settle(root: Path, writer, job: _Job, state: str, reason: str, error=None) -> str:
    """Pause or fail a running job outside a batch; a failure records its typed error."""
    if reason == "authority_lost":
        _proofs(writer).pop(job.id, None)

    def write():
        progress = {**job.progress, "error": error} if error is not None else job.progress
        writer._execute(
            "UPDATE import_jobs SET state=?,reason=?,progress_json=?,updated_at=? "
            "WHERE job_id=? AND state='running'",
            (state, reason, _json(progress), _stamp(), job.id),
        )

    _transaction(root, writer, write)
    return "paused"


def _pause(root: Path, writer, job: _Job, reason: str) -> str:
    return _settle(root, writer, job, "partial", reason)


def _authorized_now(writer, job: _Job) -> bool:
    try:
        with _snapshot(writer):
            _authorize(writer, job, prove=False)
        return True
    except _Lost:
        return False


def _step(root: Path, writer, job_id: str) -> str:
    """Run one batch of one job under its bound principal, or pause it honestly."""
    from .preview import _mutate

    job = _load(writer.connection, job_id)
    if job is None or job.state != "running":
        return "idle"
    with principal_module.request_scope(job.principal()):
        if _now() >= job.window_expires:
            return _pause(root, writer, job, "time_cap")
        try:
            with _snapshot(writer) as conn:
                proof = _authorize(writer, job, prove=True)
                generation, head = conn.execute(
                    "SELECT generation,audit_head FROM collections WHERE collection_id=?",
                    (job.collection_id,),
                ).fetchone()
                manifest = writer._collection_manifest(writer._collection_row(job.collection_id))[0]
            plan = compile_mapping(
                job.binding["mapping"]["declared"], manifest, job.binding["mapping"]["format"]
            )
            with _open_source(proof) as handle:
                batch = next(
                    iter_batches(handle, job.binding["mapping"]["format"], plan, job.checkpoint)
                )
        except (_Lost, OSError, vault.PathGuardError):
            return _pause(root, writer, job, "authority_lost")
        sequence = job.checkpoint["batch"]
        kept, superseded = _collapse(batch.rows, manifest, proof.ref)
        try:
            if batch.stop is not None or not kept:

                def settle_alone():
                    _authorize(writer, job, prove=False)
                    if batch.stop is not None:
                        _record(writer, job, batch, fail=batch.stop)
                    else:
                        _record(writer, job, batch, superseded=superseded)

                _transaction(root, writer, settle_alone)
                outcome = "paused" if batch.stop is not None else "batch"
            else:
                _mutate(
                    root,
                    writer.bulk_upsert_records,
                    job.collection_id,
                    rows=[{"item": values} for _, values in kept],
                    why=f"import {job.id[:12]} batch {sequence}",
                    expected_container_hash=tokens.container_hash(
                        job.collection_id, generation, head
                    ),
                    source=proof.ref,
                    on_reject="skip" if plan.on_invalid == "skip" else "abort",
                    request_id=f"import:{job.id}:{sequence}",
                    _import=_Settlement(job, batch, kept, superseded, plan, proof),
                )
                outcome = "batch"
        except _Lost:
            return _pause(root, writer, job, "authority_lost")
        except (collections.CollectionError, vault.PathGuardError):
            if not _authorized_now(writer, job):
                return _pause(root, writer, job, "authority_lost")
            raise
    writer.handle.import_blocked.pop(job.id, None)
    settled = _load(writer.connection, job.id)
    if settled.state == "running" and settled.checkpoint["batch"] == sequence:
        raise RuntimeError("import batch replay did not settle its checkpoint")
    return outcome


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
    transaction; ``_after_error`` settles a batch that raised.
    """
    from .preview import bound_writer

    writer = bound_writer(vault_root)
    if writer is None:
        raise connection.CollectionStoreError(
            "COLLECTION_STORE_PREVIEW_REQUIRED", "import jobs run only on the bound store writer"
        )
    if not vault.STAT_GENERATION_TRUSTED:
        _proofs(writer).clear()
    root = Path(vault_root)
    summary = {"batches": 0, "paused": 0, "errors": 0}
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
        plan = compile_mapping(request.mapping, manifest, request.format)
    cid = row["collection_id"]
    with _snapshot(writer):
        operation = writer._fresh_authorization()
        try:
            operation.require_collection(cid, complete=True)
            source, sha256, size = resolve_source(root, operation, request.source_ref)
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
    identity = _identity(binding)
    with writer.handle.transaction():
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
                    _json({**_START, "batch": 0}),
                    _json(_PROGRESS),
                    now,
                    now + JOB_WINDOW_SECONDS,
                    _stamp(),
                    _stamp(),
                ),
            )
    if latest is None:
        _proofs(writer)[job_id] = source
        return _status(writer, _load(writer.connection, job_id), "start")
    return _status(writer, _load(writer.connection, latest[0]), "start", replayed=True)


def _cancel(writer, collection: str, request: _Request) -> dict[str, Any]:
    with writer.handle.transaction(), writer._authorization(mutation=False):
        job = _visible(writer, collection, request.job_id)
        if job.state == "running" or (job.state == "partial" and job.reason in _RESUMABLE):
            writer._execute(
                "UPDATE import_jobs SET state='partial',reason='cancelled',updated_at=? WHERE job_id=?",
                (_stamp(), job.id),
            )
            job.state, job.reason = "partial", "cancelled"
            writer.handle.import_blocked.pop(job.id, None)
        return _status(writer, job, "cancel")


def _continue(root: Path, writer, collection: str, request: _Request) -> dict[str, Any]:
    """Explicit renewal by the bound principal: re-prove source, lineage and live authority."""
    with writer.read_snapshot():
        job = _visible(writer, collection, request.job_id)
    if job.state == "running":
        return _status(writer, job, "start")
    if not (job.state == "partial" and job.reason in _RESUMABLE):
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
    with writer.handle.transaction():
        now = _now()
        writer._execute(
            "UPDATE import_jobs SET binding_json=?,state='running',reason=NULL,window_started=?,window_expires=?,"
            "updated_at=? WHERE job_id=? AND state='partial'",
            (_json(job.binding), now, now + JOB_WINDOW_SECONDS, _stamp(), job.id),
        )
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

    def add(self, row: Row) -> None:
        self.rows += 1
        if row.error is not None:
            if len(self.errors) < _LISTED:
                self.errors.append({"row": row.ordinal, "byte": row.start, "code": row.error[0]})
            return
        self.observe(row.value)
        if self.plan is None:
            self.valid += 1
            return
        for spec in self.plan.fields:
            if _lookup(row.value, spec.path) is not _ABSENT:
                self.present.add(spec.target)
        if self.plan.bases:
            _, error = self.plan.time(row.value)
            if error is not None and len(self.flagged) < 2 * _LISTED:
                self.flagged.append({"row": row.ordinal, "code": error[0], "at": error[1]})
            elif error is None:
                basis = next(basis for basis in self.plan.bases if self._uses(basis, row.value))
                name = "date" if basis.date else "instant+offset" if basis.offset else "instant"
                self.bases[name] = self.bases.get(name, 0) + 1
        values, error = self.plan.apply(row.value)
        if error is not None:
            return
        self.valid += 1
        for target, value in values.items():
            try:
                collections.validate_field_value(target, value, self.manifest.schema.fields[target])
            except collections.CollectionError:
                self.conflicts.setdefault(target, set()).add(_json_type(value))

    @staticmethod
    def _uses(basis: _Basis, value: Mapping[str, Any]) -> bool:
        paths = [path for path in (basis.instant, basis.offset, basis.date) if path is not None]
        return all(_lookup(value, path) not in (_ABSENT, None) for path in paths)

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
            if spec.target not in self.present and self.valid:
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


def _preview(root: Path, writer, collection: str, request: _Request) -> dict[str, Any]:
    with writer.read_snapshot():
        row, manifest, _ = writer._collection(collection, facade_profile="records")
        source, _, size = resolve_source(root, writer._operation, request.source_ref)
        plan, findings = None, []
        if request.mapping is not None:
            try:
                plan = compile_mapping(request.mapping, manifest, request.format)
            except collections.CollectionError as error:
                findings.append({"code": error.code, **error.details})
        sample = _Sample(plan, manifest)
        try:
            with _open_source(source) as handle:
                reader, fatal = _Reader(handle, request.format, _START), False
                while sample.rows < PREVIEW_ROWS and (parsed := reader.next()) is not None:
                    sample.add(parsed)
                    if parsed.fatal:
                        fatal = True
                        break
                complete = not fatal and reader.at_end()
        except (OSError, vault.PathGuardError):
            _source_not_found()
        encoding = writer.connection.execute(
            "SELECT encoding FROM collections WHERE collection_id=?", (row["collection_id"],)
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
    return {
        "mode": "preview",
        "collection_id": row["collection_id"],
        "format": request.format,
        "source": {"ref": source.ref, "bytes": size},
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
