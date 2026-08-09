"""Twelve-key equivalence comparison with explicit null-is-never-equal semantics."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from membench.environment import BLOCKING, REPORTED

_KEYS = (
    "dataset_identity", "case_set", "session_normalization", "namespace", "ingestion_payloads",
    "readiness", "exact_query", "top_k", "retrieved_ids", "retrieved_text", "packed_context",
    "answer_judge_prompt_model_config",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _value(run: dict[str, Any], key: str) -> Any:
    aliases = {
        "dataset_identity": "dataset_identity", "case_set": "case_set", "session_normalization": "session_normalization",
        "namespace": "namespace", "ingestion_payloads": "ingestion_payloads", "readiness": "readiness",
        "exact_query": "exact_query", "top_k": "top_k", "retrieved_ids": "retrieved_ids",
        "retrieved_text": "retrieved_text", "packed_context": "packed_context",
        "answer_judge_prompt_model_config": "answer_judge_prompt_model_config",
    }
    return run.get(aliases[key])


@dataclass(frozen=True)
class Difference:
    case_id: str
    field: str
    expected: str | None
    actual: str | None
    classification: str


@dataclass(frozen=True)
class Comparison:
    diffs: tuple[Difference, ...]
    blocking: bool


def _load(run_dir: Path | str) -> dict[str, Any]:
    path = Path(run_dir) / "equivalence.json"
    return json.loads(path.read_text(encoding="utf-8"))


def compare_runs(left: Path | str, right: Path | str, *, mode: str, out: Path | str) -> Comparison:
    if mode not in {BLOCKING, REPORTED}:
        raise ValueError("mode must be blocking or report")
    first, second = _load(left), _load(right)
    case_id = str(first.get("case_id", "run"))
    diffs: list[Difference] = []
    for key in _KEYS:
        expected, actual = _value(first, key), _value(second, key)
        equal = expected is not None and actual is not None and _canonical(expected) == _canonical(actual)
        if not equal:
            diffs.append(Difference(case_id, key, None if expected is None else _canonical(expected), None if actual is None else _canonical(actual), mode))
    root = Path(out)
    root.mkdir(parents=True, exist_ok=True)
    artifact = {"schema_version": 1, "kind": "equivalence-diff.v1", "mode": mode, "diffs": [diff.__dict__ for diff in diffs]}
    (root / "equivalence-diff.v1.json").write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = ["# Equivalence diff", "", f"Mode: `{mode}`", "", "| Case | Key | Classification |", "|---|---|---|"]
    lines.extend(f"| {diff.case_id} | {diff.field} | {diff.classification.upper()} |" for diff in diffs)
    (root / "equivalence-diff.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return Comparison(tuple(diffs), bool(diffs and mode == BLOCKING))
