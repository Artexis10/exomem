"""Scope-aware inspection of payloads captured at provider transport boundaries."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from .models import CaseGold

_STRUCTURAL_KEY = re.compile(r"(answer|gold|label|ground_truth|expected|evidence|has_answer)", re.I)
_LABEL_LITERAL = ("_abs", "has_answer", "answer_session_ids")


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


def _findings(payload: object, gold: CaseGold, *, scope: Literal["ingest", "search"]) -> tuple[LeakageFinding, ...]:
    strict = scope == "ingest"
    severity: Literal["case-invalidating", "advisory"] = "case-invalidating" if strict else "advisory"
    result: list[LeakageFinding] = []
    gold_text = gold.answer.casefold()
    gold_shingles = {
        " ".join(words[index : index + 4])
        for words in (_tokens(gold.answer),)
        for index in range(max(0, len(words) - 3))
    }
    label_tokens = {gold.question_type.casefold(), *_LABEL_LITERAL}
    raw_ids = set(gold.answer_session_ids)
    for location, value, is_key in _walk(payload):
        text = str(value)
        folded = text.casefold()
        is_question_text = scope == "search" and folded == gold.question.casefold()
        if is_key and _STRUCTURAL_KEY.search(text):
            result.append(LeakageFinding(scope, "structural-key", location, severity, "sensitive structural key"))
        if is_key:
            continue
        if not is_question_text and gold_text and gold_text in folded:
            result.append(LeakageFinding(scope, "gold-text", location, severity, "exact gold answer"))
        if not is_question_text and any(shingle in " ".join(_tokens(text)) for shingle in gold_shingles):
            result.append(LeakageFinding(scope, "gold-shingle", location, severity, "four-token gold shingle"))
        if not is_question_text and any(token and token in folded for token in label_tokens):
            result.append(LeakageFinding(scope, "label-token", location, severity, "gold-bearing label token"))
        if any(raw_id and raw_id in text for raw_id in raw_ids) or re.search(r"\banswer[\w-]*", text, re.I):
            result.append(LeakageFinding(scope, "raw-upstream-id", location, severity, "raw upstream session id"))
        if strict and gold.question and gold.question.casefold() in folded:
            result.append(LeakageFinding(scope, "question-text", location, severity, "future question text"))
    return tuple(result)


def scan_ingest(payload: object, gold: CaseGold) -> tuple[LeakageFinding, ...]:
    """Strict scan: every returned finding invalidates the case."""

    return _findings(payload, gold, scope="ingest")


def scan_search(payload: object, gold: CaseGold) -> tuple[LeakageFinding, ...]:
    """Advisory scan: question text is legitimate, gold disclosure is not."""

    return _findings(payload, gold, scope="search")


def scan_artifact(payload: object, gold: CaseGold) -> tuple[LeakageFinding, ...]:
    """Artifacts retain gold for offline judging; their provenance carries the boundary."""

    del payload, gold
    return ()
