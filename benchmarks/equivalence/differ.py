"""Twelve-key equivalence comparison with explicit, documented semantics.

Each key carries its own normalizer and its own classification tier. Identity
and configuration keys BLOCK, because a difference there means the two runs
were not the same experiment; content keys are REPORTED, because they are the
measured outcome the comparison exists to observe.

===============================  ==========  ==========================================
Key                              Tier        Normalizer
===============================  ==========  ==========================================
dataset_identity                 BLOCKING    exact structural equality
case_set                         BLOCKING    order-insensitive set of normalized ids
session_normalization            BLOCKING    whitespace-normalized text
namespace                        BLOCKING    derivation-pattern check
ingestion_payloads               BLOCKING    sorted sha256 list
readiness                        BLOCKING    exact structural equality
exact_query                      BLOCKING    whitespace-normalized text
top_k                            BLOCKING    exact equality
answer_judge_prompt_model_config BLOCKING    exact structural equality
retrieved_ids                    REPORTED    order-insensitive set
retrieved_text                   REPORTED    per-item whitespace-normalized list
packed_context                   REPORTED    whitespace-normalized text
===============================  ==========  ==========================================

``null`` never equals anything, including another ``null``: an absent value is
a mismatch that demands an explanation, never a silent pass.

An entry in the exceptions register never skips a comparison. It supplies a
strictly WEAKER predicate; when that predicate holds the difference is still
recorded, but as REPORTED and without an outstanding explanation. An expired
entry supplies nothing, so its difference returns to unexplained.

The input is ``equivalence.json`` in each run directory: either a single case
object (hand-built fixtures) or ``{"cases": [...]}`` as the runner emits it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from membench.environment import BLOCKING, REPORTED
from protocol.models import EquivalenceDiff

from .exceptions import WEAKER_PREDICATES, EquivalenceException, load_exceptions

EQUIVALENCE_KEYS = (
    "dataset_identity", "case_set", "session_normalization", "namespace", "ingestion_payloads",
    "readiness", "exact_query", "top_k", "retrieved_ids", "retrieved_text", "packed_context",
    "answer_judge_prompt_model_config",
)
_BLOCKING_KEYS = frozenset({
    "dataset_identity", "case_set", "session_normalization", "namespace", "ingestion_payloads",
    "readiness", "exact_query", "top_k", "answer_judge_prompt_model_config",
})
KEY_CLASSIFICATION: dict[str, str] = {
    key: BLOCKING if key in _BLOCKING_KEYS else REPORTED for key in EQUIVALENCE_KEYS
}


def _text(value: Any) -> Any:
    return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else value


def _set(value: Any) -> Any:
    return sorted({_canonical(_text(item)) for item in value}) if isinstance(value, list) else value


def _namespace(value: Any) -> Any:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9-]{1,100}", value):
        return {"invalid_namespace": value}
    return value


def _payloads(value: Any) -> Any:
    return sorted(value) if isinstance(value, list) and all(isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item) for item in value) else value


_NORMALIZERS: dict[str, Callable[[Any], Any]] = {
    "dataset_identity": lambda value: value,
    "case_set": _set,
    "session_normalization": _text,
    "namespace": _namespace,
    "ingestion_payloads": _payloads,
    "readiness": lambda value: value,
    "exact_query": _text,
    "top_k": lambda value: value,
    "retrieved_ids": _set,
    "retrieved_text": lambda value: [_text(item) for item in value] if isinstance(value, list) else _text(value),
    "packed_context": _text,
    "answer_judge_prompt_model_config": lambda value: value,
}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Difference:
    case_id: str
    field: str
    expected: str | None
    actual: str | None
    classification: str
    explanation_required: bool
    compare_as: str | None = None


@dataclass(frozen=True)
class Comparison:
    diffs: tuple[Difference, ...]
    blocking: bool


def _load(run_dir: Path | str) -> dict[str, dict[str, Any]]:
    payload = json.loads((Path(run_dir) / "equivalence.json").read_text(encoding="utf-8"))
    cases = payload["cases"] if isinstance(payload, dict) and "cases" in payload else [payload]
    return {str(case.get("case_id", "run")): case for case in cases}


def _exception(exceptions: list[EquivalenceException], case_id: str, field: str, today) -> EquivalenceException | None:
    return next((item for item in exceptions if item.case_id == case_id and item.field == field and item.active(today)), None)


def compare_runs(left: Path | str, right: Path | str, *, mode: str, out: Path | str, exceptions_path: Path | str | None = None, today=None) -> Comparison:
    """Write a schema-shaped artifact; report mode records mismatches but does not block."""

    if mode == "report":
        mode = REPORTED
    if mode not in {BLOCKING, REPORTED}:
        raise ValueError("mode must be blocking or report")
    first, second = _load(left), _load(right)
    exceptions = load_exceptions(exceptions_path) if exceptions_path else []
    if exceptions and today is None:
        raise ValueError("today is required when applying equivalence exceptions")
    diffs: list[Difference] = []
    for case_id in sorted({*first, *second}):
        left_case, right_case = first.get(case_id, {}), second.get(case_id, {})
        for key in EQUIVALENCE_KEYS:
            expected, actual = left_case.get(key), right_case.get(key)
            # Null never equals anything, including null; absence demands an explanation.
            equal = expected is not None and actual is not None and _NORMALIZERS[key](expected) == _NORMALIZERS[key](actual)
            if equal:
                continue
            exemption = _exception(exceptions, case_id, key, today)
            rescued = bool(exemption) and WEAKER_PREDICATES[exemption.compare_as](expected, actual)
            classification = REPORTED if rescued else KEY_CLASSIFICATION[key]
            diffs.append(Difference(
                case_id, key,
                None if expected is None else _canonical(_NORMALIZERS[key](expected)),
                None if actual is None else _canonical(_NORMALIZERS[key](actual)),
                classification, not rescued, exemption.compare_as if exemption else None,
            ))
    root = Path(out)
    root.mkdir(parents=True, exist_ok=True)
    artifact = EquivalenceDiff(mode=mode, diffs=[
        {
            "case_id": diff.case_id, "field": diff.field, "expected": diff.expected, "actual": diff.actual,
            "equal": False, "classification": diff.classification,
            "explanation_required": diff.explanation_required, "compare_as": diff.compare_as,
        }
        for diff in diffs
    ]).model_dump()
    (root / "equivalence-diff.v1.json").write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = ["# Equivalence diff", "", f"Mode: `{mode}`", "", "| Case | Key | Classification | Explanation |", "|---|---|---|---|"]
    lines.extend(
        f"| {diff.case_id} | {diff.field} | {diff.classification.upper()} | "
        f"{'required' if diff.explanation_required else 'approved weaker predicate ' + str(diff.compare_as)} |"
        for diff in diffs
    )
    (root / "equivalence-diff.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return Comparison(tuple(diffs), any(diff.classification == BLOCKING for diff in diffs) and mode == BLOCKING)
