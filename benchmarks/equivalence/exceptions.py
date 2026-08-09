"""Expired exceptions never erase a difference; they only weaken comparison."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class EquivalenceException:
    case_id: str
    field: str
    compare_as: str
    evidence: str
    approver: str
    expires_at: str

    def active(self, today: dt.date | None = None) -> bool:
        return dt.date.fromisoformat(self.expires_at) >= (today or dt.date.today())


def load_exceptions(path: Path | str) -> list[EquivalenceException]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    if not isinstance(payload, list):
        raise ValueError("exceptions register must be a list")
    parsed = []
    for item in payload:
        if not isinstance(item, dict) or set(item) != {"case_id", "field", "compare_as", "evidence", "approver", "expires_at"}:
            raise ValueError("exception entries require case_id, field, compare_as, evidence, approver, expiry")
        if item["compare_as"] in {"skip", "equal", "always-equal"}:
            raise ValueError("exceptions must use a weaker compare_as predicate, never skip")
        parsed.append(EquivalenceException(**item))
    return parsed
