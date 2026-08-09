"""Append-only, typed per-case JSONL traces for report regeneration."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

from .models import TraceRecord
from pydantic import TypeAdapter

_TRACE_RECORD = TypeAdapter(TraceRecord)


def _trace_path(run_dir: Path | str, case_id: str) -> Path:
    return Path(run_dir) / "traces" / f"{case_id}.jsonl"


class CaseTraceWriter:
    def __init__(self, run_dir: Path | str, case_id: str) -> None:
        self.path = _trace_path(run_dir, case_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, entry: TraceRecord | Mapping[str, object]) -> TraceRecord:
        typed = _TRACE_RECORD.validate_python(entry)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(_TRACE_RECORD.dump_json(typed).decode("utf-8") + "\n")
        return typed


class CaseTraceReader:
    def __init__(self, run_dir: Path | str, case_id: str) -> None:
        self.path = _trace_path(run_dir, case_id)

    def __iter__(self) -> Iterator[TraceRecord]:
        if not self.path.exists():
            return iter(())
        return (_TRACE_RECORD.validate_json(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line)
