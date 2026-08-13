"""Append-only, typed per-case JSONL traces for report regeneration."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from .custody import CustodyError, HeldDirectory, hold_directory
from .models import TraceRecord, TraceRecordV2
from pydantic import TypeAdapter, ValidationError

_TRACE_RECORD = TypeAdapter(TraceRecord)
_TRACE_RECORD_V2 = TypeAdapter(TraceRecordV2)


class TraceError(ValueError):
    pass


MAX_TRACE_BYTES = 1_048_576
MAX_TRACE_INVENTORY_ENTRIES = 10_000
_TRACE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.jsonl\Z")


@dataclass(frozen=True)
class LoadedTrace:
    """One bounded, schema-validated v2 trace bound to its filename identity."""

    session_id: str
    records: tuple[TraceRecordV2, ...]

    @property
    def cleanup(self) -> TraceRecordV2:
        return next(record for record in self.records if record.record == "cleanup")


def _trace_path(run_dir: Path | str, case_id: str) -> Path:
    return Path(run_dir) / "traces" / f"{case_id}.jsonl"


class CaseTraceWriter:
    def __init__(self, run_dir: Path | str, case_id: str, *, schema_version: int = 1) -> None:
        self.path = _trace_path(run_dir, case_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if schema_version not in {1, 2}:
            raise TraceError("unknown trace schema version")
        self.schema_version = schema_version
        self._v2_traces: HeldDirectory | None = None
        self._v2_identity: tuple[int, int] | None = None
        if schema_version == 2:
            try:
                self._v2_traces = hold_directory(
                    self.path.parent, logical_ref=Path("traces"),
                )
            except CustodyError as exc:
                raise TraceError("v2 trace directory cannot be held safely") from exc

    def close(self) -> None:
        if self._v2_traces is not None:
            self._v2_traces.close()
            self._v2_traces = None

    def __del__(self) -> None:
        self.close()

    def _append_v2(self, text: bytes) -> None:
        traces = self._v2_traces
        if traces is None:
            raise TraceError("v2 trace writer is closed")
        name = self.path.name
        try:
            traces.assert_bound()
            try:
                named = os.stat(name, dir_fd=traces.fd, follow_symlinks=False)
            except FileNotFoundError:
                named = None
            if named is not None:
                if not stat.S_ISREG(named.st_mode):
                    raise TraceError("v2 trace leaf is not regular")
                if named.st_size + len(text) > MAX_TRACE_BYTES:
                    raise TraceError("v2 trace exceeds bounded write limit")
                if self._v2_identity is not None and self._v2_identity != (named.st_dev, named.st_ino):
                    raise TraceError("v2 trace leaf binding changed")
                flags = os.O_WRONLY | os.O_APPEND
            else:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            descriptor = os.open(
                name,
                flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=traces.fd,
            )
        except TraceError:
            raise
        except (CustodyError, OSError) as exc:
            raise TraceError("v2 trace leaf cannot be opened safely") from exc
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise TraceError("v2 trace leaf is not regular")
            if opened.st_size + len(text) > MAX_TRACE_BYTES:
                raise TraceError("v2 trace exceeds bounded write limit")
            named = os.stat(name, dir_fd=traces.fd, follow_symlinks=False)
            if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
                raise TraceError("v2 trace leaf binding changed")
            identity = (opened.st_dev, opened.st_ino)
            if self._v2_identity is not None and self._v2_identity != identity:
                raise TraceError("v2 trace leaf binding changed")
            view = memoryview(text)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise TraceError("v2 trace append did not progress")
                view = view[written:]
            os.fsync(descriptor)
            named = os.stat(name, dir_fd=traces.fd, follow_symlinks=False)
            if (named.st_dev, named.st_ino) != identity:
                raise TraceError("v2 trace leaf binding changed")
            self._v2_identity = identity
        except OSError as exc:
            raise TraceError("v2 trace append failed") from exc
        finally:
            os.close(descriptor)
        try:
            traces.assert_bound()
        except CustodyError as exc:
            raise TraceError("v2 trace directory binding changed") from exc

    def append(self, entry: TraceRecord | Mapping[str, object]) -> TraceRecord:
        if self.schema_version == 2:
            traces = self._v2_traces
            if traces is None:
                raise TraceError("v2 trace writer is closed")
            try:
                existing_bytes = traces.read_regular_bounded(self.path.name, max_bytes=MAX_TRACE_BYTES)
            except CustodyError:
                existing_bytes = b""
            if existing_bytes:
                try:
                    existing = json.loads(existing_bytes.splitlines()[0])
                except (json.JSONDecodeError, UnicodeDecodeError, IndexError) as exc:
                    raise TraceError("v2 trace header is invalid") from exc
                existing_version = int(existing.get("schema_version", 1))
                if existing_version != self.schema_version:
                    raise TraceError("mixed trace schema versions are invalid")
        elif self.path.exists() and self.path.stat().st_size:
            existing = json.loads(self.path.read_text(encoding="utf-8").splitlines()[0])
            existing_version = int(existing.get("schema_version", 1))
            if existing_version != self.schema_version:
                raise TraceError("mixed trace schema versions are invalid")
        if self.schema_version == 2:
            payload = dict(entry)
            payload.setdefault("protocol_version", "1.0.0")
            payload.setdefault("schema_version", 2)
            typed = _TRACE_RECORD_V2.validate_python(payload)
            adapter = _TRACE_RECORD_V2
        else:
            typed = _TRACE_RECORD.validate_python(entry)
            adapter = _TRACE_RECORD
        serialized = adapter.dump_json(typed)
        if self.schema_version == 2:
            self._append_v2(serialized + b"\n")
        else:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(serialized.decode("utf-8") + "\n")
        return typed


class CaseTraceReader:
    def __init__(self, run_dir: Path | str, case_id: str) -> None:
        self.path = _trace_path(run_dir, case_id)

    def __iter__(self) -> Iterator[TraceRecord]:
        if not self.path.exists():
            return iter(())
        lines = [line for line in self.path.read_text(encoding="utf-8").splitlines() if line]
        versions = {json.loads(line).get("schema_version", 1) for line in lines}
        if len(versions) > 1:
            raise TraceError("mixed trace schema versions are invalid")
        adapter = _TRACE_RECORD_V2 if versions == {2} else _TRACE_RECORD
        return (adapter.validate_json(line) for line in lines)


def _load_trace_file(
    traces: HeldDirectory,
    name: str,
    *,
    required_schema_version: int,
    max_trace_bytes: int,
) -> tuple[LoadedTrace, tuple[int, int]]:
    if _TRACE_NAME.fullmatch(name) is None:
        raise TraceError("trace inventory contains an unsafe or unknown filename")
    session_id = name.removesuffix(".jsonl")
    try:
        status = os.stat(name, dir_fd=traces.fd, follow_symlinks=False)
    except OSError as exc:
        raise TraceError("trace inventory entry is unavailable") from exc
    if not stat.S_ISREG(status.st_mode):
        raise TraceError("trace inventory entry is not a regular file")
    if status.st_size == 0:
        raise TraceError("trace file is empty")
    if status.st_size > max_trace_bytes:
        raise TraceError("trace file exceeds bounded read limit")
    try:
        payload, identity = traces.read_regular_bounded(
            name, max_bytes=max_trace_bytes, with_identity=True,
        )
    except CustodyError as exc:
        raise TraceError("trace file cannot be read safely") from exc
    if identity != (status.st_dev, status.st_ino):
        raise TraceError("trace inventory entry binding changed during read")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TraceError("trace file is not UTF-8") from exc
    lines = text.splitlines()
    if not lines or any(not line for line in lines):
        raise TraceError("trace file is empty or contains a blank row")
    if required_schema_version != 2:
        raise TraceError("unsupported required trace schema version")
    try:
        records = tuple(_TRACE_RECORD_V2.validate_json(line) for line in lines)
    except (ValidationError, ValueError, TypeError) as exc:
        raise TraceError("trace file contains an invalid or mixed-version row") from exc
    cleanups = tuple(record for record in records if record.record == "cleanup")
    if len(cleanups) != 1:
        raise TraceError("v2 trace must contain exactly one cleanup row")
    cleanup = cleanups[0]
    if getattr(cleanup, "session_id", None) != session_id:
        raise TraceError("trace filename and cleanup session identity disagree")
    return LoadedTrace(session_id=session_id, records=records), identity


def load_trace_inventory(
    run_dir: Path | str,
    *,
    required_schema_version: int = 2,
    max_trace_bytes: int = MAX_TRACE_BYTES,
) -> tuple[LoadedTrace, ...]:
    """Inventory every trace entry through a fresh bounded no-follow traversal."""

    if max_trace_bytes < 1:
        raise TraceError("trace bounded read limit is invalid")
    run: HeldDirectory | None = None
    traces: HeldDirectory | None = None
    try:
        run = hold_directory(Path(run_dir), logical_ref=Path("."))
        traces = run.open_dir("traces", logical_ref=Path("traces"))
        try:
            with os.scandir(traces.fd) as iterator:
                names: list[str] = []
                for entry in iterator:
                    if len(names) >= MAX_TRACE_INVENTORY_ENTRIES:
                        raise TraceError("trace inventory entry limit exceeded")
                    names.append(entry.name)
                names.sort()
        except OSError as exc:
            raise TraceError("trace directory cannot be inventoried") from exc
        loaded_with_identity = tuple(
            _load_trace_file(
                traces,
                name,
                required_schema_version=required_schema_version,
                max_trace_bytes=max_trace_bytes,
            )
            for name in names
        )
        for name, (_loaded, identity) in zip(names, loaded_with_identity, strict=True):
            try:
                status = os.stat(name, dir_fd=traces.fd, follow_symlinks=False)
            except OSError as exc:
                raise TraceError("trace inventory entry is unavailable") from exc
            if not stat.S_ISREG(status.st_mode) or identity != (status.st_dev, status.st_ino):
                raise TraceError("trace inventory entry binding changed during inventory")
        traces.assert_bound()
        run.assert_bound()
        return tuple(loaded for loaded, _identity in loaded_with_identity)
    except CustodyError as exc:
        raise TraceError("trace inventory cannot be traversed safely") from exc
    finally:
        if traces is not None:
            traces.close()
        if run is not None:
            run.close()
