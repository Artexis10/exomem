"""Expired exceptions never erase a difference; they only weaken comparison.

The register owns the vocabulary of allowed weaker predicates. There is
deliberately no "skip", "equal", or "always-equal": an entry must name a
predicate that is strictly weaker than equality but still checks something, so
a registered exception can fail. An unknown name is refused rather than treated
as a pass, and ``expires_at`` is mandatory so an exception cannot outlive its
evidence. ``active()`` requires the caller's date — library code must never
reach for ``date.today()`` and silently re-date an audit decision.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from protocol.models import EquivalenceException as ProtocolEquivalenceException

_REQUIRED_FIELDS = {"case_id", "field", "compare_as", "evidence", "approver", "expires_at"}
_FORBIDDEN_PREDICATES = {"skip", "equal", "always-equal", "ignore"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _same_shape(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, (list, dict, str)):
        return len(left) == len(right)
    return True


#: Every allowed weaker predicate, named by a register entry's ``compare_as``.
WEAKER_PREDICATES: dict[str, Callable[[Any, Any], bool]] = {
    "case-insensitive-text": lambda left, right: isinstance(left, str) and isinstance(right, str) and left.casefold() == right.casefold(),
    "set-membership": lambda left, right: isinstance(left, list) and isinstance(right, list) and {_canonical(item) for item in left} == {_canonical(item) for item in right},
    "sha256-prefix-8": lambda left, right: isinstance(left, list) and isinstance(right, list) and sorted(str(item)[:8] for item in left) == sorted(str(item)[:8] for item in right),
    "numeric-within-1": lambda left, right: isinstance(left, (int, float)) and isinstance(right, (int, float)) and abs(left - right) <= 1,
    "same-shape": _same_shape,
}


@dataclass(frozen=True)
class EquivalenceException:
    case_id: str
    field: str
    compare_as: str
    evidence: str
    approver: str
    expires_at: str

    def active(self, today: dt.date) -> bool:
        if not isinstance(today, dt.date):
            raise TypeError("active() requires the caller's date; library code must not assume today")
        return dt.date.fromisoformat(self.expires_at) >= today


def load_exceptions(path: Path | str) -> list[EquivalenceException]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    if not isinstance(payload, list):
        raise ValueError("exceptions register must be a list")
    parsed = []
    for item in payload:
        if not isinstance(item, dict) or set(item) != _REQUIRED_FIELDS:
            raise ValueError("exception entries require case_id, field, compare_as, evidence, approver, expiry")
        if item["compare_as"] in _FORBIDDEN_PREDICATES:
            raise ValueError("exceptions must use a weaker compare_as predicate, never skip")
        if item["compare_as"] not in WEAKER_PREDICATES:
            raise ValueError(
                f"unknown compare_as predicate {item['compare_as']!r}; allowed: {sorted(WEAKER_PREDICATES)}"
            )
        ProtocolEquivalenceException.model_validate(item)
        parsed.append(EquivalenceException(**item))
    return parsed
