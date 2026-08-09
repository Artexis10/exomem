"""Scope-aware leakage scans at provider transport boundaries."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from .models import CaseGold

_STRUCTURAL_KEY = re.compile(r"(answer|gold|label|ground_truth|expected|evidence|has_answer)", re.I)
_EVIDENCE_MARKED = re.compile(r"\banswer_[A-Za-z0-9]", re.I)
_LABEL_LITERAL = ("_abs", "has_answer", "answer_session_ids")
# LongMemEval's closed vocabulary is duplicated at the provider-neutral boundary
# so ``python -m benchmarks.protocol.cli`` has no benchmark-package path dependency.
QUESTION_TYPES = (
    "single-session-user", "single-session-assistant", "single-session-preference",
    "multi-session", "temporal-reasoning", "knowledge-update",
)


@dataclass(frozen=True)
class LeakageFinding:
    scope: Literal["ingest", "search", "artifact"]
    detector: str
    location: str
    severity: Literal["case-invalidating", "advisory"]
    note: str


def _walk(payload: object, location: str = "$") -> Iterator[tuple[str, object, bool]]:
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            key_text = str(key)
            yield f"{location}.{key_text}", key_text, True
            yield from _walk(value, f"{location}.{key_text}")
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        for index, value in enumerate(payload):
            yield from _walk(value, f"{location}[{index}]")
    else:
        yield location, payload, False


def _tokens(text: str) -> list[str]:
    return re.findall(r"[\w'-]+", text.casefold())


def _shingles(text: str, size: int = 4) -> set[str]:
    tokens = _tokens(text)
    return {" ".join(tokens[index : index + size]) for index in range(max(0, len(tokens) - size + 1))}


def _contains_boundary(text: str, token: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(token)}(?!\w)", text, re.IGNORECASE))


def _raw_id_found(text: str, raw_ids: Iterable[str]) -> bool:
    return bool(_EVIDENCE_MARKED.search(text)) or any(raw_id and raw_id in text for raw_id in raw_ids)


def _finding(result: list[LeakageFinding], scope: Literal["ingest", "search", "artifact"], detector: str, location: str, severity: Literal["case-invalidating", "advisory"], note: str) -> None:
    result.append(LeakageFinding(scope, detector, location, severity, note))


def scan_ingest(
    content_fields: object,
    harness_fields: object,
    gold: CaseGold,
    *,
    raw_upstream_session_ids: Iterable[str] = (),
) -> tuple[LeakageFinding, ...]:
    """Strictly scan harness-owned fields while allowing dataset evidence content."""

    result: list[LeakageFinding] = []
    raw_ids = {*gold.answer_session_ids, *raw_upstream_session_ids}
    question_shingles = _shingles(gold.question)
    for location, value, is_key in _walk(content_fields, "$.content"):
        if is_key:
            continue
        text = str(value)
        if _raw_id_found(text, raw_ids):
            _finding(result, "ingest", "raw-upstream-id", location, "case-invalidating", "evidence-marked or raw upstream session id in content")
        if question_shingles & _shingles(text):
            _finding(result, "ingest", "question-text", location, "case-invalidating", "four-token question shingle in content")
    label_tokens = {*QUESTION_TYPES, *_LABEL_LITERAL}
    gold_shingles = _shingles(gold.answer)
    for location, value, is_key in _walk(harness_fields, "$.harness"):
        text = str(value)
        if is_key and _STRUCTURAL_KEY.search(text):
            _finding(result, "ingest", "structural-key", location, "case-invalidating", "sensitive structural key")
        if is_key:
            continue
        if gold.answer and _contains_boundary(text, gold.answer):
            _finding(result, "ingest", "gold-text", location, "case-invalidating", "exact gold answer in harness field")
        if gold_shingles and gold_shingles & _shingles(text):
            _finding(result, "ingest", "gold-shingle", location, "case-invalidating", "four-token gold shingle in harness field")
        if any(token and _contains_boundary(text, token) for token in label_tokens):
            _finding(result, "ingest", "label-token", location, "case-invalidating", "gold-bearing label token in harness field")
        if _raw_id_found(text, raw_ids):
            _finding(result, "ingest", "raw-upstream-id", location, "case-invalidating", "evidence-marked or raw upstream session id in harness field")
    return tuple(result)


def scan_search(payload: object, gold: CaseGold) -> tuple[LeakageFinding, ...]:
    """Advisory scan: search may contain the question, never the gold answer."""

    result: list[LeakageFinding] = []
    gold_shingles = _shingles(gold.answer)
    label_tokens = {gold.question_type, *_LABEL_LITERAL}
    for location, value, is_key in _walk(payload):
        text = str(value)
        if is_key and _STRUCTURAL_KEY.search(text):
            _finding(result, "search", "structural-key", location, "advisory", "sensitive structural key")
        if is_key:
            continue
        if text.casefold() == gold.question.casefold():
            continue
        if gold.answer and _contains_boundary(text, gold.answer):
            _finding(result, "search", "gold-text", location, "advisory", "exact gold answer")
        if gold_shingles and gold_shingles & _shingles(text):
            _finding(result, "search", "gold-shingle", location, "advisory", "four-token gold shingle")
        if any(token and _contains_boundary(text, token) for token in label_tokens):
            _finding(result, "search", "label-token", location, "advisory", "gold-bearing label token")
        if _raw_id_found(text, gold.answer_session_ids):
            _finding(result, "search", "raw-upstream-id", location, "advisory", "raw upstream session id")
    return tuple(result)


def scan_artifact(payload: object, gold: CaseGold) -> tuple[LeakageFinding, ...]:
    """Artifacts may retain gold, with an explicit provenance-note finding."""

    del payload, gold
    return (LeakageFinding("artifact", "artifact-provenance", "$", "advisory", "offline artifact scope permits gold-bearing records"),)
