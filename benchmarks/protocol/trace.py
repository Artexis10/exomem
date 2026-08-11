"""Append-only, typed per-case JSONL traces for report regeneration."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path

from .models import TraceRecord, TraceRecordV2
from pydantic import TypeAdapter

_TRACE_RECORD = TypeAdapter(TraceRecord)
_TRACE_RECORD_V2 = TypeAdapter(TraceRecordV2)


class TraceError(ValueError):
    pass


def _trace_path(run_dir: Path | str, case_id: str) -> Path:
    return Path(run_dir) / "traces" / f"{case_id}.jsonl"


class CaseTraceWriter:
    def __init__(self, run_dir: Path | str, case_id: str, *, schema_version: int = 1) -> None:
        self.path = _trace_path(run_dir, case_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if schema_version not in {1, 2}:
            raise TraceError("unknown trace schema version")
        self.schema_version = schema_version

    def append(self, entry: TraceRecord | Mapping[str, object]) -> TraceRecord:
        if self.path.exists() and self.path.stat().st_size:
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
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(adapter.dump_json(typed).decode("utf-8") + "\n")
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
