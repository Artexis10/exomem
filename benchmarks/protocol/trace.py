"""Append-only, per-case JSONL traces for offline report regeneration."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any


def _trace_path(run_dir: Path | str, case_id: str) -> Path:
    return Path(run_dir) / "traces" / f"{case_id}.jsonl"


class CaseTraceWriter:
    def __init__(self, run_dir: Path | str, case_id: str) -> None:
        self.path = _trace_path(run_dir, case_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, entry: Mapping[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(dict(entry), ensure_ascii=False, sort_keys=True) + "\n")


class CaseTraceReader:
    def __init__(self, run_dir: Path | str, case_id: str) -> None:
        self.path = _trace_path(run_dir, case_id)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return iter(())
        return (json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line)
